"""命令行入口：python -m scheduler <条目目录> <专题id> [--capacity 小时]。"""

from __future__ import annotations

import argparse
import sys

from .model import EntryCatalog
from .plan import build_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="计算指定专题最早可交付的分批入库清单"
    )
    parser.add_argument("directory", help="知识条目所在目录（递归读取 .md/.txt/.markdown）")
    parser.add_argument("target", help="要交付的专题条目 id")
    parser.add_argument(
        "--capacity",
        type=float,
        default=None,
        help="每晚入库时段的容量上限（小时），默认不限制",
    )
    args = parser.parse_args(argv)

    catalog = EntryCatalog.from_directory(args.directory)
    result = build_plan(catalog, args.target, shift_capacity_hours=args.capacity)

    if not result.feasible:
        print(f"专题 {result.target} 无法发布。必须依赖在所有替代路径上都拿不到：")
        for blocker in result.blockers:
            print(
                f"- 缺失/不可达条目: {blocker.missing_entry_id} "
                f"（被 {blocker.required_by} 依赖）— {blocker.detail}"
            )
        return 1

    print(
        f"专题 {result.target} 最早可在第 {result.earliest_delivery_night()} 晚交付，"
        f"共 {len(result.batches)} 个批次，累计 {result.total_hours:g} 小时。"
    )
    for batch in result.batches:
        joined = ", ".join(batch.entries)
        print(f"第 {batch.night} 晚（{batch.total_hours:g} 小时）: {joined}")
    if result.chosen_alternatives:
        print("替代组选择：")
        for group, picked in result.chosen_alternatives.items():
            print(f"- {group} -> {picked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
