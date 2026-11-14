#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import pathlib
import re
import shutil
import tempfile
import textwrap
import unittest

import frontmatter
from frontmatter.default_handlers import (
    YAMLHandler,
    JSONHandler,
    TOMLHandler,
    BaseHandler,
)
from frontmatter.interpretation import (
    AmbiguousFormatError,
    Interpretation,
    Interpretations,
    NoInterpretationError,
)

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


class InterpretationTest(unittest.TestCase):
    """
    Tests for frontmatter.probe / frontmatter.interpret
    """

    maxDiff = None

    def test_probe_reports_handler_interval_and_body(self):
        "Every candidate lists handler, consumed bytes, and exact body start"
        source = b"---\ntitle: x\n---\n\nBody\n"
        report = frontmatter.probe(source)

        self.assertIsInstance(report, Interpretations)
        self.assertEqual(len(report), 1)

        candidate = report[0]
        self.assertIsInstance(candidate, Interpretation)
        self.assertIsInstance(candidate.handler, YAMLHandler)
        self.assertEqual(candidate.metadata, {"title": "x"})
        self.assertEqual(candidate.consumed, (0, 17))
        self.assertEqual(candidate.body_start, 17)
        self.assertEqual(source[candidate.body_start :], candidate.content_bytes)
        self.assertEqual(candidate.content, "\nBody\n")

    def test_probe_preserves_first_blank_line(self):
        "The newline that starts the body is never eaten"
        source = b"---\ntitle: x\n---\n\n\nBody\n"
        candidate = frontmatter.interpret(source)

        self.assertEqual(candidate.content, "\n\nBody\n")
        self.assertEqual(candidate.content_bytes, b"\n\nBody\n")

    def test_probe_preserves_crlf_body(self):
        "CRLF bodies stay byte-for-byte identical to the original"
        source = b"---\r\ntitle: x\r\n---\r\n\r\nBody\r\n"
        candidate = frontmatter.interpret(source)

        self.assertEqual(candidate.content, "\r\nBody\r\n")
        self.assertEqual(candidate.content_bytes, b"\r\nBody\r\n")
        self.assertEqual(source[candidate.body_start :], candidate.content_bytes)

    def test_probe_uses_byte_offsets_with_unicode(self):
        "Offsets count UTF-8 bytes, not characters"
        source = "---\ntitle: 中文\n---\n\nBody\n".encode("utf-8")
        candidate = frontmatter.interpret(source)

        self.assertEqual(candidate.metadata, {"title": "中文"})
        self.assertEqual(
            source[: candidate.body_start].decode(), "---\ntitle: 中文\n---\n"
        )
        self.assertEqual(source[candidate.body_start :], b"\nBody\n")

    def test_probe_does_not_strip_content(self):
        "Unlike parse, probe leaves leading and trailing whitespace alone"
        source = b"---\ntitle: x\n---\n\n  indented body  \n\n"
        candidate = frontmatter.interpret(source)

        self.assertEqual(candidate.content, "\n  indented body  \n\n")

    def test_interpret_key_order_is_equivalent(self):
        "Two handlers agreeing on body and metadata are not ambiguous"
        first = _fixed_handler(YAMLHandler, {"a": 1, "b": 2}, 20)
        second = _fixed_handler(JSONHandler, {"b": 2, "a": 1}, 20)
        text = "x" * 20 + "body"

        report = frontmatter.probe(text, handlers=[first, second])

        self.assertEqual(len(report), 2)
        self.assertFalse(report.requires_selection)
        self.assertEqual(report.choose().body_start, 20)

    def test_interpret_nested_key_order_is_equivalent(self):
        "Key order inside nested metadata is also irrelevant"
        first = _fixed_handler(YAMLHandler, {"outer": {"a": 1, "b": 2}}, 20)
        second = _fixed_handler(JSONHandler, {"outer": {"b": 2, "a": 1}}, 20)
        text = "x" * 20 + "body"

        report = frontmatter.probe(text, handlers=[first, second])

        self.assertFalse(report.requires_selection)

    def test_interpret_different_body_lengths_require_choice(self):
        "Different cut points must surface as an ambiguity"
        first = _fixed_handler(YAMLHandler, {"a": 1}, 10)
        second = _fixed_handler(JSONHandler, {"a": 1}, 20)
        text = "x" * 20 + "body"

        report = frontmatter.probe(text, handlers=[first, second])

        self.assertTrue(report.requires_selection)
        self.assertRaises(
            AmbiguousFormatError, frontmatter.interpret, text, handlers=[first, second]
        )

    def test_interpret_error_lists_candidates(self):
        "The ambiguity error exposes every candidate for a format prompt"
        first = _fixed_handler(YAMLHandler, {"a": 1}, 10)
        second = _fixed_handler(JSONHandler, {"a": 1}, 20)
        text = "x" * 20 + "body"

        with self.assertRaises(AmbiguousFormatError) as caught:
            frontmatter.interpret(text, handlers=[first, second])

        self.assertEqual(len(caught.exception.interpretations), 2)
        self.assertEqual(
            [candidate.body_start for candidate in caught.exception.interpretations],
            [10, 20],
        )

    def test_interpret_can_choose_differing_format(self):
        "Explicit selection returns the body cut belonging to that handler"
        first = _fixed_handler(YAMLHandler, {"a": 1}, 10)
        second = _fixed_handler(JSONHandler, {"a": 2}, 20)
        text = "x" * 20 + "body"

        chosen = frontmatter.interpret(
            text, handler=JSONHandler, handlers=[first, second]
        )

        self.assertIsInstance(chosen.handler, JSONHandler)
        self.assertEqual(chosen.metadata, {"a": 2})
        self.assertEqual(chosen.body_start, 20)
        self.assertEqual(chosen.content, "body")
        self.assertEqual(chosen.to_post().content, "body")

    def test_interpret_metadata_disagreement_requires_choice(self):
        "Same cut point but different metadata still needs a decision"
        first = _fixed_handler(YAMLHandler, {"a": 1}, 20)
        second = _fixed_handler(JSONHandler, {"a": 2}, 20)
        text = "x" * 20 + "body"

        report = frontmatter.probe(text, handlers=[first, second])

        self.assertTrue(report.requires_selection)

    def test_interpret_chooses_equivalent_format_automatically(self):
        "Equivalent candidates return the first one without a prompt"
        first = _fixed_handler(YAMLHandler, {"a": 1}, 20)
        second = _fixed_handler(JSONHandler, {"a": 1}, 20)
        text = "x" * 20 + "body"

        chosen = frontmatter.interpret(text, handlers=[first, second])

        self.assertIsInstance(chosen.handler, YAMLHandler)

    def test_probe_skips_handlers_that_cannot_parse(self):
        "A handler whose load fails is not a candidate"
        text = "x" * 20 + "body"
        broken = _fixed_handler(YAMLHandler, {"a": 1}, 20, fail=True)

        self.assertEqual(frontmatter.probe(text, handlers=[broken]), [])

    def test_probe_requires_literal_body_suffix(self):
        "A handler that invents content is not an explanation"
        text = "x" * 20 + "body"
        invented = _fixed_handler(YAMLHandler, {}, 20, content="something else")

        self.assertEqual(frontmatter.probe(text, handlers=[invented]), [])

    def test_interpret_explicit_handler_without_detect(self):
        "An explicitly supplied handler bypasses detect, like parse does"
        text = "..." + "body"
        handler = _configured_handler(
            BaseHandler, {"a": 1}, 3, detect=lambda text: False
        )

        chosen = frontmatter.interpret(text, handler=handler, handlers=[])

        self.assertEqual(chosen.metadata, {"a": 1})
        self.assertEqual(chosen.content, "body")

    def test_interpret_no_candidates(self):
        "No explanation raises a specific error"
        self.assertRaises(
            NoInterpretationError, frontmatter.interpret, "plain text", handlers=[]
        )

    def test_probe_json_body_is_not_a_second_document(self):
        "A JSON metadata block followed by JSON-looking body stays one post"
        text = '{\n    "title": "x"\n}\n\n{\n    "paragraph": true\n}\n\nMore.\n'

        candidate = frontmatter.interpret(text)

        self.assertIsInstance(candidate.handler, JSONHandler)
        self.assertEqual(candidate.metadata, {"title": "x"})
        self.assertTrue(candidate.content.startswith('\n{\n    "paragraph"'))

    def test_probe_all_fixtures_preserve_bytes(self):
        "Every candidate's content is exactly the original suffix"
        for filename in pathlib.Path("tests").glob("**/*.*"):
            if filename.suffix not in (".md", ".txt"):
                continue
            source = filename.read_bytes()
            for candidate in frontmatter.probe(source):
                with self.subTest(filename=str(filename)):
                    self.assertEqual(
                        source[candidate.body_start :], candidate.content_bytes
                    )
                    self.assertEqual(
                        candidate.content_bytes.decode("utf-8"), candidate.content
                    )
                    self.assertEqual(
                        source[: candidate.body_start], candidate.consumed_bytes
                    )

    def test_probe_custom_handler(self):
        "Custom handlers participate without special casing"
        text = "@@@\ntitle: x\n@@@\n\nBody\n"
        handler = YAMLHandler(
            fm_boundary=re.compile(r"^@@@\s*$", re.MULTILINE),
            start_delimiter="@@@",
            end_delimiter="@@@",
        )

        candidate = frontmatter.interpret(text, handler=handler, handlers=[])

        self.assertEqual(candidate.metadata, {"title": "x"})
        self.assertEqual(candidate.content, "\nBody\n")


def _fixed_text(text, cut):
    return text[:cut], text[cut:]


def _configured_handler(base, metadata, cut, **kwargs):
    "A ``base`` subclass with a fixed split, without inheriting its split code."
    fixed_split = kwargs.get("split", _fixed_text)
    fixed_detect = kwargs.get("detect", lambda text: True)
    fixed_load = kwargs.get("load", lambda fm: metadata)

    class Fixed(base):
        FM_BOUNDARY = re.compile(r"^---$", re.MULTILINE)

        def detect(self, text):
            return fixed_detect(text)

        def split(self, text):
            return fixed_split(text, cut)

        def load(self, fm):
            return fixed_load(fm)

    return Fixed()


def _fixed_handler(base, metadata, cut, **kwargs):
    if "content" in kwargs:
        invented = kwargs.pop("content")

        def content_split(text, cut, invented=invented):
            return text[:cut], invented

        kwargs["split"] = content_split
    if kwargs.pop("fail", False):

        def failing_load(fm):
            raise ValueError("cannot load")

        kwargs["load"] = failing_load
    if "detect" in kwargs:
        detected = kwargs["detect"]
        kwargs["detect"] = lambda text, detected=detected: detected
    return _configured_handler(base, metadata, cut, **kwargs)


if __name__ == "__main__":
    unittest.main()
