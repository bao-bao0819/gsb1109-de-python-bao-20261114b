"""知识条目的数据模型与 front matter 解析。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

# front matter 键名别名： canonical name -> 接受的别名（含中文）
_PREREQ_KEYS = ("prerequisites", "prereqs", "先决条目")
_ALT_KEYS = ("alternative_groups", "alternatives", "可选替代条目", "替代条目")
_HOURS_KEYS = ("estimate_hours", "duration", "预计导入耗时")
_ID_KEYS = ("id", "条目")


class ScheduleError(ValueError):
    """条目数据本身不合法（例如重复 id、耗时为负）。"""


@dataclass(frozen=True)
class Entry:
    """一篇知识条目及其依赖声明。

    prerequisites 中的每个条目都必须可用；
    alternative_groups 中的每组只需任意一个成员可用即可，
    这样替代路径上的循环不会堵死已经有另一路可用的条目。
    """

    entry_id: str
    prerequisites: tuple[str, ...]
    alternative_groups: tuple[tuple[str, ...], ...]
    estimate_hours: float
    source: Path | None = field(default=None)

    def requirement_groups(self) -> tuple[tuple[str, ...], ...]:
        """先决条目视为单元素组，与替代组统一成 OR/AND 结构。"""
        singleton = tuple((prereq,) for prereq in self.prerequisites)
        return singleton + self.alternative_groups


@dataclass
class EntryCatalog:
    """一个目录下全部知识条目的集合。"""

    entries: dict[str, Entry]

    @classmethod
    def from_directory(cls, directory: str | Path) -> "EntryCatalog":
        base = Path(directory)
        if not base.is_dir():
            raise ScheduleError(f"条目目录不存在: {base}")

        entries: dict[str, Entry] = {}
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".txt", ".markdown"}:
                continue
            post = frontmatter.load(path)
            entry = _entry_from_metadata(path.stem, path, post.metadata)
            if entry.entry_id in entries:
                raise ScheduleError(f"重复的条目 id: {entry.entry_id}")
            entries[entry.entry_id] = entry

        if not entries:
            raise ScheduleError(f"目录中没有找到知识条目: {base}")
        return cls(entries)

    def ids(self) -> set[str]:
        return set(self.entries)


def _first(metadata: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in metadata:
            return metadata[key]
    return None


def _as_str_list(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, (list, tuple)):
        raise ScheduleError(f"依赖声明必须是字符串或列表，实际为 {type(raw).__name__}: {raw!r}")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ScheduleError(f"依赖 id 必须是字符串，实际为 {item!r}")
        result.append(item)
    return tuple(result)


def _as_groups(raw: Any) -> tuple[tuple[str, ...], ...]:
    """支持三种写法：扁平列表（单组）、列表的列表（多组）、映射（先决 -> 替代品）。"""
    if raw is None:
        return ()
    if isinstance(raw, dict):
        groups: list[tuple[str, ...]] = []
        for prereq, substitutes in raw.items():
            members = [str(prereq), *_as_str_list(substitutes)]
            groups.append(tuple(dict.fromkeys(members)))
        return tuple(groups)
    if isinstance(raw, str):
        return ((raw,),)
    if not isinstance(raw, (list, tuple)):
        raise ScheduleError(f"替代条目声明格式无法识别: {raw!r}")

    groups = []
    for item in raw:
        if isinstance(item, str):
            groups.append((item,))
        elif isinstance(item, (list, tuple)):
            group = _as_str_list(item)
            if group:
                groups.append(group)
        else:
            raise ScheduleError(f"替代组必须是字符串或列表，实际为 {item!r}")
    return tuple(groups)


def _entry_from_metadata(stem: str, path: Path, metadata: dict[str, Any]) -> Entry:
    raw_id = _first(metadata, _ID_KEYS)
    entry_id = str(raw_id) if raw_id is not None else stem

    prerequisites = _as_str_list(_first(metadata, _PREREQ_KEYS))
    alternative_groups = _as_groups(_first(metadata, _ALT_KEYS))

    raw_hours = _first(metadata, _HOURS_KEYS)
    if raw_hours is None:
        estimate_hours = 0.0
    else:
        try:
            estimate_hours = float(raw_hours)
        except (TypeError, ValueError) as exc:
            raise ScheduleError(f"{entry_id} 的预计导入耗时不是数字: {raw_hours!r}") from exc
    if estimate_hours < 0:
        raise ScheduleError(f"{entry_id} 的预计导入耗时不能为负: {estimate_hours}")

    return Entry(
        entry_id=entry_id,
        prerequisites=prerequisites,
        alternative_groups=alternative_groups,
        estimate_hours=estimate_hours,
        source=path,
    )
