#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from pathlib import Path

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


class MigrationTest(unittest.TestCase):
    """
    Tests for lossless YAML/TOML to JSON migration artifacts
    """

    maxDiff = None

    def test_date_and_date_like_string(self):
        "A parsed date and a quoted date-like string stay different"
        text = '---\npublished: 2024-05-01\nlabel: "2024-05-01"\n---\n\nBody\n'
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.metadata["published"], "2024-05-01")
        self.assertEqual(artifact.metadata["label"], "2024-05-01")
        self.assertEqual(artifact.types, {"/published": "date"})

        metadata = artifact.decoded_metadata
        self.assertEqual(metadata["published"], datetime.date(2024, 5, 1))
        self.assertEqual(metadata["label"], "2024-05-01")

        restored = frontmatter.restore(artifact)
        reparsed = frontmatter.parse(restored)[0]
        self.assertIsInstance(reparsed["published"], datetime.date)
        self.assertIsInstance(reparsed["label"], str)

    def test_decimal_and_big_integer(self):
        "Floats and out-of-range integers keep their type"
        text = "---\nratio: 1.20\nviews: 42\nreach: 1208925819614629174706176\n---\n"
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.metadata["ratio"], 1.2)
        self.assertEqual(artifact.metadata["reach"], "1208925819614629174706176")
        self.assertEqual(
            artifact.types, {"/ratio": "float", "/reach": "bigint"}
        )

        metadata = artifact.decoded_metadata
        self.assertIsInstance(metadata["ratio"], float)
        self.assertIsInstance(metadata["views"], int)
        self.assertEqual(metadata["reach"], 2**80)

        restored = frontmatter.restore(artifact)
        reparsed = frontmatter.parse(restored)[0]
        self.assertIsInstance(reparsed["ratio"], float)
        self.assertIsInstance(reparsed["views"], int)
        self.assertEqual(reparsed["reach"], 2**80)

    def test_explicit_null_and_missing_field(self):
        "An empty value stays present and null; a missing key stays missing"
        text = "---\nempty:\nexplicit: null\npresent: value\n---\n"
        artifact = frontmatter.migrate(text)

        self.assertIn("empty", artifact.metadata)
        self.assertIsNone(artifact.metadata["empty"])
        self.assertIsNone(artifact.metadata["explicit"])
        self.assertNotIn("missing", artifact.metadata)

        metadata = artifact.decoded_metadata
        self.assertIn("empty", metadata)
        self.assertIsNone(metadata["empty"])
        self.assertNotIn("missing", metadata)

        restored = frontmatter.restore(artifact)
        reparsed = frontmatter.parse(restored)[0]
        self.assertIn("empty", reparsed)
        self.assertIsNone(reparsed["empty"])
        self.assertNotIn("missing", reparsed)

    def test_reserved_names_in_source_do_not_collide(self):
        "Source fields named like artifact keys restore verbatim"
        text = textwrap.dedent(
            """\
            ---
            version: 7
            format: weird
            encoding: declared
            delimiters: custom
            metadata: a field
            types: another field
            content: body field
            prefix: leading field
            ---

            Body
            ---
            A horizontal rule, not a fence.
            """
        )
        artifact = frontmatter.migrate(text)
        reserved = artifact.reserved_fields

        self.assertEqual(len(reserved), 8)
        for name in reserved:
            self.assertIn(name, artifact.metadata)

        # The artifact's own keys are outside its metadata object.
        document = artifact.to_dict()
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["format"], "yaml")
        self.assertIsInstance(document["metadata"], dict)
        self.assertIsInstance(document["types"], dict)

        metadata = artifact.decoded_metadata
        self.assertEqual(metadata["version"], 7)
        self.assertEqual(metadata["format"], "weird")
        self.assertEqual(metadata["metadata"], "a field")
        self.assertEqual(metadata["types"], "another field")
        self.assertEqual(metadata["content"], "body field")

        restored = frontmatter.restore(artifact)
        self.assertIn("A horizontal rule, not a fence.", frontmatter.parse(restored)[1])

    def test_body_bytes_survive(self):
        "Separator lines, CRLF endings and trailing whitespace are untouched"
        text = textwrap.dedent(
            """\
            ---
            title: A post
            ---


            Intro
            ---
            Still content.
            --
            The end.
            """
        ).replace("\n", "\r\n")
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.newline, "\r\n")
        separator = "title: A post\r\n---"
        original_body = text[text.index(separator) + len(separator) :]
        self.assertEqual(artifact.content, original_body)

        restored = frontmatter.restore(artifact)
        restored_body = restored[restored.index(separator) + len(separator) :]
        self.assertEqual(original_body, restored_body)
        self.assertIn("\r\n---\r\n", original_body)

    def test_leading_whitespace_is_preserved(self):
        "Text before the opening fence is kept as a prefix"
        text = "\n\n---\ntitle: A post\n---\nBody\n"
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.prefix, "\n\n")
        self.assertEqual(frontmatter.restore(artifact), text)

    def test_json_source_round_trip(self):
        "JSON front matter becomes an artifact and comes back as JSON"
        text = '{\n    "when": "2024-05-01",\n    "count": 3\n}\n\nBody\n'
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.format, "json")
        self.assertEqual(artifact.metadata, {"when": "2024-05-01", "count": 3})
        self.assertEqual(artifact.types, {})

        restored = frontmatter.restore(artifact)
        reparsed = frontmatter.parse(restored, handler=JSONHandler())[1].strip()
        self.assertEqual(reparsed, "Body")

    def test_fixture_files_round_trip(self):
        "Every fixture converts, verifies and keeps its body intact"
        documents = sorted(Path("tests").glob("**/*.md")) + sorted(
            Path("tests").glob("**/*.txt")
        )
        self.assertTrue(documents)
        for path in documents:
            if "migration" in path.parts:
                continue
            with self.subTest(path=str(path)):
                data = path.read_bytes()
                artifact = frontmatter.migrate(data)
                frontmatter.verify_file(artifact, path)
                restored = frontmatter.restore(artifact).encode("utf-8")
                original = frontmatter.parse(data.decode("utf-8"))
                recreated = frontmatter.parse(restored.decode("utf-8"))
                self.assertEqual(original[1], recreated[1])

    def test_migration_fixtures_round_trip(self):
        "Migration fixtures keep values, types and body bytes"
        paths = [
            Path("tests/migration/types-and-collisions.yaml"),
            Path("tests/migration/typed-toml.toml"),
        ]
        for path in paths:
            with self.subTest(path=str(path)):
                data = path.read_bytes()
                artifact = frontmatter.migrate(data)
                frontmatter.verify_file(artifact, path)
                restored = frontmatter.restore(artifact).encode("utf-8")
                original = frontmatter.parse(data.decode("utf-8"))
                recreated = frontmatter.parse(restored.decode("utf-8"))
                self.assertEqual(original[1], recreated[1])

    def test_cli_convert_restore_verify(self):
        "The command line converts, restores and verifies a file"
        source = Path("tests/migration/types-and-collisions.yaml")
        tempdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tempdir)
        artifact_file = tempdir / "artifact.json"
        restored_file = tempdir / "restored.yaml"

        convert = [
            sys.executable,
            "-m",
            "frontmatter.migration",
            "convert",
            str(source),
            "-o",
            str(artifact_file),
        ]
        self.assertEqual(subprocess.run(convert).returncode, 0)
        self.assertTrue(artifact_file.exists())

        document = json.loads(artifact_file.read_text())
        self.assertIn("metadata", document)
        self.assertIn("types", document)
        self.assertIn("/published", document["types"])

        restore = [
            sys.executable,
            "-m",
            "frontmatter.migration",
            "restore",
            str(artifact_file),
            "-o",
            str(restored_file),
        ]
        self.assertEqual(subprocess.run(restore).returncode, 0)
        self.assertTrue(restored_file.exists())
        self.assertEqual(
            frontmatter.parse(source.read_text())[1],
            frontmatter.parse(restored_file.read_text())[1],
        )

        verify = [
            sys.executable,
            "-m",
            "frontmatter.migration",
            "verify",
            str(artifact_file),
            "--against",
            str(source),
        ]
        self.assertEqual(subprocess.run(verify, capture_output=True).returncode, 0)

    def test_json_braces_inside_strings(self):
        "The JSON object extent is found with a real decoder, not a line scan"
        text = textwrap.dedent(
            """\
            {
              "title": "a } brace",
              "nested": {"a": [1, {"b": 2}]}
            }

            Body with } and { characters.
            """
        )
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.format, "json")
        self.assertEqual(artifact.metadata["title"], "a } brace")
        self.assertEqual(artifact.metadata["nested"], {"a": [1, {"b": 2}]})
        frontmatter.verify(artifact, text)
        self.assertIn("Body with } and { characters.", artifact.content)

    def test_bytes_encoding_round_trip(self):
        "The source encoding is recorded and reused when restoring"
        text = "---\ntitle: café\n---\nCafé body\n".encode("latin-1")
        artifact = frontmatter.migrate(text, "latin-1")

        self.assertEqual(artifact.encoding, "latin-1")
        self.assertEqual(artifact.metadata["title"], "café")
        self.assertEqual(
            frontmatter.restore(artifact).encode("latin-1"), text
        )

    def test_yaml_set_and_binary_values(self):
        "Set and byte-string values survive as their original types"
        text = '---\ns: !!set {a, b}\nb: !!binary "YWJj"\n---\n'
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.types, {"/s": "set", "/b": "bytes"})
        metadata = artifact.decoded_metadata
        self.assertEqual(metadata["s"], {"a", "b"})
        self.assertEqual(metadata["b"], b"abc")

        frontmatter.verify(artifact, text)

    def test_nested_toml_datetime_and_time(self):
        "Nested TOML temporal types keep timezone and clock values"
        if TOMLHandler is None:
            return
        text = (
            "+++\n"
            "[d]\n"
            "when = 2024-05-01T12:34:56Z\n"
            "clock = 12:34:56\n"
            "+++\n"
        )
        artifact = frontmatter.migrate(text)

        self.assertEqual(
            artifact.types,
            {"/d/when": "datetime", "/d/clock": "time"},
        )
        metadata = artifact.decoded_metadata
        self.assertEqual(
            metadata["d"]["when"],
            datetime.datetime(
                2024, 5, 1, 12, 34, 56, tzinfo=datetime.timezone.utc
            ),
        )
        self.assertEqual(metadata["d"]["clock"], datetime.time(12, 34, 56))
        frontmatter.verify(artifact, text)

    def test_non_string_metadata_keys_are_rejected(self):
        "YAML permits non-string keys, but JSON objects do not"
        with self.assertRaises(frontmatter.UnsupportedTypeError):
            frontmatter.migrate("---\n1: one\n---\n")

    def test_malformed_artifact_is_rejected(self):
        "Missing or mistyped artifact fields fail loudly"
        with self.assertRaises(frontmatter.MigrationError):
            frontmatter.artifact_loads('{"metadata": []}')
        with self.assertRaises(frontmatter.MigrationError):
            frontmatter.artifact_loads(
                '{"format": "yaml", "metadata": {}, "types": {"/x": 1}}'
            )
        with self.assertRaises(frontmatter.MigrationError):
            frontmatter.artifact_loads(
                '{"format": "yaml", "metadata": {}, "content": 1}'
            )

    def test_type_notes_must_point_at_real_values(self):
        "A stale type entry is an error instead of silently ignored"
        artifact = frontmatter.MigrationArtifact(
            metadata={"a": 1}, types={"/gone": "date"}
        )
        with self.assertRaises(frontmatter.SerializationError):
            artifact.decoded_metadata

    def test_pointer_escaping_for_unusual_keys(self):
        "Keys containing slashes and tildes are escaped per JSON Pointer"
        text = textwrap.dedent(
            """\
            ---
            "a/b~c": 2024-05-01
            nested:
              "x/y~z": 1.5
            ---
            """
        )
        artifact = frontmatter.migrate(text)

        self.assertEqual(
            artifact.types,
            {"/a~1b~0c": "date", "/nested/x~1y~0z": "float"},
        )
        metadata = artifact.decoded_metadata
        self.assertIsInstance(metadata["a/b~c"], datetime.date)
        self.assertIsInstance(metadata["nested"]["x/y~z"], float)

    def test_nan_and_infinity_are_tagged(self):
        "Non-finite floats are strings in JSON and restored as floats"
        text = "---\nf: .nan\ng: .inf\n---\n"
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.metadata, {"f": "NaN", "g": "Infinity"})
        self.assertEqual(artifact.types, {"/f": "float", "/g": "float"})
        # Strict JSON: allow_nan=False must not raise.
        self.assertEqual(
            json.dumps(artifact.to_dict(), allow_nan=False),
            json.dumps(artifact.to_dict(), allow_nan=False),
        )

        metadata = artifact.decoded_metadata
        self.assertTrue(math.isnan(metadata["f"]))
        self.assertTrue(math.isinf(metadata["g"]))
        frontmatter.verify(artifact, text)

    def test_custom_handler_with_distinct_end_delimiter(self):
        "Handlers with literal start/end delimiters work, reverse layouts do not"
        class PandocHandler(YAMLHandler):
            START_DELIMITER = "---"
            END_DELIMITER = "..."
            FM_BOUNDARY = re.compile(r"^(?:---|\.\.\.)\s*$", re.MULTILINE)

            def split(self, text):
                _, start, text = text.partition(self.START_DELIMITER)
                fm, end, content = text.partition(self.END_DELIMITER)
                return fm, content

        text = (
            "---\n"
            "title: A post\n"
            "...\n\n"
            "Body\n"
            "---\n"
            "Actually a rule.\n"
        )
        artifact = frontmatter.migrate(text, handler=PandocHandler())

        self.assertEqual(artifact.format, "yaml")
        self.assertEqual(artifact.start_delimiter, "---")
        self.assertEqual(artifact.end_delimiter, "...")
        self.assertEqual(artifact.metadata, {"title": "A post"})
        frontmatter.verify(artifact, text, handler=PandocHandler())

        class ReversedHandler(YAMLHandler):
            FM_BOUNDARY = re.compile(r"^-{3,}\s*$", re.MULTILINE)

            def split(self, text):
                content, fm, _ = text.rsplit(self.FM_BOUNDARY, 2)
                return fm, content

        reversed_text = "Body first\n---\ntitle: A post\n---\n"
        with self.assertRaises(frontmatter.MigrationError):
            frontmatter.migrate(reversed_text, handler=ReversedHandler())

    def test_toml_source_round_trip(self):
        "TOML dates, floats and large integers keep their Python types"
        if TOMLHandler is None:
            return
        text = (
            "+++\n"
            "published = 2024-05-01\n"
            "label = \"2024-05-01\"\n"
            "ratio = 1.20\n"
            "reach = 1208925819614629174706176\n"
            "+++\n\nBody\n"
        )
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.format, "toml")
        self.assertEqual(
            artifact.types,
            {"/published": "date", "/ratio": "float", "/reach": "bigint"},
        )
        metadata = artifact.decoded_metadata
        self.assertIsInstance(metadata["published"], datetime.date)
        self.assertIsInstance(metadata["ratio"], float)
        self.assertEqual(metadata["reach"], 2**80)

        restored = frontmatter.restore(artifact)
        self.assertEqual(artifact.content, "\n\nBody\n")
        self.assertIn("Body", restored)

    def test_toml_null_is_reported(self):
        "TOML cannot express null, so it is refused rather than dropped"
        if TOMLHandler is None:
            return
        artifact = frontmatter.migrate("+++\nempty = \"\"\n+++\n")
        artifact.metadata = {"empty": None}
        artifact.types = {}

        with self.assertRaises(frontmatter.UnsupportedTypeError):
            frontmatter.restore(artifact)

    def test_artifact_json_has_no_type_notes_for_plain_values(self):
        "Ordinary JSON metadata needs no type table"
        text = textwrap.dedent(
            """\
            ---
            title: Hello
            count: 3
            draft: false
            tags: [one, two]
            nothing: null
            ---
            """
        )
        artifact = frontmatter.migrate(text)

        self.assertEqual(artifact.types, {})
        self.assertEqual(
            json.loads(frontmatter.artifact_dumps(artifact)),
            {
                "version": 1,
                "format": "yaml",
                "encoding": "utf-8",
                "delimiters": {"start": "---", "end": "---", "newline": "\n"},
                "metadata": {
                    "title": "Hello",
                    "count": 3,
                    "draft": False,
                    "tags": ["one", "two"],
                    "nothing": None,
                },
                "types": {},
                "content": "\n",
                "prefix": "",
            },
        )

    def test_verify_reports_value_and_type_differences(self):
        "Verification catches type drift, not just text drift"
        text = "---\npublished: 2024-05-01\n---\n"
        artifact = frontmatter.migrate(text)
        frontmatter.verify(artifact, text)

        artifact.types = {}
        with self.assertRaises(frontmatter.VerificationError):
            frontmatter.verify(artifact, text)

        artifact.types = {"/published": "date"}
        artifact.metadata = {"published": "2024-05-02"}
        with self.assertRaises(frontmatter.VerificationError):
            frontmatter.verify(artifact, text)

    def test_dates_strings_and_numbers_in_nested_metadata(self):
        "Type notes follow JSON Pointer paths through nested structures"
        text = textwrap.dedent(
            """\
            ---
            timeline:
              start: 2024-05-01
              label: "2024-05-01"
            stats: [1.5, 2, 1208925819614629174706176]
            ---
            """
        )
        artifact = frontmatter.migrate(text)

        self.assertEqual(
            artifact.types,
            {
                "/timeline/start": "date",
                "/stats/0": "float",
                "/stats/2": "bigint",
            },
        )
        metadata = artifact.decoded_metadata
        self.assertIsInstance(metadata["timeline"]["start"], datetime.date)
        self.assertEqual(metadata["timeline"]["label"], "2024-05-01")
        self.assertEqual(metadata["stats"], [1.5, 2, 2**80])


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


if __name__ == "__main__":
    unittest.main()
