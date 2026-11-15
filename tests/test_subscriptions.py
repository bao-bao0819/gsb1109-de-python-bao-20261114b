#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tests for subscription views and membership-change notifications.
"""

import unittest

from frontmatter.subscriptions import (
    MembershipChange,
    MissingKeyCondition,
    RangeCondition,
    SubscriptionEngine,
    TagCondition,
    View,
)


class ConditionTest(unittest.TestCase):
    def test_tag_condition_scalar_and_list(self):
        self.assertTrue(TagCondition("tags", "py").matches({"tags": ["py"]}))
        self.assertTrue(TagCondition("lang", "py").matches({"lang": "py"}))
        self.assertFalse(TagCondition("tags", "rs").matches({"tags": ["py"]}))
        self.assertFalse(TagCondition("tags", "py").matches({}))

    def test_tag_condition_absent(self):
        condition = TagCondition("tags", "py", present=False)
        self.assertTrue(condition.matches({}))
        self.assertTrue(condition.matches({"tags": ["rs"]}))
        self.assertFalse(condition.matches({"tags": ["py"]}))

    def test_range_condition_bounds(self):
        condition = RangeCondition("score", min=1, max=5)
        self.assertTrue(condition.matches({"score": 1}))
        self.assertTrue(condition.matches({"score": 5}))
        self.assertFalse(condition.matches({"score": 6}))
        self.assertFalse(condition.matches({}))
        self.assertFalse(condition.matches({"score": "high"}))
        self.assertTrue(RangeCondition("score", min=10).matches({"score": 10}))

    def test_missing_key_condition(self):
        self.assertTrue(MissingKeyCondition("reviewed").matches({}))
        self.assertFalse(MissingKeyCondition("reviewed").matches({"reviewed": True}))
        self.assertTrue(
            MissingKeyCondition("reviewed", present=True).matches({"reviewed": True})
        )

    def test_view_validation(self):
        with self.assertRaises(ValueError):
            View("empty", match="all")
        with self.assertRaises(ValueError):
            View("bad", TagCondition("a", 1), match="nope")


class SubscriptionEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = SubscriptionEngine()
        self.python_view = View("python", TagCondition("tags", "python"))
        self.engine.add_view(self.python_view)
        self.engine.add_view(View("rust", TagCondition("tags", "rust")))

    def test_first_sync_is_silent_baseline(self):
        self.assertEqual(
            self.engine.sync("p1", {"tags": ["python"]}, revision="r0"), []
        )
        self.assertTrue(self.engine.get_membership("p1", "python"))
        self.assertFalse(self.engine.get_membership("p1", "rust"))

    def test_only_changed_membership_notifies(self):
        self.engine.prime("p1", {"tags": ["python"]})
        changes = self.engine.sync("p1", {"tags": ["python", "rust"]}, revision="r1")
        self.assertEqual(
            changes,
            [
                MembershipChange(
                    view_id="rust", action="joined", field_sets=(("tags",),)
                )
            ],
        )
        self.assertTrue(self.engine.get_membership("p1", "python"))
        self.assertTrue(self.engine.get_membership("p1", "rust"))

    def test_unrelated_field_change_is_silent(self):
        self.engine.prime("p1", {"tags": ["python"], "title": "old"})
        self.assertEqual(
            self.engine.sync(
                "p1", {"tags": ["python"], "title": "new"}, revision="r1"
            ),
            [],
        )

    def test_minimal_field_set_excludes_irrelevant_changes(self):
        view = View(
            "scored",
            TagCondition("tags", "news"),
            RangeCondition("score", min=10),
        )
        engine = SubscriptionEngine()
        engine.add_view(view)
        engine.prime("p1", {"tags": ["other"], "score": 1, "title": "old"})
        changes = engine.sync(
            "p1",
            {"tags": ["news"], "score": 99, "title": "new"},
            revision="r1",
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].action, "joined")
        self.assertEqual(changes[0].field_sets, (("score", "tags"),))

    def test_or_view_does_not_leave_while_one_condition_holds(self):
        view = View(
            "either",
            TagCondition("tags", "x"),
            TagCondition("category", "y"),
            match="any",
        )
        engine = SubscriptionEngine()
        engine.add_view(view)
        engine.prime("p1", {"tags": ["x"], "category": "y"})

        # One OR arm drops out, the other still holds: no unsubscribe notice.
        self.assertEqual(
            engine.sync("p1", {"tags": [], "category": "y"}, revision="r1"), []
        )
        self.assertTrue(engine.get_membership("p1", "either"))

        # The last arm dropping out finally leaves the view.
        changes = engine.sync("p1", {"tags": [], "category": "z"}, revision="r2")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].action, "left")
        self.assertEqual(changes[0].field_sets, (("category",),))

    def test_or_view_requires_all_fields_to_change_together_to_leave(self):
        view = View(
            "triple",
            TagCondition("a", "on"),
            TagCondition("b", "on"),
            TagCondition("c", "on"),
            match="any",
        )
        engine = SubscriptionEngine()
        engine.add_view(view)
        engine.prime("p1", {"a": "on", "b": "on", "c": "on"})

        # Any single change leaves two OR arms true: membership is unchanged.
        for key in ("a", "b", "c"):
            single = {"a": "on", "b": "on", "c": "on", key: "off"}
            scout = SubscriptionEngine()
            scout.add_view(view)
            scout.prime("p1", {"a": "on", "b": "on", "c": "on"})
            self.assertEqual(scout.sync("p1", single), [])

        changes = engine.sync("p1", {"a": "off", "b": "off", "c": "off"}, revision="r1")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].action, "left")
        # The three fields must change together; nothing smaller explains it.
        self.assertEqual(changes[0].field_sets, (("a", "b", "c"),))

    def test_and_view_requires_all_fields_to_change_together_to_join(self):
        view = View(
            "triple",
            TagCondition("a", "on"),
            TagCondition("b", "on"),
            TagCondition("c", "on"),
        )
        engine = SubscriptionEngine()
        engine.add_view(view)
        engine.prime("p1", {"a": "off", "b": "off", "c": "off"})
        changes = engine.sync("p1", {"a": "on", "b": "on", "c": "on"}, revision="r1")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].action, "joined")
        self.assertEqual(changes[0].field_sets, (("a", "b", "c"),))

    def test_or_join_reports_each_single_field_explanation(self):
        view = View(
            "either",
            TagCondition("a", "on"),
            TagCondition("b", "on"),
            match="any",
        )
        engine = SubscriptionEngine()
        engine.add_view(view)
        engine.prime("p1", {"a": "off", "b": "off"})
        changes = engine.sync("p1", {"a": "on", "b": "on"}, revision="r1")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].field_sets, (("a",), ("b",)))
        self.assertEqual(changes[0].fields, ("a",))

    def test_missing_key_drives_join_and_leave(self):
        view = View("unreviewed", MissingKeyCondition("reviewed"))
        engine = SubscriptionEngine()
        engine.add_view(view)
        engine.prime("p1", {"reviewed": True})
        changes = engine.sync("p1", {}, revision="r1")
        self.assertEqual(
            changes,
            [
                MembershipChange(
                    view_id="unreviewed",
                    action="joined",
                    field_sets=(("reviewed",),),
                )
            ],
        )
        changes = engine.sync("p1", {"reviewed": False}, revision="r2")
        self.assertEqual(changes[0].action, "left")
        self.assertEqual(changes[0].field_sets, (("reviewed",),))

    def test_revision_replay_does_not_notify_twice(self):
        engine = SubscriptionEngine()
        engine.add_view(self.python_view)
        engine.prime("p1", {"tags": ["rust"]})
        first = engine.sync("p1", {"tags": ["python"]}, revision="r1")
        self.assertEqual(len(first), 1)

        # Replaying the same revision, even with the old content, is a no-op.
        self.assertEqual(engine.sync("p1", {"tags": ["rust"]}, revision="r1"), [])
        self.assertTrue(engine.get_membership("p1", "python"))

        # A genuinely new revision can notify again.
        second = engine.sync("p1", {"tags": ["rust"]}, revision="r2")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].action, "left")

    def test_unchanged_metadata_without_revision_is_replay(self):
        engine = SubscriptionEngine()
        engine.add_view(self.python_view)
        engine.prime("p1", {"tags": ["python"]})
        self.assertEqual(engine.sync("p1", {"tags": ["python"]}), [])
        changes = engine.sync("p1", {"tags": ["rust"]})
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].action, "left")

    def test_several_views_flip_in_one_sync(self):
        self.engine.prime("p1", {"tags": []})
        changes = self.engine.sync("p1", {"tags": ["python", "rust"]}, revision="r1")
        self.assertEqual([change.view_id for change in changes], ["python", "rust"])
        for change in changes:
            self.assertEqual(change.action, "joined")
            self.assertEqual(change.field_sets, (("tags",),))


if __name__ == "__main__":
    unittest.main()
