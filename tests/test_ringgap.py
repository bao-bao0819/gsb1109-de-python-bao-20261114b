import unittest

from ringgap import (
    RING_SIZE,
    Anchor,
    Snapshot,
    build_request_plan,
    classify,
    enumerate_states,
    generation_of,
    in_window,
    name_of,
    reconcile,
    report_from_dict,
    representative,
    slot_of,
    window_floor,
)


def device_snapshot(head, missing_sequences, sample_time):
    """模拟设备在头序号 ``head`` 时的目录快照。"""
    files = {}
    for sequence in range(window_floor(head), head + 1):
        if sequence in missing_sequences:
            continue
        files[name_of(sequence % RING_SIZE)] = float(sample_time)
    return Snapshot.from_mapping(sample_time, files)


def head_anchor(head, sample_time):
    return Anchor.build(name_of(head % RING_SIZE), head, sample_time)


def entries_by_name(bucket):
    return {entry.name: entry for entry in bucket}


class RingHelpersTest(unittest.TestCase):
    def test_hex_names_round_trip(self):
        self.assertEqual(name_of(0), "00")
        self.assertEqual(name_of(255), "ff")
        self.assertEqual(slot_of("0a"), 10)
        self.assertEqual(slot_of("ff"), 255)
        with self.assertRaises(ValueError):
            slot_of("zz")
        with self.assertRaises(ValueError):
            name_of(256)

    def test_representative_and_generation(self):
        self.assertEqual(generation_of(255), 0)
        self.assertEqual(generation_of(256), 1)
        self.assertEqual(representative(10, 265), 10)
        self.assertEqual(representative(10, 266), 266)
        self.assertEqual(representative(0, 255), 0)
        self.assertEqual(representative(0, 256), 256)

    def test_window_floor_and_in_window(self):
        self.assertEqual(window_floor(9), 0)
        self.assertEqual(window_floor(255), 0)
        self.assertEqual(window_floor(265), 10)
        self.assertTrue(in_window(10, 265))
        self.assertFalse(in_window(20, 9))

    def test_anchor_must_match_slot(self):
        with self.assertRaises(ValueError):
            Anchor.build("0a", 11, 0)
        anchor = Anchor.build("0a", 266, 0)
        self.assertEqual((anchor.slot, anchor.sequence), (10, 266))


class SingleLapTest(unittest.TestCase):
    def setUp(self):
        # 设备已产出 0..255，序号 10 丢件；速率 1 个/秒
        self.snapshot_a = device_snapshot(255, {10}, 0)
        self.snapshot_b = device_snapshot(265, {10}, 10)
        self.anchors = [head_anchor(255, 0), head_anchor(265, 10)]
        self.rate = 1.0

    def test_state_is_pinned(self):
        states, heads_b = enumerate_states(
            self.snapshot_a, self.snapshot_b, self.rate, self.anchors
        )
        self.assertEqual(states, ((265, 255),))
        self.assertEqual(heads_b, (265,))

    def test_gap_inside_window_is_requestable(self):
        result = classify(
            self.snapshot_a, self.snapshot_b, self.rate, self.anchors
        )
        self.assertEqual(len(result.overwritten), 0)
        self.assertEqual(len(result.uncertain), 0)
        requestable = entries_by_name(result.requestable)
        self.assertEqual(set(requestable), {"0a"})
        entry = requestable["0a"]
        self.assertEqual(
            (entry.absolute_sequence, entry.generation), (10, 0)
        )

    def test_receipt_moves_gap_to_confirmed(self):
        result = classify(
            self.snapshot_a,
            self.snapshot_b,
            self.rate,
            self.anchors,
            receipts=[10],
        )
        self.assertEqual(len(result.requestable), 0)
        confirmed = entries_by_name(result.confirmed)
        self.assertIn("0a", confirmed)
        self.assertEqual(confirmed["0a"].absolute_sequence, 10)


class OverwriteTest(unittest.TestCase):
    def test_aged_gap_is_overwritten(self):
        snapshot_a = device_snapshot(255, {10}, 0)
        # 头序号推进到 275，序号 266 占据槽位 0a，旧缺件 10 已滚出窗口
        snapshot_c = device_snapshot(275, set(), 20)
        anchors = [head_anchor(255, 0), head_anchor(275, 20)]
        result = classify(snapshot_a, snapshot_c, 1.0, anchors)
        overwritten = entries_by_name(result.overwritten)
        self.assertEqual(set(overwritten), {"0a"})
        self.assertEqual(overwritten["0a"].absolute_sequence, 10)
        self.assertEqual(len(result.requestable), 0)

    def test_reconcile_never_registers_new_file_as_old_gap(self):
        snapshot_b = device_snapshot(265, {10}, 10)
        snapshot_current = device_snapshot(275, set(), 20)
        anchors = [
            head_anchor(265, 10),
            head_anchor(275, 20),
        ]
        classification = classify(
            device_snapshot(255, {10}, 0),
            snapshot_b,
            1.0,
            [head_anchor(255, 0), head_anchor(265, 10)],
        )
        plan = build_request_plan(classification, 20)
        self.assertEqual(len(plan.items), 1)
        review = reconcile(plan, snapshot_b, snapshot_current, 1.0, anchors)
        self.assertEqual(len(review.keep), 0)
        self.assertEqual(len(review.fulfilled), 0)
        self.assertEqual({item.absolute_sequence for item in review.overwritten}, {10})

    def test_reconcile_keeps_live_gap_and_detects_backfill(self):
        snapshot_b = device_snapshot(265, {10}, 10)
        # 没有新产出，缺件仍在窗口内
        snapshot_unchanged = device_snapshot(265, {10}, 11)
        anchors = [head_anchor(265, 10), head_anchor(265, 11)]
        classification = classify(
            device_snapshot(255, {10}, 0),
            snapshot_b,
            1.0,
            [head_anchor(255, 0), head_anchor(265, 10)],
        )
        plan = build_request_plan(classification, 11)
        review = reconcile(plan, snapshot_b, snapshot_unchanged, 1.0, anchors)
        self.assertEqual({item.absolute_sequence for item in review.keep}, {10})

        # 序号 10 被补传，同名文件代次与计划一致
        snapshot_backfilled = device_snapshot(265, set(), 11)
        review_backfilled = reconcile(
            plan, snapshot_b, snapshot_backfilled, 1.0, anchors
        )
        self.assertEqual(
            {item.absolute_sequence for item in review_backfilled.fulfilled}, {10}
        )

    def test_reconcile_boundary_is_uncertain_without_current_anchor(self):
        snapshot_b = device_snapshot(265, {10}, 10)
        # 只有 t=10 的锚点，t=20 时头序号在 [265,275] 之间都相容：
        # 序号 10 在部分状态下仍在窗口、部分状态下已滚出
        snapshot_current = device_snapshot(275, {10}, 20)
        anchors = [head_anchor(265, 10)]
        classification = classify(
            device_snapshot(255, {10}, 0),
            snapshot_b,
            1.0,
            [head_anchor(255, 0), head_anchor(265, 10)],
        )
        plan = build_request_plan(classification, 20)
        review = reconcile(plan, snapshot_b, snapshot_current, 1.0, anchors)
        self.assertEqual({item.absolute_sequence for item in review.uncertain}, {10})
        self.assertFalse(review.keep or review.overwritten or review.fulfilled)

    def test_reconcile_receipt_marks_fulfilled(self):
        snapshot_b = device_snapshot(265, {10}, 10)
        snapshot_current = device_snapshot(265, {10}, 11)
        anchors = [head_anchor(265, 10), head_anchor(265, 11)]
        classification = classify(
            device_snapshot(255, {10}, 0),
            snapshot_b,
            1.0,
            [head_anchor(255, 0), head_anchor(265, 10)],
        )
        plan = build_request_plan(classification, 11)
        review = reconcile(
            plan, snapshot_b, snapshot_current, 1.0, anchors, receipts=[10]
        )
        self.assertEqual({item.absolute_sequence for item in review.fulfilled}, {10})


class UnpinnedAndEarlyRingTest(unittest.TestCase):
    def test_without_anchor_every_gap_is_uncertain(self):
        snapshot_a = device_snapshot(255, {10}, 0)
        snapshot_b = device_snapshot(265, {10}, 10)
        result = classify(snapshot_a, snapshot_b, 1.0, anchors=[])
        self.assertEqual(len(result.requestable), 0)
        self.assertEqual(len(result.overwritten), 0)
        self.assertTrue(result.uncertain)
        self.assertIsNone(result.uncertain[0].generation)

    def test_not_yet_produced_slots_are_not_gaps(self):
        # 设备只产出了 0..11，其余槽位是还没轮到，不是缺件
        snapshot_a = device_snapshot(9, set(), 0)
        snapshot_b = device_snapshot(11, set(), 2)
        result = classify(
            snapshot_a, snapshot_b, 1.0, [head_anchor(9, 0), head_anchor(11, 2)]
        )
        self.assertFalse(
            result.overwritten or result.requestable or result.uncertain
        )


class MultiWrapTest(unittest.TestCase):
    def test_fast_device_generations_stay_uncertain(self):
        # 锚点在 t=0(头序号 0)，速率上限 10/秒：
        # t=100 与 t=200 时头序号都有跨多代的相容取值，缺件代次无法唯一确定
        anchor = Anchor.build("00", 0, 0)
        snapshot_a = device_snapshot(1000, set(), 100)
        snapshot_b = device_snapshot(2000, {1788}, 200)
        result = classify(snapshot_a, snapshot_b, 10.0, [anchor])
        uncertain = entries_by_name(result.uncertain)
        self.assertIn("fc", uncertain)
        entry = uncertain["fc"]
        self.assertIsNone(entry.generation)
        self.assertGreater(len(entry.possible_sequences), 1)
        generations = {generation_of(seq) for seq in entry.possible_sequences}
        self.assertGreater(len(generations), 1)
        # 代次不确定的名字不得进入请求计划
        plan = build_request_plan(result, 200)
        self.assertNotIn("fc", {item.name for item in plan.items})

    def test_pinned_multiwrap_gap_is_still_requestable(self):
        # 同样是跨多圈的高速设备，但两个时刻都有精确锚点时状态可钉死
        snapshot_a = device_snapshot(1000, set(), 100)
        snapshot_b = device_snapshot(2000, {1788}, 200)
        anchors = [head_anchor(1000, 100), head_anchor(2000, 200)]
        result = classify(snapshot_a, snapshot_b, 10.0, anchors)
        requestable = entries_by_name(result.requestable)
        self.assertIn("fc", requestable)
        self.assertEqual(requestable["fc"].absolute_sequence, 1788)
        self.assertEqual(requestable["fc"].generation, generation_of(1788))

    def test_impossible_rate_raises(self):
        snapshot_a = device_snapshot(255, {10}, 0)
        snapshot_b = device_snapshot(265, {10}, 10)
        anchors = [head_anchor(255, 0), head_anchor(265, 10)]
        with self.assertRaises(ValueError):
            classify(snapshot_a, snapshot_b, 0.1, anchors)

    def test_file_cannot_vanish_within_same_generation(self):
        snapshot_a = device_snapshot(300, set(), 0)
        # 槽位 37(序号 55)在头 310 时仍在窗口内，却被人为删成缺件
        snapshot_b = device_snapshot(310, {55}, 10)
        anchors = [head_anchor(300, 0), head_anchor(310, 10)]
        states, _ = enumerate_states(snapshot_a, snapshot_b, 1.0, anchors)
        self.assertEqual(states, ())
        with self.assertRaises(ValueError):
            classify(snapshot_a, snapshot_b, 1.0, anchors)


class JsonEntryPointTest(unittest.TestCase):
    def test_report_from_dict_round_trip(self):
        payload = {
            "rate_per_sec": 1.0,
            "snapshot_a": {
                "time": 0,
                "files": {
                    name_of(seq % RING_SIZE): 0
                    for seq in range(256)
                    if seq != 10
                },
            },
            "snapshot_b": {
                "time": 10,
                "files": {
                    name_of(seq % RING_SIZE): 10
                    for seq in range(10, 266)
                    if seq != 10
                },
            },
            "anchors": [
                {"name": "ff", "sequence": 255, "time": 0},
                {"name": "09", "sequence": 265, "time": 10},
            ],
            "receipts": [],
        }
        report = report_from_dict(payload)
        requestable = {entry["name"]: entry for entry in report["requestable"]}
        self.assertEqual(requestable["0a"]["absolute_sequence"], 10)
        self.assertEqual(report["feasible_heads_b"], [265])


if __name__ == "__main__":
    unittest.main()
