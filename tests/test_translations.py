"""Tests for exporting translation groups from a local article collection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import frontmatter
from frontmatter.translations import export_translation_groups, main

ARTICLES = Path(__file__).parent.parent / "examples" / "translations" / "articles"


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    return export_translation_groups(ARTICLES)


def keys(report: dict[str, object], kind: str) -> list[set[str]]:
    return [set(item["keys"]) for item in report["conflicts"] if item["kind"] == kind]


def test_group_members_use_real_document_keys(report):
    three_language = [
        group
        for group in report["groups"]
        if set(group["members"].values())
        == {"en-nginx", "zh-nginx", "fr-nginx"}
    ]
    assert len(three_language) == 1
    assert three_language[0]["members"] == {
        "en": "en-nginx",
        "zh": "zh-nginx",
        "fr": "fr-nginx",
    }


def test_mutual_pairs_are_confirmed_in_both_directions(report):
    mutual = {(pair["a"], pair["b"]) for pair in report["mutual_pairs"]}
    assert ("en-nginx", "fr-nginx") in mutual
    assert ("en-nginx", "zh-nginx") in mutual
    assert ("en-redirect", "fr-redirect") in mutual


def test_one_way_declaration_is_not_promoted_to_a_group(report):
    one_way = {(item["from"], item["to"]) for item in report["one_way"]}
    assert ("en-decoration", "fr-decoration") in one_way
    assert ("fr-nginx", "zh-nginx") in one_way
    grouped = {key for group in report["groups"] for key in group["keys"]}
    assert "fr-decoration" not in grouped


def test_declaration_conflict_stays_out_of_groups(report):
    mismatches = keys(report, "language_mismatch")
    assert {"en-mismatch", "en-wrong-lang"} in mismatches
    grouped = {key for group in report["groups"] for key in group["keys"]}
    assert "en-mismatch" not in grouped


def test_two_french_primaries_block_the_whole_chain(report):
    blocked = report["blocked_chains"]
    assert len(blocked) == 1
    chain = blocked[0]
    assert chain["reason"] == "multiple_french_primaries"
    assert chain["members"] == [
        "en-routing",
        "fr-routing-1",
        "fr-routing-2",
        "zh-routing",
    ]
    assert chain["duplicate_languages"] == {"fr": ["fr-routing-1", "fr-routing-2"]}

    grouped = {key for group in report["groups"] for key in group["keys"]}
    assert grouped.isdisjoint(chain["members"])


def test_confirmed_pairs_inside_a_blocked_chain_are_still_listed(report):
    mutual = {(pair["a"], pair["b"]) for pair in report["mutual_pairs"]}
    assert ("en-routing", "fr-routing-1") in mutual
    assert ("fr-routing-2", "zh-routing") in mutual
    listed = {frozenset(pair) for pair in report["blocked_chains"][0]["pairs"]}
    assert frozenset({"en-routing", "fr-routing-1"}) in listed


def test_consistent_transitive_chain_forms_one_group(report):
    chained = [
        group
        for group in report["groups"]
        if set(group["members"].values()) == {"en-chain", "fr-chain", "zh-chain"}
    ]
    assert len(chained) == 1
    assert chained[0]["pairs"] == [
        ["en-chain", "fr-chain"],
        ["fr-chain", "zh-chain"],
    ]


def test_redirects_resolve_to_current_document_keys(report):
    redirects = {item["from"]: item for item in report["redirects"]}
    assert redirects["old/redirect-fr"]["to"] == "fr-redirect"
    assert redirects["old/redirect-fr"]["status"] == "redirect"
    assert redirects["old-link"]["chain"] == ["old-link", "old/redirect-fr"]
    assert redirects["old-link"]["to"] == "fr-redirect"


def test_same_title_draft_never_replaces_missing_translation(report):
    missing = {
        item["keys"][0]: item
        for item in report["conflicts"]
        if item["kind"] == "missing_target"
    }
    entry = missing["en-missing"]
    assert entry["target"] == "fr-release-notes"
    assert entry["draft_candidates"] == ["fr-release-notes-draft"]
    assert entry["draft_linked"] is False

    grouped = {key for group in report["groups"] for key in group["keys"]}
    assert "fr-release-notes-draft" not in grouped
    assert "en-missing" not in grouped


def test_same_title_non_draft_is_not_a_substitute_either(report):
    missing = {
        item["keys"][0]: item
        for item in report["conflicts"]
        if item["kind"] == "missing_target"
    }
    assert missing["en-widget"]["target"] == "fr-widget"
    grouped = {key for group in report["groups"] for key in group["keys"]}
    assert "fr-widget-copy" not in grouped


def test_ambiguous_and_duplicate_keys_are_reported(report):
    assert {"shared-a"} in keys(report, "ambiguous_target")
    duplicates = keys(report, "duplicate_document_key")
    assert {"dup-key"} in duplicates
    grouped = {key for group in report["groups"] for key in group["keys"]}
    assert "dup-key" not in grouped


def test_every_reported_key_exists_in_the_collection(report):
    known = set(report["documents"])
    for item in report["conflicts"]:
        if item["kind"] == "duplicate_document_key":
            continue
        for key in item.get("keys", []):
            assert key in known, key
        for key in item.get("draft_candidates", []):
            assert key in known, key
    for item in report["one_way"]:
        assert item["from"] in known and item["to"] in known
    for pair in report["mutual_pairs"]:
        assert pair["a"] in known and pair["b"] in known


def test_no_redirects_mode_keeps_old_links_unresolved(tmp_path):
    (tmp_path / "en.md").write_text(
        "---\nkey: en-a\nlang: en\ntranslations:\n  fr: /old/fr-a\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "fr.md").write_text(
        "---\nkey: fr-a\nlang: fr\nredirect_from:\n  - /old/fr-a\n"
        "translations:\n  en: en-a\n---\n",
        encoding="utf-8",
    )
    followed = export_translation_groups(tmp_path)
    assert {pair["a"] for pair in followed["mutual_pairs"]} == {"en-a"}

    unfollowed = export_translation_groups(tmp_path, follow_redirects=False)
    assert unfollowed["mutual_pairs"] == []
    assert unfollowed["conflicts"][0]["kind"] == "missing_target"


def test_cli_writes_json(tmp_path, capsys):
    assert main([str(ARTICLES)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["documents"] == len(payload["documents"])
    assert payload["blocked_chains"][0]["reason"] == "multiple_french_primaries"


def test_public_api_is_exported():
    assert frontmatter.export_translation_groups is export_translation_groups


def test_missing_directory_raises(tmp_path):
    with pytest.raises(NotADirectoryError):
        export_translation_groups(tmp_path / "absent")
