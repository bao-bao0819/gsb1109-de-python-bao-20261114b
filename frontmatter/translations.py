# -*- coding: utf-8 -*-
"""
Export confirmable translation groups from a local article collection.

Articles are Markdown files with front matter. Each article carries its own
document key and may declare translations by language::

    ---
    key: nginx-timeouts
    lang: en
    title: Nginx timeouts
    translations:
      zh: nginx-timeouts-zh
      fr: nginx-timeouts-fr
    ---

Declarations are only evidence, not proof. A pair is *mutual* when both
documents declare each other; a declaration without a matching reverse
declaration is reported as *one-way*. Conflicting language tags, ambiguous
targets, duplicate keys and broken redirects are reported separately.

Old links may live in ``redirect_from`` / ``aliases``. A declaration that
names an old link is resolved to the current document key and the redirect
chain is kept in the output.

Drafts never stand in for a missing translation. If a target is absent, the
exporter reports it as missing and only mentions same-title drafts as
candidates that were deliberately not linked.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import TextIO

__all__ = [
    "Declaration",
    "Document",
    "export_translation_groups",
    "main",
]


ARTICLE_SUFFIXES = frozenset({".md", ".markdown", ".mdx"})

_LANGUAGE_ALIASES = {
    "zh": "zh",
    "cn": "zh",
    "zho": "zh",
    "chinese": "zh",
    "中文": "zh",
    "en": "en",
    "eng": "en",
    "english": "en",
    "英文": "en",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "francais": "fr",
    "français": "fr",
    "法文": "fr",
    "法语": "fr",
}

_DRAFT_STATUSES = frozenset({"draft", "草稿", "drafting"})


@dataclass(frozen=True)
class Declaration:
    """A single translation declaration from one document to another."""

    language: str | None
    target: str


@dataclass(frozen=True)
class Document:
    """An article parsed from the collection."""

    key: str
    key_source: str
    path: str
    language: str | None
    title: str | None
    draft: bool
    declarations: tuple[Declaration, ...]
    redirect_from: tuple[str, ...]
    redirect_to: str | None


@dataclass(frozen=True)
class _Edge:
    source: str
    target: str
    language: str | None
    redirect_chain: tuple[str, ...]


def normalize_language(value: object) -> str | None:
    """Normalize a language tag to its primary subtag, when possible."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace("_", "-")
    if not text:
        return None
    alias = _LANGUAGE_ALIASES.get(text)
    if alias is not None:
        return alias
    base = text.split("-", 1)[0]
    return _LANGUAGE_ALIASES.get(base, base)


def _as_string(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _first_string(metadata: Mapping[str, object], names: Sequence[str]) -> str | None:
    for name in names:
        text = _as_string(metadata.get(name))
        if text is not None:
            return text
    return None


def _is_draft(metadata: Mapping[str, object]) -> bool:
    value = metadata.get("draft")
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
        return True
    status = metadata.get("status")
    if isinstance(status, str) and status.strip().lower() in _DRAFT_STATUSES:
        return True
    return False


def _declarations_for(language: object, target: object) -> list[Declaration]:
    lang = normalize_language(language)
    if isinstance(target, str):
        text = target.strip()
        return [Declaration(lang, text)] if text else []
    if isinstance(target, Mapping):
        nested = target.get("key")
        if nested is None:
            nested = target.get("doc")
        if nested is None:
            nested = target.get("target")
        if nested is None:
            nested = target.get("href")
        if isinstance(nested, str) and nested.strip():
            return [Declaration(lang, nested.strip())]
        if len(target) == 1:
            only_language, only_target = next(iter(target.items()))
            return _declarations_for(only_language, only_target)
        return []
    if isinstance(target, (list, tuple)):
        found: list[Declaration] = []
        for item in target:
            found.extend(_declarations_for(lang, item))
        return found
    return []


def _parse_translations(value: object) -> tuple[Declaration, ...]:
    declarations: list[Declaration] = []
    if isinstance(value, Mapping):
        for raw_language, raw_target in value.items():
            declarations.extend(_declarations_for(raw_language, raw_target))
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, Mapping):
                language = item.get("lang")
                if language is None:
                    language = item.get("language")
                declarations.extend(_declarations_for(language, item))
            elif isinstance(item, str):
                text = item.strip()
                if text:
                    declarations.append(Declaration(None, text))
    elif isinstance(value, str):
        text = value.strip()
        if text:
            declarations.append(Declaration(None, text))

    seen: set[tuple[str | None, str]] = set()
    unique: list[Declaration] = []
    for declaration in declarations:
        identity = (declaration.language, declaration.target)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(declaration)
    return tuple(unique)


def _parse_redirect_to(metadata: Mapping[str, object]) -> str | None:
    for name in ("redirect_to", "redirect"):
        value = metadata.get(name)
        if isinstance(value, str):
            inline = value.strip()
            if inline:
                return inline
        elif isinstance(value, Mapping):
            for field in ("url", "to", "target"):
                nested = _as_string(value.get(field))
                if nested is not None:
                    return nested
    return None


def _parse_redirects(metadata: Mapping[str, object]) -> tuple[str, ...]:
    redirects: list[str] = []
    for name in ("redirect_from", "aliases", "redirects"):
        value = metadata.get(name)
        if isinstance(value, str):
            text = value.strip()
            if text:
                redirects.append(text)
        elif isinstance(value, (list, tuple)):
            for item in value:
                item_text = _as_string(item)
                if item_text is not None:
                    redirects.append(item_text)

    seen: set[str] = set()
    unique: list[str] = []
    for redirect in redirects:
        if redirect in seen:
            continue
        seen.add(redirect)
        unique.append(redirect)
    return tuple(unique)


def _read_documents(
    root: Path,
) -> tuple[list[Document], list[dict[str, object]]]:
    from . import load

    documents: list[Document] = []
    problems: list[dict[str, object]] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in ARTICLE_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        try:
            post = load(str(path))
        except Exception as error:  # pragma: no cover - depends on malformed input
            problems.append(
                {
                    "kind": "unreadable_document",
                    "path": relative.as_posix(),
                    "detail": str(error),
                }
            )
            continue

        metadata = post.metadata
        metadata_key = _first_string(
            metadata, ("key", "document_key", "doc_key", "id")
        )
        key = metadata_key
        if key is None:
            key = relative.with_suffix("").as_posix()
        documents.append(
            Document(
                key=key,
                key_source="frontmatter" if metadata_key is not None else "path",
                path=relative.as_posix(),
                language=normalize_language(metadata.get("lang"))
                or normalize_language(metadata.get("language")),
                title=_as_string(metadata.get("title")),
                draft=_is_draft(metadata),
                declarations=_parse_translations(metadata.get("translations")),
                redirect_from=_parse_redirects(metadata),
                redirect_to=_parse_redirect_to(metadata),
            )
        )
    return documents, problems


def _normalize_reference(reference: str) -> str:
    text = reference.strip().strip("\"'").replace("\\", "/")
    if "://" in text:
        text = text.split("://", 1)[1]
        text = text.split("/", 1)[1] if "/" in text else ""
    text = text.split("#", 1)[0].split("?", 1)[0]
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _reference_variants(reference: str) -> tuple[str, ...]:
    normalized = _normalize_reference(reference)
    if not normalized:
        return ()
    variants = [normalized]
    path = Path(normalized)
    has_suffix = path.suffix.lower() in ARTICLE_SUFFIXES
    if has_suffix:
        variants.append(path.with_suffix("").as_posix())
    if "/" not in normalized:
        variants.append(path.name)
        variants.append(path.stem)
    seen: set[str] = set()
    unique: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.add(variant)
            unique.append(variant)
    return tuple(unique)


def _build_reference_index(
    documents: Mapping[str, Document],
) -> dict[str, set[str]]:
    """Map every acceptable spelling of a reference to real document keys."""
    index: dict[str, set[str]] = {}
    for key, document in documents.items():
        variants = set(_reference_variants(key))
        variants.update(_reference_variants(document.path))
        variants.add(document.path)
        stem = Path(document.path).stem
        variants.add(stem)
        for variant in variants:
            if variant:
                index.setdefault(variant, set()).add(key)
    return index


def _document_matches(
    reference: str,
    index: Mapping[str, set[str]],
) -> tuple[str, ...]:
    found: set[str] = set()
    for variant in _reference_variants(reference):
        found.update(index.get(variant, ()))
    return tuple(sorted(found))


def _resolve_reference(
    reference: str,
    index: Mapping[str, set[str]],
    links: Mapping[str, str],
    *,
    follow_redirects: bool,
) -> tuple[str | None, tuple[str, ...], str]:
    matches = _document_matches(reference, index)
    if len(matches) > 1:
        return None, (), "ambiguous"
    if len(matches) == 1:
        return matches[0], (), "direct"
    if not follow_redirects:
        return None, (), "missing"

    starts = [
        variant for variant in _reference_variants(reference) if variant in links
    ]
    if len(starts) > 1:
        return None, (), "ambiguous"
    if not starts:
        return None, (), "missing"

    chain: list[str] = []
    seen: set[str] = set()
    current = starts[0]
    while current in links:
        if current in seen:
            return None, tuple(chain), "cycle"
        seen.add(current)
        chain.append(current)
        current = links[current]
        matches = _document_matches(current, index)
        if len(matches) > 1:
            return None, tuple(chain), "ambiguous"
        if len(matches) == 1:
            return matches[0], tuple(chain), "redirect"
    matches = _document_matches(current, index)
    if len(matches) > 1:
        return None, tuple(chain), "ambiguous"
    if len(matches) == 1:
        return matches[0], tuple(chain), "redirect"
    return None, tuple(chain), "missing"


def _normalized_title(title: str | None) -> str | None:
    if title is None:
        return None
    text = " ".join(title.split()).casefold()
    return text or None


def _draft_lookalikes(
    source: Document,
    language: str | None,
    documents: Iterable[Document],
) -> list[str]:
    wanted = _normalized_title(source.title)
    if wanted is None:
        return []
    found: list[str] = []
    for candidate in documents:
        if candidate.key == source.key or not candidate.draft:
            continue
        if candidate.language != language:
            continue
        if _normalized_title(candidate.title) != wanted:
            continue
        found.append(candidate.key)
    return sorted(found)


def _pair_payload(
    left: str,
    right: str,
    documents: Mapping[str, Document],
) -> dict[str, object]:
    return {
        "a": left,
        "b": right,
        "languages": {
            str(documents[left].language): left,
            str(documents[right].language): right,
        },
    }


def export_translation_groups(
    root: str | PathLike[str],
    *,
    follow_redirects: bool = True,
) -> dict[str, object]:
    """Export translation groups that can be confirmed from local articles.

    Only mutually declared pairs become group members. One-way declarations,
    conflicts and unresolved references are reported alongside, always with
    the real document keys. A component that would contain two documents in
    the same language is never emitted as one group; its confirmed pairs are
    still listed individually.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise NotADirectoryError(str(root_path))

    scanned, conflicts = _read_documents(root_path)
    by_key: dict[str, list[Document]] = {}
    for document in scanned:
        by_key.setdefault(document.key, []).append(document)

    documents: dict[str, Document] = {}
    for key, group in by_key.items():
        if len(group) > 1:
            conflicts.append(
                {
                    "kind": "duplicate_document_key",
                    "keys": [key],
                    "paths": sorted(document.path for document in group),
                }
            )
            continue
        documents[key] = group[0]

    redirect_stubs: dict[str, str] = {}
    for key in sorted(documents):
        document = documents[key]
        if document.redirect_to is None:
            continue
        redirect_stubs[key] = _normalize_reference(document.redirect_to)
        del documents[key]

    ordered = [documents[key] for key in sorted(documents)]
    index = _build_reference_index(documents)

    aliases: dict[str, str] = {}
    blocked_aliases: set[str] = set()
    for document in ordered:
        for raw_alias in document.redirect_from:
            alias = _normalize_reference(raw_alias)
            if not alias or alias == document.key:
                continue
            shadowed = _document_matches(alias, index)
            if shadowed:
                conflicts.append(
                    {
                        "kind": "alias_shadows_document",
                        "keys": sorted(set(shadowed) | {document.key}),
                        "alias": alias,
                    }
                )
                blocked_aliases.add(alias)
                aliases.pop(alias, None)
                continue
            if alias in blocked_aliases:
                continue
            owner = aliases.get(alias)
            if owner is not None and owner != document.key:
                conflicts.append(
                    {
                        "kind": "alias_conflict",
                        "keys": sorted({owner, document.key}),
                        "alias": alias,
                    }
                )
                blocked_aliases.add(alias)
                aliases.pop(alias, None)
                continue
            aliases[alias] = document.key

    links: dict[str, str] = dict(aliases)
    links.update(redirect_stubs)

    redirects: list[dict[str, object]] = []
    for entry in sorted(aliases) + sorted(redirect_stubs):
        resolved, chain, status = _resolve_reference(
            entry,
            index,
            links,
            follow_redirects=True,
        )
        redirects.append(
            {
                "from": entry,
                "to": resolved,
                "chain": list(chain),
                "status": status,
                "source": "redirect_from" if entry in aliases else "redirect_to",
            }
        )
        if status == "cycle":
            conflicts.append(
                {
                    "kind": "redirect_cycle",
                    "keys": [entry],
                    "chain": list(chain),
                }
            )
        if entry in redirect_stubs and resolved is None:
            conflicts.append(
                {
                    "kind": "broken_redirect",
                    "keys": [entry],
                    "target": redirect_stubs[entry],
                    "chain": list(chain),
                    "status": status,
                }
            )

    edges: dict[tuple[str, str], _Edge] = {}
    unconfirmed: list[dict[str, object]] = []
    resolved_redirects: list[dict[str, object]] = []
    declared_links: set[tuple[str, str]] = set()

    for document in ordered:
        grouped: dict[str, list[Declaration]] = {}
        for declaration in document.declarations:
            grouped.setdefault(declaration.language or "", []).append(declaration)

        for language_key in sorted(grouped):
            declarations = grouped[language_key]
            targets = {declaration.target for declaration in declarations}
            language = declarations[0].language
            if len(targets) > 1:
                conflicts.append(
                    {
                        "kind": "ambiguous_target",
                        "keys": [document.key],
                        "declared_language": language,
                        "targets": sorted(targets),
                    }
                )
                continue

            declaration = declarations[0]
            resolved, chain, status = _resolve_reference(
                declaration.target,
                index,
                links,
                follow_redirects=follow_redirects,
            )
            if status == "cycle":
                conflicts.append(
                    {
                        "kind": "redirect_cycle",
                        "keys": [document.key],
                        "target": declaration.target,
                        "chain": list(chain),
                    }
                )
                continue
            if status == "ambiguous":
                conflicts.append(
                    {
                        "kind": "ambiguous_target",
                        "keys": [document.key],
                        "declared_language": language,
                        "target": declaration.target,
                    }
                )
                continue
            if resolved is None:
                lookalikes = _draft_lookalikes(document, language, ordered)
                conflicts.append(
                    {
                        "kind": "missing_target",
                        "keys": [document.key],
                        "declared_language": language,
                        "target": declaration.target,
                        "draft_candidates": lookalikes,
                        "draft_linked": False,
                    }
                )
                continue

            if chain:
                resolved_redirects.append(
                    {
                        "from": document.key,
                        "declared_target": declaration.target,
                        "resolved_target": resolved,
                        "chain": list(chain),
                    }
                )

            target = documents[resolved]
            if resolved == document.key:
                conflicts.append(
                    {
                        "kind": "self_declaration",
                        "keys": [document.key],
                        "declared_language": language,
                    }
                )
                continue
            if document.draft:
                unconfirmed.append(
                    {
                        "from": document.key,
                        "to": resolved,
                        "declared_language": language,
                        "reason": "draft_source",
                    }
                )
                continue
            if target.draft:
                unconfirmed.append(
                    {
                        "from": document.key,
                        "to": resolved,
                        "declared_language": language,
                        "reason": "draft_target",
                    }
                )
                continue
            if language is None:
                unconfirmed.append(
                    {
                        "from": document.key,
                        "to": resolved,
                        "declared_language": None,
                        "reason": "unknown_declared_language",
                    }
                )
                continue
            if document.language is None:
                conflicts.append(
                    {
                        "kind": "unknown_source_language",
                        "keys": [document.key],
                        "declared_language": language,
                        "target": resolved,
                    }
                )
                continue
            if language == document.language:
                conflicts.append(
                    {
                        "kind": "same_language_declaration",
                        "keys": [document.key, resolved],
                        "declared_language": language,
                    }
                )
                continue
            if target.language is None:
                conflicts.append(
                    {
                        "kind": "unknown_target_language",
                        "keys": [document.key, resolved],
                        "declared_language": language,
                    }
                )
                continue
            if target.language != language:
                conflicts.append(
                    {
                        "kind": "language_mismatch",
                        "keys": [document.key, resolved],
                        "declared_language": language,
                        "target_language": target.language,
                    }
                )
                continue

            declared_links.add((document.key, resolved))
            edges[(document.key, resolved)] = _Edge(
                source=document.key,
                target=resolved,
                language=language,
                redirect_chain=chain,
            )

    mutual: list[dict[str, object]] = []
    one_way: list[dict[str, object]] = []
    pairs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for left_key, right_key in sorted(edges):
        edge = edges[(left_key, right_key)]
        if (right_key, left_key) in edges:
            pair = (
                (left_key, right_key)
                if left_key < right_key
                else (right_key, left_key)
            )
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                pairs.append(pair)
                mutual.append(_pair_payload(pair[0], pair[1], documents))
        else:
            one_way.append(
                {
                    "from": left_key,
                    "to": right_key,
                    "declared_language": edge.language,
                    "via_redirect": list(edge.redirect_chain),
                }
            )
            source_language = documents[left_key].language
            for declaration in documents[right_key].declarations:
                if declaration.language != source_language:
                    continue
                elsewhere, _, _ = _resolve_reference(
                    declaration.target,
                    index,
                    links,
                    follow_redirects=follow_redirects,
                )
                if elsewhere is None or elsewhere == left_key:
                    continue
                conflicts.append(
                    {
                        "kind": "recipient_points_elsewhere",
                        "keys": sorted({left_key, right_key, elsewhere}),
                        "declared_language": source_language,
                        "declared_target": declaration.target,
                    }
                )

    def components_of(links: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
        parent: dict[str, str] = {key: key for key in documents}

        def find(key: str) -> str:
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left, right in links:
            union(left, right)

        grouped: dict[str, set[str]] = {}
        for key in documents:
            grouped.setdefault(find(key), set()).add(key)
        return grouped

    def language_breakdown(members: Iterable[str]) -> dict[str, list[str]]:
        by_language: dict[str, list[str]] = {}
        for key in sorted(members):
            language = documents[key].language
            if language is None:
                continue
            by_language.setdefault(language, []).append(key)
        return by_language

    def pair_membership(members: set[str]) -> list[list[str]]:
        return [
            [left, right]
            for left, right in pairs
            if left in members and right in members
        ]

    blocked_chains: list[dict[str, object]] = []
    blocked_members: set[str] = set()
    for members in components_of(declared_links).values():
        if len(members) < 2:
            continue
        by_language = language_breakdown(members)
        duplicate_languages = {
            language: keys
            for language, keys in sorted(by_language.items())
            if len(keys) > 1
        }
        unknown_language = sorted(
            key for key in members if documents[key].language is None
        )
        if not duplicate_languages and not unknown_language:
            continue
        reason = "multiple_primaries"
        if "fr" in duplicate_languages:
            reason = "multiple_french_primaries"
        elif unknown_language:
            reason = "unknown_language_member"
        blocked_members.update(members)
        blocked_chains.append(
            {
                "members": sorted(members),
                "languages": {
                    language: keys for language, keys in sorted(by_language.items())
                },
                "duplicate_languages": duplicate_languages,
                "unknown_language": unknown_language,
                "pairs": pair_membership(members),
                "reason": reason,
            }
        )

    groups: list[dict[str, object]] = []
    for members in components_of(pairs).values():
        if len(members) < 2 or members & blocked_members:
            continue
        by_language = language_breakdown(members)
        unknown_language = sorted(
            key for key in members if documents[key].language is None
        )
        if unknown_language or any(len(keys) > 1 for keys in by_language.values()):
            continue
        groups.append(
            {
                "members": {
                    language: keys[0] for language, keys in sorted(by_language.items())
                },
                "keys": sorted(members),
                "pairs": pair_membership(members),
            }
        )

    documents_payload = {
        key: {
            "key": document.key,
            "key_source": document.key_source,
            "path": document.path,
            "language": document.language,
            "title": document.title,
            "draft": document.draft,
            "translations": [
                {"language": declaration.language, "target": declaration.target}
                for declaration in document.declarations
            ],
            "redirect_from": list(document.redirect_from),
            "redirect_to": document.redirect_to,
        }
        for key, document in sorted(documents.items())
    }

    conflicts.sort(
        key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)
    )

    return {
        "root": str(root_path),
        "summary": {
            "documents": len(documents),
            "groups": len(groups),
            "mutual_pairs": len(mutual),
            "one_way": len(one_way),
            "blocked_chains": len(blocked_chains),
            "conflicts": len(conflicts),
        },
        "documents": documents_payload,
        "groups": groups,
        "blocked_chains": blocked_chains,
        "mutual_pairs": mutual,
        "one_way": one_way,
        "conflicts": conflicts,
        "redirects": redirects,
        "resolved_redirects": resolved_redirects,
        "unconfirmed": unconfirmed,
    }


def main(
    argv: Sequence[str] | None = None,
    stream: TextIO | None = None,
) -> int:
    """Command-line entry point: ``python -m frontmatter.translations DIR``."""
    parser = argparse.ArgumentParser(
        prog="python -m frontmatter.translations",
        description="Export confirmable translation groups from a local article set.",
    )
    parser.add_argument("root", help="directory containing Markdown articles")
    parser.add_argument(
        "--no-redirects",
        action="store_true",
        help="do not follow redirect_from/aliases when resolving targets",
    )
    parser.add_argument("--output", help="write JSON here instead of stdout")
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    report = export_translation_groups(
        arguments.root,
        follow_redirects=not arguments.no_redirects,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    if arguments.output:
        Path(arguments.output).write_text(text + "\n", encoding="utf-8")
    else:
        output = sys.stdout if stream is None else stream
        print(text, file=output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
