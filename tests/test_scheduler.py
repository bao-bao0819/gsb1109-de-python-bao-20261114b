"""scheduler 排期规则测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scheduler.model import EntryCatalog, ScheduleError
from scheduler.plan import build_plan


def write_entry(directory: Path, entry_id: str, body: str) -> None:
    (directory / f"{entry_id}.md").write_text(
        f"---\nid: {entry_id}\n{body}\n---\n正文 {entry_id}\n", encoding="utf-8"
    )


def nights_of(result):
    return {
        entry_id: batch.night
        for batch in result.batches
        for entry_id in batch.entries
    }


def test_chain_requires_next_night(tmp_path: Path) -> None:
    write_entry(tmp_path, "a", "预计导入耗时: 2")
    write_entry(tmp_path, "b", "先决条目: [a]\n预计导入耗时: 3")
    write_entry(tmp_path, "t", "先决条目: [b]\n预计导入耗时: 1")

    result = build_plan(EntryCatalog.from_directory(tmp_path), "t")

    assert result.feasible
    assert nights_of(result) == {"a": 1, "b": 2, "t": 3}
    assert result.earliest_delivery_night() == 3
    assert result.total_hours == 6


def test_independent_entries_share_first_night(tmp_path: Path) -> None:
    write_entry(tmp_path, "a", "")
    write_entry(tmp_path, "b", "")
    write_entry(tmp_path, "t", "先决条目: [a, b]")

    result = build_plan(EntryCatalog.from_directory(tmp_path), "t")

    assert nights_of(result) == {"a": 1, "b": 1, "t": 2}


def test_alternative_cycle_does_not_block_when_other_route_exists(
    tmp_path: Path,
) -> None:
    write_entry(tmp_path, "z", "")
    write_entry(tmp_path, "x", "可选替代条目:\n  - [y, z]")
    write_entry(tmp_path, "y", "可选替代条目:\n  - [x, z]")
    write_entry(tmp_path, "t", "先决条目: [x]")

    result = build_plan(EntryCatalog.from_directory(tmp_path), "t")

    assert result.feasible
    assert nights_of(result) == {"z": 1, "x": 2, "t": 3}
    assert result.chosen_alternatives["x#group0"] == "z"


def test_shorter_alternative_path_is_chosen(tmp_path: Path) -> None:
    write_entry(tmp_path, "a0", "")
    write_entry(tmp_path, "a1", "先决条目: [a0]")
    write_entry(tmp_path, "a", "先决条目: [a1]")
    write_entry(tmp_path, "b", "")
    write_entry(tmp_path, "t", "可选替代条目:\n  - [a, b]")

    result = build_plan(EntryCatalog.from_directory(tmp_path), "t")

    assert result.feasible
    assert set(nights_of(result)) == {"b", "t"}
    assert nights_of(result) == {"b": 1, "t": 2}
    assert result.chosen_alternatives["t#group0"] == "b"


def test_missing_prerequisite_is_reported_as_root_blocker(tmp_path: Path) -> None:
    write_entry(tmp_path, "t", "先决条目: [ghost]")

    result = build_plan(EntryCatalog.from_directory(tmp_path), "t")

    assert not result.feasible
    assert result.blockers[0].missing_entry_id == "ghost"
    assert result.blockers[0].required_by == "t"


def test_all_alternative_paths_missing_are_all_reported(tmp_path: Path) -> None:
    write_entry(tmp_path, "t", "可选替代条目:\n  - [ghost1, ghost2]")

    result = build_plan(EntryCatalog.from_directory(tmp_path), "t")

    assert not result.feasible
    missing = {blocker.missing_entry_id for blocker in result.blockers}
    assert missing == {"ghost1", "ghost2"}


def test_pure_prerequisite_cycle_without_escape_is_infeasible(tmp_path: Path) -> None:
    write_entry(tmp_path, "p", "先决条目: [q]")
    write_entry(tmp_path, "q", "先决条目: [p]")
    write_entry(tmp_path, "t", "先决条目: [p]")

    result = build_plan(EntryCatalog.from_directory(tmp_path), "t")

    assert not result.feasible
    assert any(blocker.detail.startswith("依赖形成闭环") for blocker in result.blockers)


def test_unknown_target(tmp_path: Path) -> None:
    write_entry(tmp_path, "a", "")

    result = build_plan(EntryCatalog.from_directory(tmp_path), "nope")

    assert not result.feasible
    assert result.blockers[0].missing_entry_id == "nope"


def test_shift_capacity_pushes_overflow_to_later_nights(tmp_path: Path) -> None:
    write_entry(tmp_path, "a", "预计导入耗时: 1.5")
    write_entry(tmp_path, "b", "预计导入耗时: 1.5")
    write_entry(tmp_path, "t", "先决条目: [a, b]\n预计导入耗时: 1")

    uncapped = build_plan(EntryCatalog.from_directory(tmp_path), "t")
    assert nights_of(uncapped) == {"a": 1, "b": 1, "t": 2}

    capped = build_plan(
        EntryCatalog.from_directory(tmp_path), "t", shift_capacity_hours=2
    )
    assert capped.feasible
    assert capped.batches[0].entries == ("a",)
    assert capped.batches[1].entries == ("b",)
    assert capped.batches[2].entries == ("t",)
    assert capped.earliest_delivery_night() == 3


def test_english_frontmatter_keys(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\nid: a\nestimate_hours: 2\n---\n", encoding="utf-8"
    )
    (tmp_path / "t.md").write_text(
        "---\nid: t\nprerequisites: [a]\nalternatives:\n  - [a]\n---\n",
        encoding="utf-8",
    )

    result = build_plan(EntryCatalog.from_directory(tmp_path), "t")

    assert result.feasible
    assert nights_of(result) == {"a": 1, "t": 2}


def test_duplicate_ids_raise(tmp_path: Path) -> None:
    write_entry(tmp_path, "a", "")
    (tmp_path / "a-copy.md").write_text("---\nid: a\n---\n", encoding="utf-8")

    with pytest.raises(ScheduleError):
        EntryCatalog.from_directory(tmp_path)
