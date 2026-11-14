#!/usr/bin/env python3
"""wirecheck — 旧板卡地址线交换排查工具.

场景: 一批旧板卡在同一页内交换了两三根地址线, 导出的 Intel HEX
记录校验和全部正常, 但文件里的地址被接线排列置换过. 本工具:

1. 用已核对的 (物理地址, 值) 对和"某段曲线必须单调"的约束,
   筛出与全部已读数据相容的接线排列;
2. 把在剩余排列下都指向同一物理位置的样本整理出来, 直接交付标定库;
3. 核对严格按地址逐条进行 —— 读到相同数值的两个地址绝不因此合并;
4. 若剩余候选不止一个(资料不足), 在实际可读且校准参考值覆盖的地址中,
   挑出按参考值最能区分候选的地址, 建议现场复测.

用法:
    python wirecheck.py spec.json     # 按规格文件分析
    python wirecheck.py --demo        # 运行内置合成示例

spec.json 示例:
    {
      "hex": "dump.hex",
      "bits": [0, 1, 2, 3],
      "permutations": [[[1, 3], [3, 1]]],          // 可选, 缺省按 bits 自动生成
      "verified": [[5, 11], [10, 29]],
      "monotonic": [{"start": 32, "end": 47, "direction": "up"}],
      "reference": [[0, 11], [1, 48]]               // 可选, 校准参考值
    }
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from itertools import combinations
from itertools import permutations as iter_permutations
from pathlib import Path

from intelhex import IntelHex


def apply_perm(addr, perm):
    """把物理地址按接线排列映射到导出文件中的地址."""
    out = addr
    for src, dst in perm.items():
        if (addr >> src) & 1:
            out |= 1 << dst
        else:
            out &= ~(1 << dst)
    return out


def invert_perm(perm):
    return {dst: src for src, dst in perm.items()}


def line_swap_permutations(bits, moved=(2, 3)):
    """生成"交换两三根线"的全部排列: 恰好移动 2 或 3 根指定地址线."""
    result = []
    for k in moved:
        for subset in combinations(sorted(bits), k):
            for target in iter_permutations(subset):
                if all(t != s for s, t in zip(subset, target)):
                    result.append(dict(zip(subset, target)))
    return result


def perm_label(perm):
    if not perm:
        return "identity"
    seen = set()
    cycles = []
    for start in sorted(perm):
        if start in seen:
            continue
        cycle = [start]
        seen.add(start)
        nxt = perm[start]
        while nxt != start:
            cycle.append(nxt)
            seen.add(nxt)
            nxt = perm[nxt]
        cycles.append("->".join("A%d" % b for b in cycle))
    return "  ".join(cycles)


@dataclass
class MonoRule:
    start: int
    end: int
    direction: str = "up"

    def __post_init__(self):
        if self.direction not in ("up", "down"):
            raise ValueError("direction 必须是 'up' 或 'down'")


@dataclass
class Probe:
    dump_addr: int
    predictions: dict
    distinct: int
    min_gap: int


@dataclass
class Analysis:
    survivors: list
    rejected: dict
    pinned: dict
    probe: Probe | None


def load_hex(path):
    """载入 Intel HEX; 载入过程即校验全部记录校验和."""
    ih = IntelHex(str(path))
    return {a: ih[a] for a in ih.addresses()}


def validate_verified(verified):
    seen = {}
    for addr, value in verified:
        if addr in seen and seen[addr] != value:
            raise ValueError(
                "已核对地址 0x%04X 出现两个不同的值 (0x%02X 与 0x%02X)"
                % (addr, seen[addr], value))
        seen[addr] = value


def check_perm(perm, data, verified, mono_rules):
    """返回 None 表示该排列与全部已读数据相容, 否则返回第一条不满足的原因.

    逐地址比对: 两个地址读到相同数值, 也绝不据此把它们当成同一个地址.
    """
    for paddr, expect in verified:
        d = apply_perm(paddr, perm)
        if d not in data:
            return "物理 0x%04X 的导出地址 0x%04X 不在 HEX 中" % (paddr, d)
        if data[d] != expect:
            return ("物理 0x%04X 应为 0x%02X, 该排列下读到 0x%02X"
                    % (paddr, expect, data[d]))
    for rule in mono_rules:
        seq = []
        for p in range(rule.start, rule.end + 1):
            d = apply_perm(p, perm)
            if d not in data:
                return ("单调段内物理 0x%04X 的导出地址 0x%04X 不在 HEX 中"
                        % (p, d))
            seq.append(data[d])
        if rule.direction == "up":
            broken = any(a > b for a, b in zip(seq, seq[1:]))
        else:
            broken = any(a < b for a, b in zip(seq, seq[1:]))
        if broken:
            return ("单调段 0x%04X..0x%04X (%s) 被破坏"
                    % (rule.start, rule.end, rule.direction))
    return None


def pinned_samples(survivors, data):
    """在全部剩余排列下都指向同一物理位置的样本 {物理地址: 值}."""
    inverses = [invert_perm(s) for s in survivors]
    pinned = {}
    for dump_addr in sorted(data):
        phys = {apply_perm(dump_addr, inv) for inv in inverses}
        if len(phys) == 1:
            pinned[phys.pop()] = data[dump_addr]
    return pinned


def best_probe(survivors, data, reference):
    """在实际可读的导出地址中, 挑按校准参考值最能区分剩余候选的复测地址.

    评分: 先看候选预测值的不同取值个数(越多越好), 再看相邻取值的最小
    间隔(越大越耐测量误差). 候选预测完全一致的地址没有区分价值.
    """
    if len(survivors) < 2:
        return None
    inverses = [invert_perm(s) for s in survivors]
    best = None
    for dump_addr in sorted(data):
        phys = [apply_perm(dump_addr, inv) for inv in inverses]
        if any(p not in reference for p in phys):
            continue
        values = [reference[p] for p in phys]
        distinct = len(set(values))
        if distinct < 2:
            continue
        ordered = sorted(values)
        min_gap = min(b - a for a, b in zip(ordered, ordered[1:]))
        if best is None or (distinct, min_gap) > (best.distinct, best.min_gap):
            predictions = {
                perm_label(s): (p, reference[p])
                for s, p in zip(survivors, phys)
            }
            best = Probe(dump_addr, predictions, distinct, min_gap)
    return best


def analyze(data, perms, verified, mono_rules, reference=None):
    validate_verified(verified)
    survivors = []
    rejected = {}
    for perm in perms:
        reason = check_perm(perm, data, verified, mono_rules)
        if reason is None:
            survivors.append(perm)
        else:
            rejected[perm_label(perm)] = reason
    pinned = pinned_samples(survivors, data) if survivors else {}
    probe = None
    if reference and len(survivors) > 1:
        probe = best_probe(survivors, data, reference)
    return Analysis(survivors, rejected, pinned, probe)


def print_report(result):
    print("== 接线排列判定 ==")
    total = len(result.survivors) + len(result.rejected)
    print("候选排列 %d 个, 相容 %d 个:" % (total, len(result.survivors)))
    for perm in result.survivors:
        print("  [相容] %s" % perm_label(perm))
    for label, reason in result.rejected.items():
        print("  [排除] %-24s %s" % (label, reason))
    if not result.survivors:
        print("!! 没有任何排列与已读数据相容, 请检查已核对数据或扩大允许排列集合")
        return

    print()
    print("== 可交付标定库的样本 ==")
    print("共 %d 个物理地址在全部剩余排列下指向同一位置:" % len(result.pinned))
    for addr in sorted(result.pinned)[:16]:
        print("  物理 0x%04X = 0x%02X" % (addr, result.pinned[addr]))
    if len(result.pinned) > 16:
        print("  ... (其余 %d 个从略)" % (len(result.pinned) - 16))

    if len(result.survivors) == 1:
        print()
        print("== 结论 ==")
        print("接线排列已唯一确定: %s" % perm_label(result.survivors[0]))
        return

    print()
    print("== 资料不足, 建议现场复测 ==")
    if result.probe is None:
        print("没有可参考校准值区分的可读地址, 请补充校准参考值后重试")
        return
    probe = result.probe
    print("建议复测导出地址 0x%04X (可区分 %d 组候选, 参考值最小间隔 %d):"
          % (probe.dump_addr, probe.distinct, probe.min_gap))
    for label, (phys, value) in probe.predictions.items():
        print("  %-24s 物理 0x%04X, 参考值 0x%02X" % (label, phys, value))


def run_spec(path):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    data = load_hex(spec["hex"])
    if "permutations" in spec:
        perms = [{int(s): int(d) for s, d in p} for p in spec["permutations"]]
    else:
        perms = line_swap_permutations(spec["bits"])
    verified = [tuple(pair) for pair in spec.get("verified", [])]
    rules = [MonoRule(r["start"], r["end"], r.get("direction", "up"))
             for r in spec.get("monotonic", [])]
    ref_raw = spec.get("reference")
    reference = None
    if ref_raw:
        if isinstance(ref_raw, dict):
            reference = {int(a, 0): v for a, v in ref_raw.items()}
        else:
            reference = {a: v for a, v in ref_raw}
    print_report(analyze(data, perms, verified, rules, reference))


def run_demo():
    bits = [0, 1, 2, 3]
    true_perm = {1: 3, 3: 1}
    rho = {0: 1, 1: 0}
    content = {a: (a * 37 + 11) & 0xFF for a in range(0x100)}
    ramp = [0x10, 0x12, 0x12, 0x16, 0x18, 0x1A, 0x1A, 0x1E,
            0x20, 0x22, 0x22, 0x26, 0x28, 0x2A, 0x2A, 0x2E]
    for off, value in enumerate(ramp):
        content[0x20 + off] = value
    verified_points = (0x05, 0x11, 0x27)
    for p in verified_points:
        content[apply_perm(p, rho)] = content[p]
    data = {d: content[apply_perm(d, true_perm)] for d in content}
    verified = [(a, content[a]) for a in verified_points]
    rules = [MonoRule(0x20, 0x2F, "up")]
    reference = dict(content)

    with tempfile.TemporaryDirectory() as tmp:
        hex_path = Path(tmp) / "dump.hex"
        ih = IntelHex()
        ih.fromdict(data)
        ih.write_hex_file(str(hex_path))
        loaded = load_hex(hex_path)

    print("(合成示例: 真实接线为 %s, 曲线段 0x20..0x2F 单调不减, "
          "HEX 校验和正常)" % perm_label(true_perm))
    print()
    result = analyze(loaded, line_swap_permutations(bits), verified, rules,
                     reference)
    print_report(result)
    assert true_perm in result.survivors
    assert len(result.survivors) > 1 and result.probe is not None


def main(argv):
    if "--demo" in argv:
        run_demo()
        return 0
    if len(argv) != 1:
        print(__doc__)
        return 2
    run_spec(argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
