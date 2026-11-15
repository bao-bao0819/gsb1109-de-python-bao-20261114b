#!/usr/bin/env python
# -*- coding: utf-8 -*-

import unittest

import frontmatter
from frontmatter import (
    DELETE_KEY,
    FieldSource,
    InheritancePolicy,
    Layer,
    MetadataIndex,
    merge_layers,
)
from frontmatter.inheritance import CLEARED, SET


POLICY = InheritancePolicy(
    clearable_fields=["summary", "tags", "references"],
    keyed_lists={"references": "id", "editors": "name"},
)


def layers(site, section, article):
    return [Layer("site", site), Layer("section", section), Layer("article", article)]


class ScalarInheritanceTest(unittest.TestCase):
    def test_missing_key_inherits_parent(self):
        resolution = merge_layers(
            layers({"layout": "book"}, {"author": "Ada"}, {"title": "Ch 1"}), POLICY
        )
        self.assertEqual(resolution.metadata["layout"], "book")
        self.assertEqual(resolution.metadata["author"], "Ada")
        self.assertEqual(resolution.metadata["title"], "Ch 1")
        self.assertEqual(resolution.sources["layout"], FieldSource("site", SET))
        self.assertEqual(resolution.sources["author"], FieldSource("section", SET))
        self.assertEqual(resolution.sources["title"], FieldSource("article", SET))

    def test_child_overrides_parent_value(self):
        resolution = merge_layers(
            layers({"layout": "book"}, {"layout": "guide"}, {}), POLICY
        )
        self.assertEqual(resolution.metadata["layout"], "guide")
        self.assertEqual(resolution.sources["layout"].layer, "section")

    def test_explicit_null_on_clearable_field_clears_inherited_value(self):
        resolution = merge_layers(
            layers(
                {"summary": "from site"},
                {"summary": None},
                {"title": "Ch 1"},
            ),
            POLICY,
        )
        self.assertNotIn("summary", resolution.metadata)
        self.assertEqual(
            resolution.sources["summary"], FieldSource("section", CLEARED)
        )
        # the article has no summary key at all, so it inherits the cleared state
        self.assertTrue(resolution.inherits("summary", "article"))

    def test_article_can_repopulate_cleared_field(self):
        resolution = merge_layers(
            layers(
                {"summary": "from site"},
                {"summary": None},
                {"summary": "article summary"},
            ),
            POLICY,
        )
        self.assertEqual(resolution.metadata["summary"], "article summary")
        self.assertEqual(resolution.sources["summary"], FieldSource("article", SET))

    def test_explicit_null_is_distinct_from_missing_key(self):
        # 'layout' is NOT clearable: an explicit null is a real stored value
        resolution = merge_layers(
            layers({"layout": "book"}, {"layout": None}, {}), POLICY
        )
        self.assertIn("layout", resolution.metadata)
        self.assertIsNone(resolution.metadata["layout"])
        self.assertEqual(resolution.sources["layout"], FieldSource("section", SET))

        # whereas an absent key simply inherits
        resolution2 = merge_layers(layers({"layout": "book"}, {}, {}), POLICY)
        self.assertEqual(resolution2.metadata["layout"], "book")

    def test_inherits_only_true_for_values_coming_from_above(self):
        resolution = merge_layers(
            layers({"layout": "book"}, {"author": "Ada"}, {"author": "Grace"}),
            POLICY,
        )
        self.assertTrue(resolution.inherits("layout", "article"))
        self.assertFalse(resolution.inherits("author", "article"))
        self.assertTrue(resolution.inherits("layout", "section"))
        self.assertFalse(resolution.inherits("author", "section"))


class KeyedListMergeTest(unittest.TestCase):
    def test_lists_merge_by_entry_identifier(self):
        resolution = merge_layers(
            layers(
                {"references": [{"id": "r1", "title": "One"}]},
                {"references": [{"id": "r2", "title": "Two"}]},
                {},
            ),
            POLICY,
        )
        self.assertEqual(
            resolution.metadata["references"],
            [
                {"id": "r1", "title": "One"},
                {"id": "r2", "title": "Two"},
            ],
        )
        self.assertEqual(
            resolution.source_of("references", "r1"), FieldSource("site", SET)
        )
        self.assertEqual(
            resolution.source_of("references", "r2"), FieldSource("section", SET)
        )

    def test_child_entry_overrides_same_identifier(self):
        resolution = merge_layers(
            layers(
                {"references": [{"id": "r1", "title": "Old"}]},
                {},
                {"references": [{"id": "r1", "title": "New"}]},
            ),
            POLICY,
        )
        self.assertEqual(
            resolution.metadata["references"], [{"id": "r1", "title": "New"}]
        )
        self.assertEqual(
            resolution.source_of("references", "r1"), FieldSource("article", SET)
        )

    def test_parent_removal_then_child_readd_are_distinct(self):
        resolution = merge_layers(
            layers(
                {"references": [{"id": "r1", "title": "Original"}]},
                {"references": [{"id": "r1", "_remove": True}]},
                {"references": [{"id": "r1", "title": "Re-added"}]},
            ),
            POLICY,
        )
        # effective value comes from the article, not the original site entry
        self.assertEqual(
            resolution.metadata["references"], [{"id": "r1", "title": "Re-added"}]
        )
        history = resolution.list_history["references"]["r1"]
        self.assertEqual(
            history,
            [
                FieldSource("site", SET),
                FieldSource("section", CLEARED),
                FieldSource("article", SET),
            ],
        )
        self.assertEqual(
            resolution.source_of("references", "r1"), FieldSource("article", SET)
        )

    def test_removal_without_readd_stays_gone(self):
        resolution = merge_layers(
            layers(
                {"references": [{"id": "r1", "title": "One"}, {"id": "r2"}]},
                {"references": [{"id": "r1", "_remove": True}]},
                {},
            ),
            POLICY,
        )
        self.assertEqual(resolution.metadata["references"], [{"id": "r2"}])
        self.assertIsNone(resolution.source_of("references", "r1"))
        self.assertEqual(
            [s.action for s in resolution.list_history["references"]["r1"]],
            [SET, CLEARED],
        )

    def test_remove_marker_is_stripped_from_payload(self):
        resolution = merge_layers(
            layers({}, {"references": [{"id": "r1", "title": "One"}]}, {}), POLICY
        )
        entry = resolution.metadata["references"][0]
        self.assertNotIn("_remove", entry)

    def test_custom_identifier_key(self):
        resolution = merge_layers(
            layers(
                {"editors": [{"name": "ada", "role": "author"}]},
                {"editors": [{"name": "ada", "role": "lead"}]},
                {},
            ),
            POLICY,
        )
        self.assertEqual(
            resolution.metadata["editors"], [{"name": "ada", "role": "lead"}]
        )

    def test_null_clears_whole_keyed_list_when_clearable(self):
        resolution = merge_layers(
            layers(
                {"references": [{"id": "r1"}]},
                {"references": None},
                {},
            ),
            POLICY,
        )
        self.assertNotIn("references", resolution.metadata)
        self.assertEqual(
            resolution.sources["references"], FieldSource("section", CLEARED)
        )

    def test_missing_identifier_raises(self):
        with self.assertRaises(ValueError):
            merge_layers(
                layers({}, {"references": [{"title": "no id"}]}, {}), POLICY
            )


class MetadataIndexTest(unittest.TestCase):
    def _index(self):
        index = MetadataIndex(
            {"layout": "book", "summary": "site summary"}, policy=POLICY
        )
        index.add_section(
            "math",
            {"author": "Ada", "references": [{"id": "r1", "title": "Axioms"}]},
        )
        index.add_article(
            "math",
            "inheriting",
            {"title": "Inheriting article"},
        )
        index.add_article(
            "math",
            "overriding",
            {"author": "Grace"},
        )
        index.add_article(
            "math",
            "clearing",
            {"summary": None},
        )
        return index

    def test_resolve_three_layers(self):
        index = self._index()
        resolution = index.resolve("math", "inheriting")
        self.assertEqual(resolution.metadata["layout"], "book")
        self.assertEqual(resolution.metadata["author"], "Ada")
        self.assertEqual(resolution.metadata["title"], "Inheriting article")
        self.assertEqual(
            resolution.metadata["references"], [{"id": "r1", "title": "Axioms"}]
        )

    def test_articles_inheriting_detects_overrides_and_clears(self):
        index = self._index()
        self.assertEqual(
            index.articles_inheriting("math", "author"),
            ("inheriting", "clearing"),
        )
        self.assertEqual(
            index.articles_inheriting("math", "summary"),
            ("inheriting", "overriding"),
        )
        self.assertEqual(
            index.articles_inheriting("math", "references"),
            ("inheriting", "overriding", "clearing"),
        )

    def test_section_revision_only_hits_inheriting_articles(self):
        index = self._index()
        affected = index.revise_section("math", {"author": "Katherine"})

        # the overriding article keeps its own author and is not reported
        self.assertEqual(
            index.resolve("math", "overriding").metadata["author"], "Grace"
        )
        self.assertNotIn("overriding", affected)

        # inheriting articles receive the new section default
        self.assertEqual(
            index.resolve("math", "inheriting").metadata["author"], "Katherine"
        )
        self.assertEqual(affected["inheriting"]["author"], ("Ada", "Katherine"))

        # clearing article inherits the (new) section value too
        self.assertEqual(
            index.resolve("math", "clearing").metadata["author"], "Katherine"
        )

    def test_section_revision_clears_via_null_for_clearable_field(self):
        index = self._index()
        affected = index.revise_section("math", {"summary": None})

        # only articles that inherited the site summary are affected;
        # the clearing article already had no summary
        self.assertEqual(set(affected), {"inheriting", "overriding"})
        for name in ("inheriting", "overriding"):
            self.assertNotIn("summary", index.resolve("math", name).metadata)
            self.assertEqual(affected[name]["summary"], ("site summary", DELETE_KEY))

    def test_section_revision_delete_key_differs_from_null(self):
        index = self._index()
        # deleting the section's own key makes articles inherit straight
        # from the site layer instead
        index.revise_section("math", {"author": "Katherine"})
        affected = index.revise_section("math", {"author": DELETE_KEY})
        self.assertEqual(
            index.resolve("math", "inheriting").metadata.get("author"), None
        )
        self.assertNotIn("author", index.resolve("math", "inheriting").metadata)
        self.assertEqual(affected["inheriting"]["author"], ("Katherine", DELETE_KEY))
        # the overriding article is untouched
        self.assertNotIn("overriding", affected)

    def test_unknown_section_raises(self):
        index = self._index()
        with self.assertRaises(KeyError):
            index.add_article("nope", "x", {})
        with self.assertRaises(KeyError):
            index.revise_section("nope", {})

    def test_keyed_list_revision_propagates_to_inheritors(self):
        index = self._index()
        index.add_article(
            "math",
            "own-list",
            {"references": [{"id": "r9", "title": "Local"}]},
        )
        affected = index.revise_section(
            "math",
            {
                "references": [
                    {"id": "r1", "_remove": True},
                    {"id": "r2", "title": "New"},
                ]
            },
        )
        inheriting_refs = index.resolve("math", "inheriting").metadata["references"]
        self.assertEqual(inheriting_refs, [{"id": "r2", "title": "New"}])
        self.assertIn("references", affected["inheriting"])

        # the article with its own list still merges on top of the section state
        own_refs = index.resolve("math", "own-list").metadata["references"]
        self.assertEqual(
            own_refs,
            [{"id": "r2", "title": "New"}, {"id": "r9", "title": "Local"}],
        )


class FrontmatterIntegrationTest(unittest.TestCase):
    def test_tuple_layers_and_default_policy(self):
        # without a policy every present key overrides and null is a real value
        resolution = merge_layers(
            [
                ("site", {"layout": "book", "draft": False}),
                ("article", {"draft": None}),
            ]
        )
        self.assertEqual(resolution.metadata["layout"], "book")
        self.assertIsNone(resolution.metadata["draft"])
        self.assertEqual(
            resolution.sources["draft"], FieldSource("article", SET)
        )
        self.assertEqual(resolution.layers[0].name, "site")

    def test_posts_work_as_layers(self):
        site = frontmatter.loads("---\nlayout: book\n---\nsite body")
        section = frontmatter.loads("---\nauthor: Ada\n---\nsection body")
        article = frontmatter.loads("---\ntitle: Ch 1\n---\narticle body")

        resolution = merge_layers(
            [Layer("site", site), Layer("section", section), Layer("article", article)],
            POLICY,
        )
        self.assertEqual(
            dict(resolution.metadata),
            {"layout": "book", "author": "Ada", "title": "Ch 1"},
        )

    def test_yaml_null_survives_as_explicit_none(self):
        post = frontmatter.loads("---\nlayout: null\n---\nbody")
        resolution = merge_layers(
            [Layer("site", {"layout": "book"}), Layer("article", post.metadata)],
            POLICY,
        )
        self.assertIn("layout", resolution.metadata)
        self.assertIsNone(resolution.metadata["layout"])


if __name__ == "__main__":
    unittest.main()
