"""多母版字体交付前的授权素材包最低成本采购求解器。

规则:
- 每个 UFO 目录视为一个母版; 必需字符必须在**所有**母版中可用才算补齐。
- 采购只准从清单中的授权包补字; 包有成本、适用母版列表和可供字形集合。
- 包内字形若带组件, 组件必须来自同包或已持有(现有 UFO / 其他已购包)的字形,
  且组件本身也要可用(递归)。只堆轮廓却缺基础组件的包不算完成。
- 目标: 覆盖全部必需字符的总价最低组合; 若无解, 列明每个字符在哪个母版、
  哪条部件链上缺货。

用法:
    python solver.py --ufo MASTER1.ufo --ufo MASTER2.ufo --packages packages.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import ufoLib2


@dataclass
class Master:
    name: str
    glyphs: dict[str, list[str]]  # 字形名 -> 组件基字形列表
    cmap: dict[str, str]  # 字符 -> 字形名


@dataclass
class Package:
    name: str
    cost: int
    masters: list[str] | None  # None 表示全部母版
    glyphs: dict[str, list[str]]

    def licensed_for(self, master_name: str) -> bool:
        return self.masters is None or master_name in self.masters


def load_master(path: Path) -> Master:
    font = ufoLib2.Font.open(path)
    glyphs = {
        glyph.name: [component.baseGlyph for component in glyph.components]
        for glyph in font
    }
    cmap = {}
    for glyph in font:
        for codepoint in glyph.unicodes:
            cmap.setdefault(chr(codepoint), glyph.name)
    return Master(name=path.stem, glyphs=glyphs, cmap=cmap)


def usable_closure(
    existing: dict[str, list[str]],
    packs: list[Package],
    master_name: str,
) -> set[str]:
    """计算某母版下的可用字形闭包: 现有字形 + 已购包中组件链完整的字形。"""
    usable: set[str] = set()
    providers: list[dict[str, list[str]]] = [existing]
    for pack in packs:
        if pack.licensed_for(master_name):
            providers.append(pack.glyphs)

    changed = True
    while changed:
        changed = False
        for source in providers:
            for glyph_name, components in source.items():
                if glyph_name not in usable and all(
                    component in usable for component in components
                ):
                    usable.add(glyph_name)
                    changed = True
    return usable


def resolve_required(
    required: list[str], masters: list[Master]
) -> tuple[dict[str, str | None], list[str]]:
    """把必需条目(单字符或字形名)解析为字形名; 返回 {条目: 字形名或None}。"""
    resolved: dict[str, str | None] = {}
    errors: list[str] = []
    for entry in required:
        if len(entry) == 1:
            glyph_name = next(
                (m.cmap[entry] for m in masters if entry in m.cmap), None
            )
            if glyph_name is None:
                errors.append(f"字符 {entry!r} 在所有母版中都没有 unicode 映射")
            resolved[entry] = glyph_name
        else:
            resolved[entry] = entry
    return resolved, errors


def find_min_costCombination(
    masters: list[Master],
    packs: list[Package],
    required_glyphs: set[str],
) -> tuple[list[Package], int] | None:
    """枚举子集求最低总价; 包数规模适合穷举(清单通常只有几十个包)。"""
    best: tuple[list[Package], int] | None = None
    for size in range(len(packs) + 1):
        from itertools import combinations

        for combo in combinations(packs, size):
            cost = sum(pack.cost for pack in combo)
            if best is not None and cost >= best[1]:
                continue
            if all(
                required_glyphs
                <= usable_closure(master.glyphs, list(combo), master.name)
                for master in masters
            ):
                best = (list(combo), cost)
    return best


def diagnose_glyph(
    glyph_name: str,
    master: Master,
    packs: list[Package],
    usable_max: set[str],
) -> str:
    """在"全部包都买下"的最大情形下, 追踪 glyph_name 缺货的部件链。"""

    def providers_of(name: str) -> list[tuple[str, list[str]]]:
        sources: list[tuple[str, list[str]]] = []
        if name in master.glyphs:
            sources.append(("现有 UFO", master.glyphs[name]))
        for pack in packs:
            if pack.licensed_for(master.name) and name in pack.glyphs:
                sources.append((f"授权包 {pack.name}", pack.glyphs[name]))
        return sources

    def trace(name: str, visited: frozenset[str]) -> list[str] | None:
        """返回从 name 出发到缺货部件的链; 若 name 本身无来源则立即返回。"""
        sources = providers_of(name)
        if not sources:
            return [name]
        if name in visited:
            return None
        visited = visited | {name}
        # 优先沿现有 UFO 的定义追踪, 否则沿任一授权包的定义追踪
        for _origin, components in sources:
            for component in components:
                if component in usable_max:
                    continue
                sub = trace(component, visited)
                if sub is not None:
                    return [name] + sub
        return None

    chain = trace(glyph_name, frozenset())
    if chain is None:
        return f"字形 {glyph_name} 不可用(组件循环或定义缺失)"
    missing = chain[-1]
    path = " -> ".join(chain)
    return f"部件链 {path} 缺货(现有 UFO 与所有授权包均未提供 {missing!r})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ufo", action="append", required=True, help="母版 UFO 目录, 可多次指定")
    parser.add_argument("--packages", required=True, help="授权包清单 JSON")
    parser.add_argument("--required", nargs="*", help="必需字符/字形, 覆盖清单中的 required")
    args = parser.parse_args(argv)

    masters = [load_master(Path(path)) for path in args.ufo]
    master_names = [master.name for master in masters]

    manifest = json.loads(Path(args.packages).read_text(encoding="utf-8"))
    packs = [
        Package(
            name=entry["name"],
            cost=entry["cost"],
            masters=entry.get("masters"),
            glyphs={name: list(comps) for name, comps in entry["glyphs"].items()},
        )
        for entry in manifest["packages"]
    ]
    for pack in packs:
        unknown = set(pack.masters or []) - set(master_names)
        if unknown:
            print(f"警告: 包 {pack.name} 声明了未知母版 {sorted(unknown)}", file=sys.stderr)

    required = args.required if args.required else manifest.get("required", [])
    resolved, errors = resolve_required(required, masters)
    required_glyphs = {name for name in resolved.values() if name is not None}

    result = find_min_costCombination(masters, packs, required_glyphs)

    if result is not None and not errors:
        combo, total = result
        print(f"最低总价: {total}")
        print("采购组合:")
        for pack in sorted(combo, key=lambda p: p.name):
            print(f"  - {pack.name} (成本 {pack.cost})")
        print("覆盖明细:")
        for entry, glyph_name in resolved.items():
            label = f"{entry!r} ({glyph_name})" if entry != glyph_name else glyph_name
            per_master = []
            for master in masters:
                existing = usable_closure(master.glyphs, [], master.name)
                source = "现有" if glyph_name in existing else "采购包"
                per_master.append(f"{master.name}={source}")
            print(f"  {label}: {', '.join(per_master)}")
        return 0

    # 无解: 在最大(全购)情形下逐字符、逐母版诊断缺货部件链
    print("无解: 以下必需字符无法补齐")
    for error in errors:
        print(f"  {error}")
    for entry, glyph_name in resolved.items():
        if glyph_name is None:
            continue
        uncovered = []
        for master in masters:
            usable_max = usable_closure(master.glyphs, packs, master.name)
            if glyph_name not in usable_max:
                uncovered.append((master, usable_max))
        if not uncovered:
            continue
        label = f"{entry!r} ({glyph_name})" if entry != glyph_name else glyph_name
        print(f"字符 {label}:")
        for master, usable_max in uncovered:
            detail = diagnose_glyph(glyph_name, master, packs, usable_max)
            print(f"  母版 {master.name}: {detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
