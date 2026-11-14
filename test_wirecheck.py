"""wirecheck 的单元测试."""

import pytest

from wirecheck import (MonoRule, analyze, apply_perm, best_probe, check_perm,
                       invert_perm, line_swap_permutations, perm_label,
                       pinned_samples, validate_verified)


def make_dump(true_perm, content):
    return {d: content[apply_perm(d, true_perm)] for d in content}


def test_apply_perm_swaps_bits():
    perm = {1: 3, 3: 1}
    assert apply_perm(0b0000, perm) == 0b0000
    assert apply_perm(0b0010, perm) == 0b1000
    assert apply_perm(0b1000, perm) == 0b0010
    assert apply_perm(0b1010, perm) == 0b1010
    assert apply_perm(0xF2, perm) == 0xF8
    assert apply_perm(0xF0, perm) == 0xF0


def test_invert_perm_roundtrip():
    perm = {0: 2, 2: 3, 3: 0}
    for addr in range(0x100):
        assert apply_perm(apply_perm(addr, perm), invert_perm(perm)) == addr


def test_generator_counts_and_derangement():
    perms = line_swap_permutations([0, 1, 2, 3])
    assert len(perms) == 6 + 8
    for perm in perms:
        assert len(perm) in (2, 3)
        assert all(src != dst for src, dst in perm.items())


def test_perm_label():
    assert perm_label({}) == "identity"
    assert perm_label({1: 3, 3: 1}) == "A1->A3"
    assert perm_label({0: 2, 2: 3, 3: 0}) == "A0->A2->A3"


def sample_setup():
    true_perm = {1: 3, 3: 1}
    content = {a: (a * 37 + 11) & 0xFF for a in range(0x100)}
    for i, a in enumerate(range(0x20, 0x30)):
        content[a] = 0x10 + 2 * i
    data = make_dump(true_perm, content)
    return true_perm, content, data


def test_check_perm_accepts_true_wiring():
    true_perm, content, data = sample_setup()
    verified = [(a, content[a]) for a in (0x05, 0x0A, 0x11, 0x27)]
    rules = [MonoRule(0x20, 0x2F, "up")]
    assert check_perm(true_perm, data, verified, rules) is None


def test_check_perm_rejects_wrong_wiring():
    true_perm, content, data = sample_setup()
    verified = [(a, content[a]) for a in (0x05, 0x0A, 0x11, 0x27)]
    rules = [MonoRule(0x20, 0x2F, "up")]
    wrong = {0: 1, 1: 0}
    assert check_perm(wrong, data, verified, rules) is not None


def test_monotonic_rule_filters():
    true_perm, content, data = sample_setup()
    rules = [MonoRule(0x20, 0x2F, "up")]
    kept = [p for p in line_swap_permutations([0, 1, 2, 3])
            if check_perm(p, data, [], rules) is None]
    assert true_perm in kept
    assert len(kept) < 14


def test_same_value_addresses_stay_distinct():
    true_perm = {1: 3, 3: 1}
    content = {a: (a * 13 + 7) & 0xFF for a in range(0x100)}
    content[0x02] = 0x55
    content[0x08] = 0x55
    data = make_dump(true_perm, content)
    verified = [(0x02, 0x55), (0x08, 0x55)]
    assert check_perm(true_perm, data, verified, []) is None
    swapped = {1: 3, 3: 0, 0: 1}
    assert apply_perm(0x02, swapped) == apply_perm(0x02, true_perm)
    assert apply_perm(0x08, swapped) != apply_perm(0x08, true_perm)
    assert data[apply_perm(0x08, swapped)] != 0x55
    assert check_perm(swapped, data, verified, []) is not None


def test_validate_verified_conflict():
    validate_verified([(1, 0x10), (2, 0x10), (1, 0x10)])
    with pytest.raises(ValueError):
        validate_verified([(1, 0x10), (1, 0x20)])


def test_pinned_samples_require_unanimity():
    survivors = [{1: 3, 3: 1}, {1: 2, 2: 1}]
    data = {a: a & 0xFF for a in range(0x100)}
    pinned = pinned_samples(survivors, data)
    for phys, value in pinned.items():
        dumps = {apply_perm(phys, s) for s in survivors}
        assert len(dumps) == 1
        assert data[dumps.pop()] == value
    assert 0x02 not in pinned
    assert 0x00 in pinned
    assert 0x10 in pinned


def test_best_probe_picks_most_discriminating():
    survivors = [{1: 3, 3: 1}, {0: 1, 1: 0}]
    data = {a: 0 for a in range(0x100)}
    reference = {a: 0 for a in range(0x100)}
    reference[0x02] = 0x10
    reference[0x08] = 0x80
    probe = best_probe(survivors, data, reference)
    assert probe is not None
    assert probe.dump_addr == 0x02
    assert probe.distinct == 2
    assert probe.min_gap == 0x80


def test_best_probe_none_when_unique_survivor():
    assert best_probe([{1: 3, 3: 1}], {0: 1}, {0: 1}) is None


def test_analyze_end_to_end():
    true_perm, content, data = sample_setup()
    verified = [(a, content[a]) for a in (0x05, 0x0A, 0x11, 0x27)]
    rules = [MonoRule(0x20, 0x2F, "up")]
    result = analyze(data, line_swap_permutations([0, 1, 2, 3]),
                     verified, rules, dict(content))
    assert true_perm in result.survivors
    assert len(result.survivors) + len(result.rejected) == 14
    if len(result.survivors) > 1:
        assert result.probe is not None
        assert result.probe.dump_addr in data
