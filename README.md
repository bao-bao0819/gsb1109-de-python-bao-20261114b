# Python Frontmatter

[Front matter](https://docs.github.com/en/contributing/writing-for-github-docs/using-yaml-frontmatter) offers a useful way to add arbitrary, structured metadata to text documents, regardless of type.

This is a small package to load and parse files (or just text) with YAML (or JSON, TOML or other) front matter.

[![Tests](https://github.com/eyeseast/python-frontmatter/workflows/Test/badge.svg)](https://github.com/eyeseast/python-frontmatter/actions?query=workflow%3ATest)
[![PyPI](https://img.shields.io/pypi/v/python-frontmatter.svg)](https://pypi.org/project/python-frontmatter/)

**[Documentation](https://python-frontmatter.readthedocs.io/en/latest/)**

## Install:

```shell
# with pip
pip install python-frontmatter

# or uv
uv add python-frontmatter

# local development, with uv
uv sync
```

## Usage:

```python
>>> import frontmatter

```

Load a post from a filename:

```python
>>> post = frontmatter.load('tests/yaml/hello-world.txt')

```

Or a file (or file-like object):

```python
>>> with open('tests/yaml/hello-world.txt') as f:
...     post = frontmatter.load(f)

```

Or load from text:

```python
>>> with open('tests/yaml/hello-world.txt') as f:
...     post = frontmatter.loads(f.read())

```

If the file has a [Byte-Order Mark](https://en.wikipedia.org/wiki/Byte_order_mark) (BOM), strip it off first. An easy way to do this is by using the [`utf-8-sig`](https://docs.python.org/3/library/codecs.html?highlight=utf%208%20sig#module-encodings.utf_8_sig) encoding:

```python
>>> with open('tests/yaml/hello-world.txt', encoding="utf-8-sig") as f:
...     post = frontmatter.load(f)

```

Access content:

```python
>>> print(post.content)
Well, hello there, world.

# this works, too
>>> print(post)
Well, hello there, world.

```

Use metadata (metadata gets proxied as post keys):

```python
>>> print(post['title'])
Hello, world!

```

Metadata is a dictionary, with some handy proxies:

```python
>>> sorted(post.keys())
['layout', 'title']

>>> from pprint import pprint
>>> post['excerpt'] = 'tl;dr'
>>> pprint(post.metadata)
{'excerpt': 'tl;dr', 'layout': 'post', 'title': 'Hello, world!'}

```

If you don't need the whole post object, just parse:

```python
>>> with open('tests/yaml/hello-world.txt') as f:
...     metadata, content = frontmatter.parse(f.read())
>>> print(metadata['title'])
Hello, world!

```

Write back to plain text, too:

```python
>>> print(frontmatter.dumps(post)) # doctest: +NORMALIZE_WHITESPACE
---
excerpt: tl;dr
layout: post
title: Hello, world!
---
Well, hello there, world.

```

Or write to a file (or file-like object):

```python
>>> from io import StringIO
>>> f = StringIO()
>>> frontmatter.dump(post, f)
>>> print(f.getvalue()) # doctest: +NORMALIZE_WHITESPACE
---
excerpt: tl;dr
layout: post
title: Hello, world!
---
Well, hello there, world.

```

For more examples, see files in the `tests/` directory. Each sample file has a corresponding `.result.json` file showing the expected parsed output. See also the `examples/` directory, which covers more ways to customize input and output.

## Migrating YAML and TOML to JSON

If you have a mixed collection of YAML and TOML documents and a JSON-only index,
`frontmatter.migrate` produces a migration artifact: plain JSON metadata plus a
small type table, so values can be restored to their original formats without
guessing.

```python
>>> artifact = frontmatter.migrate('''\
... ---
... published: 2024-05-01
... label: "2024-05-01"
... ratio: 1.20
... reach: 1208925819614629174706176
... empty:
... ---
...
... Body
... ---
... A rule, not a fence.
... ''')
>>> artifact.metadata['published'], artifact.metadata['label']
('2024-05-01', '2024-05-01')
>>> artifact.types
{'/published': 'date', '/ratio': 'float', '/reach': 'bigint'}
>>> type(artifact.decoded_metadata['published'])
<class 'datetime.date'>

```

The artifact separates concerns: `metadata` holds JSON-only values, `types`
holds JSON Pointer paths for values whose Python type JSON cannot express, and
`content` holds the body exactly as it appeared after the closing fence. Because
annotations live outside the metadata object, a document may already contain
front matter fields named `metadata`, `types`, `content`, `version`, `format`,
`encoding`, `delimiters` or `prefix` without a collision.

Explicit `null` values stay in `metadata` as JSON `null`, so they remain
distinct from absent keys. Integers above the JSON-safe range and floats that
JSON would collapse into integers are stored as strings and tagged. Date-like
strings remain strings; only values the parser actually produced as dates are
tagged as dates.

Convert, restore and verify from the command line:

```shell
python -m frontmatter.migration convert old.md -o old.json
python -m frontmatter.migration restore old.json -o old.md
python -m frontmatter.migration verify old.json --against old.md
```

`verify` compares values and types in both the artifact and the restored
document, and checks that the body bytes are unchanged. A YAML or TOML file is
re-emitted with equivalent values and types rather than identical fence
formatting; the body after the closing delimiter is preserved verbatim.

