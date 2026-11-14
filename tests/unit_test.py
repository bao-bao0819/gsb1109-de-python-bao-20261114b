#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import shutil
import tempfile
import textwrap
import unittest

import frontmatter
from frontmatter.default_handlers import YAMLHandler, JSONHandler, TOMLHandler

try:
    import pyaml
except ImportError:
    pyaml = None
try:
    import toml
except ImportError:
    toml = None


class FrontmatterTest(unittest.TestCase):
    """
    Tests for parsing various kinds of content and metadata
    """

    maxDiff = None

    def test_with_markdown_content(self):
        "Parse frontmatter and only the frontmatter"
        post = frontmatter.load("tests/yaml/hello-markdown.md")

        metadata = {"author": "bob", "something": "else", "test": "tester"}
        for k, v in metadata.items():
            self.assertEqual(post[k], v)

    def test_unicode_post(self):
        "Ensure unicode is parsed correctly"
        chinese = frontmatter.load("tests/yaml/chinese.txt", "utf-8")
        output = frontmatter.dumps(chinese)
        zh = "中文"

        self.assertTrue(isinstance(chinese.content, str))

        # check that we're dumping out unicode metadata, too
        self.assertTrue(zh in output)

        # this shouldn't work as ascii, because it's Hanzi
        self.assertRaises(UnicodeEncodeError, chinese.content.encode, "ascii")

    def test_check_no_frontmatter(self):
        "Checks if a file does not have a frontmatter"
        ret = frontmatter.check("tests/empty/no-frontmatter.txt")

        self.assertEqual(ret, False)

    def test_check_empty_frontmatter(self):
        "Checks if a file has a frontmatter (empty or not)"
        ret = frontmatter.check("tests/empty/empty-frontmatter.txt")

        self.assertEqual(ret, True)

    def test_no_frontmatter(self):
        "This is not a zen exercise."
        post = frontmatter.load("tests/empty/no-frontmatter.txt")
        with open("tests/empty/no-frontmatter.txt", "r", encoding="utf-8") as f:
            content = f.read().strip()

        self.assertEqual(post.metadata, {})
        self.assertEqual(post.content, content)

    def test_empty_frontmatter(self):
        "Frontmatter, but no metadata"
        post = frontmatter.load("tests/empty/empty-frontmatter.txt")
        content = "I have frontmatter but no metadata."

        self.assertEqual(post.metadata, {})
        self.assertEqual(post.content, content)

    def test_extra_space(self):
        "Extra space in frontmatter delimiter"
        post = frontmatter.load("tests/yaml/extra-space.txt")
        content = "This file has an extra space on the opening line of the frontmatter."

        self.assertEqual(post.content, content)
        metadata = {"something": "else", "test": "tester"}
        for k, v in metadata.items():
            self.assertEqual(post[k], v)

    def test_to_dict(self):
        "Dump a post as a dict, for serializing"
        post = frontmatter.load("tests/yaml/network-diagrams.md")
        post_dict = post.to_dict()

        for k, v in post.metadata.items():
            self.assertEqual(post_dict[k], v)

        self.assertEqual(post_dict["content"], post.content)

    def test_to_string(self):
        "Calling str(post) returns post.content"
        post = frontmatter.load("tests/yaml/hello-world.txt")

        # test unicode and bytes
        text = "Well, hello there, world."
        self.assertEqual(str(post), text)
        self.assertEqual(bytes(post), text.encode("utf-8"))

    def test_pretty_dumping(self):
        "Use pyaml to dump nicer"
        if pyaml is not None:
            with open("tests/yaml/unpretty.md", "r", encoding="utf-8") as f:
                data = f.read()

            post = frontmatter.load("tests/yaml/unpretty.md")
            yaml = pyaml.dump(post.metadata)

            # the unsafe dumper gives you nicer output, for times you want that
            dump = frontmatter.dumps(post, Dumper=pyaml.PYAMLDumper)

            self.assertTrue(yaml in dump)
            self.assertEqual(dump, data)

    def test_with_crlf_string(self):
        markdown_bytes = b'---\r\ntitle: "my title"\r\ncontent_type: "post"\r\npublished: no\r\n---\r\n\r\nwrite your content in markdown here'
        loaded = frontmatter.loads(markdown_bytes, "utf-8")
        self.assertEqual(loaded["title"], "my title")

    def test_dumping_with_custom_delimiters(self):
        "dump with custom delimiters"
        post = frontmatter.load("tests/yaml/hello-world.txt")
        dump = frontmatter.dumps(post, start_delimiter="+++", end_delimiter="+++")

        self.assertTrue("+++" in dump)

    def test_dump_to_file(self):
        "dump post to filename"
        post = frontmatter.load("tests/yaml/hello-world.txt")

        tempdir = tempfile.mkdtemp()
        filename = os.path.join(tempdir, "hello.md")
        frontmatter.dump(post, filename)

        with open(filename) as f:
            self.assertEqual(f.read(), frontmatter.dumps(post))

        # cleanup
        shutil.rmtree(tempdir)


class HandlerTest(unittest.TestCase):
    """
    Tests for custom handlers and formatting
    """

    TEST_FILES = {
        "tests/yaml/hello-world.txt": YAMLHandler,
        "tests/json/hello-json.md": JSONHandler,
        "tests/toml/hello-toml.md": TOMLHandler,
    }

    def sanity_check(self, filename, handler_type):
        "Ensure we can load -> dump -> load"
        post = frontmatter.load(filename)

        self.assertIsInstance(post.handler, handler_type)

        # dump and reload
        repost = frontmatter.loads(frontmatter.dumps(post))

        self.assertEqual(post.metadata, repost.metadata)
        self.assertEqual(post.content, repost.content)
        self.assertEqual(post.handler, repost.handler)

    def test_detect_format(self):
        "detect format based on default handlers"

        for filename, Handler in self.TEST_FILES.items():
            with open(filename, "r", encoding="utf-8") as f:
                format = frontmatter.detect_format(f.read(), frontmatter.handlers)
                self.assertIsInstance(format, Handler)

    def test_sanity_all(self):
        "Run sanity check on all handlers"
        for filename, Handler in self.TEST_FILES.items():
            self.sanity_check(filename, Handler)

    def test_no_handler(self):
        "default to YAMLHandler when no handler is attached"
        post = frontmatter.load("tests/yaml/hello-world.txt")
        del post.handler

        text = frontmatter.dumps(post)
        self.assertIsInstance(
            frontmatter.detect_format(text, frontmatter.handlers), YAMLHandler
        )

    def test_custom_handler(self):
        "allow caller to specify a custom delimiter/handler"

        # not including this in the regular test directory
        # because it would/should be invalid per the defaults
        custom = textwrap.dedent("""
        ...
        dummy frontmatter
        ...
        dummy content
        """)

        # and a custom handler that really doesn't do anything
        class DummyHandler(object):
            def load(self, fm):
                return {"value": fm}

            def split(self, text):
                return "dummy frontmatter", "dummy content"

        # but we tell frontmatter that it is the appropriate handler
        # for the '...' delimiter
        # frontmatter.handlers['...'] = DummyHandler()
        post = frontmatter.loads(custom, handler=DummyHandler())

        self.assertEqual(post["value"], "dummy frontmatter")

    def test_toml(self):
        "load toml frontmatter"
        if toml is None:
            return
        post = frontmatter.load("tests/toml/hello-toml.md")
        metadata = {"author": "bob", "something": "else", "test": "tester"}
        for k, v in metadata.items():
            self.assertEqual(post[k], v)

    def test_json(self):
        "load raw JSON frontmatter"
        post = frontmatter.load("tests/json/hello-json.md")
        metadata = {"author": "bob", "something": "else", "test": "tester"}
        for k, v in metadata.items():
            self.assertEqual(post[k], v)


class HandlerBaseTest:
    """
    Tests for frontmatter.handlers
    """

    def setUp(self):
        """
        This method should be overridden to initalize the TestCase
        """
        self.handler = None
        self.data = {
            "filename": "tests/yaml/hello-world.txt",
            "content": """\
""",
            "metadata": {},
        }

    def read_from_tests(self):
        with open(self.data["filename"]) as f:
            return f.read()

    def test_external(self):
        filename = self.data["filename"]
        content = self.data["content"]
        metadata = self.data["metadata"]

        post = frontmatter.load(filename)

        self.assertEqual(post.content, content.strip())
        for k, v in metadata.items():
            self.assertEqual(post[k], v)

        # dumps and then loads to ensure round trip conversions.
        posttext = frontmatter.dumps(post, handler=self.handler)
        post_2 = frontmatter.loads(posttext)

        for k in post.metadata:
            self.assertEqual(post.metadata[k], post_2.metadata[k])

        self.assertEqual(post.content, post_2.content)

    def test_detect(self):
        text = self.read_from_tests()

        self.assertTrue(self.handler.detect(text))

    def test_split_content(self):
        text = self.read_from_tests()

        fm, content = self.handler.split(text)

        self.assertEqual(content, self.data["content"])

    def test_split_load(self):
        text = self.read_from_tests()
        fm, content = self.handler.split(text)
        fm_load = self.handler.load(fm)

        # The format of the failmsg makes it easy to copy into the test.
        any_fail = False
        failmsg = "The following metadata did not match the test:"
        for k in self.data["metadata"]:
            if fm_load[k] == self.data["metadata"][k]:
                continue
            any_fail = True
            failmsg += '\n"{0}": {1},'.format(k, repr(fm_load[k]))

        if any_fail:
            self.fail(failmsg)


class YAMLHandlerTest(HandlerBaseTest, unittest.TestCase):
    def setUp(self):
        self.handler = YAMLHandler()
        self.data = {
            "filename": "tests/yaml/hello-markdown.md",
            # TODO: YAMLHandler.split() is prepending '\n' to the content
            "content": """\

Title
=====

title2
------

Hello.

Just need three dashes
---

And this shouldn't break.""",
            "metadata": {"test": "tester", "author": "bob", "something": "else"},
        }


class JSONHandlerTest(HandlerBaseTest, unittest.TestCase):
    def setUp(self):
        self.handler = JSONHandler()
        self.data = {
            "filename": "tests/json/hello-json.md",
            # TODO: JSONHandler.split() is prepending '\n' to the content
            "content": """\


Title
=====

title2
------

Hello.

Just need three dashes
---

And this might break.
""",
            "metadata": {"test": "tester", "author": "bob", "something": "else"},
        }


class TOMLHandlerTest(HandlerBaseTest, unittest.TestCase):
    def setUp(self):
        self.handler = TOMLHandler()
        self.data = {
            "filename": "tests/toml/hello-toml.md",
            # TODO: TOMLHandler.split() is prepending '\n' to the content
            "content": """\

Title
=====

title2
------

Hello.

Just need three dashes
---

And this shouldn't break.
""",
            "metadata": {"test": "tester", "author": "bob", "something": "else"},
        }


CHAPTERS_TEXT = """\
---
author: &author
  name: Bob
  mail: bob@example.com
chapters:
  one: *author
  two: *author
  three: *author
standalone:
  name: Bob
  mail: bob@example.com
---
Chapter body.
"""

CYCLE_TEXT = """\
---
node_a: &a
  name: Bob
  friend: *a
node_b: &b
  name: Carol
  mate: *a
---
Cyclic body.
"""


class AnchorRevisionTest(unittest.TestCase):
    """
    Anchor-aware revisions: local edits detach, shared edits propagate,
    equal-but-unanchored dicts stay independent, and cycles stay finite.
    """

    maxDiff = None

    def test_anchor_map_records_definitions_and_aliases(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        self.assertIsNotNone(post.anchor_map)
        assert post.anchor_map is not None
        self.assertEqual(post.anchor_map.definitions, {"author": ("author",)})
        self.assertEqual(
            post.anchor_map.aliases,
            {
                ("chapters", "one"): "author",
                ("chapters", "two"): "author",
                ("chapters", "three"): "author",
            },
        )

    def test_references_follow_anchor_but_not_equal_dicts(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        self.assertEqual(
            post.references("chapters.three"),
            [
                ("author",),
                ("chapters", "one"),
                ("chapters", "two"),
                ("chapters", "three"),
            ],
        )
        # identical contents, but no anchor: not part of the shared group
        self.assertEqual(post.references("standalone"), [])
        self.assertIsNot(
            post["standalone"], post["author"],
        )
        self.assertEqual(post["standalone"], post["author"])

    def test_local_revision_changes_only_named_chapter(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        result = post.revise(
            "chapters.three.name", "Dana", frontmatter.RevisionScope.LOCAL
        )

        self.assertEqual(result.changed, [("chapters", "three", "name")])
        self.assertTrue(result.detached)
        self.assertEqual(post["chapters"]["three"]["name"], "Dana")
        self.assertEqual(post["author"]["name"], "Bob")
        self.assertEqual(post["chapters"]["one"]["name"], "Bob")
        self.assertEqual(post["chapters"]["two"]["name"], "Bob")
        # the equal-but-unanchored dict is never touched
        self.assertEqual(post["standalone"]["name"], "Bob")

    def test_local_revision_roundtrip_keeps_remaining_shares(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        post.revise("chapters.three.name", "Dana", "local")

        reloaded = frontmatter.loads(frontmatter.dumps(post))
        self.assertEqual(reloaded["chapters"]["three"]["name"], "Dana")
        self.assertEqual(reloaded["chapters"]["one"]["name"], "Bob")
        self.assertEqual(reloaded["standalone"]["name"], "Bob")
        # chapter three left the group; one and two still share the anchor
        self.assertEqual(
            reloaded.references("chapters.one"),
            [("author",), ("chapters", "one"), ("chapters", "two")],
        )
        self.assertEqual(reloaded.references("chapters.three"), [])
        self.assertIs(
            reloaded["chapters"]["one"], reloaded["author"],
        )
        self.assertIsNot(
            reloaded["chapters"]["three"], reloaded["author"],
        )

    def test_shared_revision_changes_every_reference_only(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        result = post.revise(
            "chapters.three.name", "Dana", frontmatter.RevisionScope.SHARED
        )

        self.assertEqual(
            result.changed,
            [
                ("author",),
                ("chapters", "one"),
                ("chapters", "two"),
                ("chapters", "three"),
            ],
        )
        for location in ("author", "one", "two", "three"):
            if location == "author":
                self.assertEqual(post["author"]["name"], "Dana")
            else:
                self.assertEqual(
                    post["chapters"][location]["name"], "Dana"
                )
        # not anchored, so it must not change
        self.assertEqual(post["standalone"]["name"], "Bob")

        reloaded = frontmatter.loads(frontmatter.dumps(post))
        self.assertEqual(reloaded["chapters"]["three"]["name"], "Dana")
        self.assertEqual(reloaded["standalone"]["name"], "Bob")
        # YAML re-emits mapping keys sorted, so compare without ordering
        self.assertCountEqual(
            reloaded.references("chapters.three"),
            [
                ("author",),
                ("chapters", "one"),
                ("chapters", "two"),
                ("chapters", "three"),
            ],
        )

    def test_revising_unanchored_equal_dict_is_local_only(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        result = post.revise(
            "standalone.name", "Erin", frontmatter.RevisionScope.SHARED
        )
        self.assertEqual(result.changed, [("standalone", "name")])
        self.assertEqual(post["standalone"]["name"], "Erin")
        self.assertEqual(post["author"]["name"], "Bob")

    def test_noop_revision_reports_no_changes(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        result = post.revise(
            "chapters.three.name", "Bob", frontmatter.RevisionScope.SHARED
        )
        self.assertEqual(result.changed, [])
        self.assertEqual(post["author"]["name"], "Bob")

    def test_local_revision_replaces_whole_alias_node(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        new_author = {"name": "Dana", "mail": "dana@example.com"}
        result = post.revise("chapters.three", new_author, "local")
        self.assertEqual(result.changed, [("chapters", "three")])
        self.assertEqual(post["chapters"]["three"], new_author)
        self.assertEqual(post["chapters"]["one"]["name"], "Bob")
        self.assertEqual(post.references("chapters.three"), [])

    def test_local_revision_through_definition_moves_anchor(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        result = post.revise("author.name", "Dana", "local")
        self.assertEqual(result.changed, [("author", "name")])
        self.assertTrue(result.detached)
        self.assertEqual(post["author"]["name"], "Dana")
        # the three aliases remain a shared group without the definition
        self.assertEqual(
            post.references("chapters.one"),
            [
                ("chapters", "one"),
                ("chapters", "two"),
                ("chapters", "three"),
            ],
        )
        self.assertEqual(post.references("author"), [])

        reloaded = frontmatter.loads(frontmatter.dumps(post))
        self.assertEqual(reloaded["author"]["name"], "Dana")
        self.assertEqual(reloaded["chapters"]["one"]["name"], "Bob")
        self.assertIs(
            reloaded["chapters"]["one"], reloaded["chapters"]["two"],
        )
        self.assertIsNot(
            reloaded["chapters"]["one"], reloaded["author"],
        )

    def test_result_describe_lists_real_changes_and_sources(self):
        post = frontmatter.loads(CHAPTERS_TEXT)
        result = post.revise("chapters.three.name", "Dana", "local")
        text = result.describe()
        self.assertIn("chapters.three.name", text)
        self.assertIn("alias *author", text)
        self.assertIn("defined at author", text)

    def test_interned_scalars_are_not_treated_as_cycles(self):
        # CPython interns small ints and some strings; sharing by identity
        # must only apply to containers, otherwise these look like a cycle.
        text = (
            "---\n"
            "profile: &p\n  count: 5\n  retries: 5\n"
            "other: *p\n"
            "---\nbody\n"
        )
        post = frontmatter.loads(text)
        sources = post.sources("profile")
        self.assertFalse(
            any(source.cyclic for source in sources),
            [source.describe() for source in sources],
        )
        # the genuine container-level alias is still reported
        self.assertEqual(
            post.references("other"),
            [("profile",), ("other",)],
        )

    def test_cycle_sources_are_finite(self):
        post = frontmatter.loads(CYCLE_TEXT)
        sources = post.sources("node_a")
        descriptions = [source.describe() for source in sources]

        # traversal terminates and names the loop back explicitly
        self.assertTrue(
            any(source.cyclic for source in sources),
            f"expected a cyclic source, got {descriptions}",
        )
        self.assertTrue(
            any("cyclic reference, chain stopped" in text for text in descriptions)
        )
        cyclic = next(source for source in sources if source.cyclic)
        self.assertEqual(cyclic.location, ("node_a", "friend"))
        self.assertEqual(cyclic.anchor, "a")

    def test_revision_inside_cycle_does_not_recurse_forever(self):
        post = frontmatter.loads(CYCLE_TEXT)
        result = post.revise("node_b.mate.name", "Dana", "local")
        self.assertEqual(result.changed, [("node_b", "mate", "name")])
        self.assertTrue(
            any(source.cyclic for source in result.sources)
        )

        reloaded = frontmatter.loads(frontmatter.dumps(post))
        # the original self-loop survives, and node_b now owns a detached copy
        self.assertIs(
            reloaded["node_a"]["friend"], reloaded["node_a"],
        )
        self.assertIsNot(
            reloaded["node_b"]["mate"], reloaded["node_a"],
        )
        self.assertEqual(reloaded["node_a"]["name"], "Bob")
        self.assertEqual(reloaded["node_b"]["mate"]["name"], "Dana")
        self.assertIs(
            reloaded["node_b"]["mate"]["friend"],
            reloaded["node_b"]["mate"],
        )

    def test_sequence_alias_path(self):
        text = (
            "---\n"
            "profile: &p\n  name: Bob\n"
            "chapters:\n  - *p\n  - *p\n"
            "---\nbody\n"
        )
        post = frontmatter.loads(text)
        self.assertEqual(
            post.references("chapters.0"),
            [("profile",), ("chapters", 0), ("chapters", 1)],
        )
        result = post.revise("chapters.1.name", "Dana", "shared")
        self.assertEqual(
            result.changed,
            [("profile",), ("chapters", 0), ("chapters", 1)],
        )
        self.assertEqual(post["chapters"][0]["name"], "Dana")

    def test_json_post_has_no_anchor_map_but_keeps_identity_fallback(self):
        text = (
            "{\n"
            '  "author": {"name": "Bob"},\n'
            '  "body": "x"\n'
            "}\n"
            "content\n"
        )
        post = frontmatter.loads(text)
        self.assertIsNone(post.anchor_map)
        # JSON cannot express anchors, so identity sharing is the fallback
        shared = post["author"]
        post.metadata["mirror"] = shared
        self.assertEqual(
            post.references("mirror"),
            [("author",), ("mirror",)],
        )


if __name__ == "__main__":
    unittest.main()
