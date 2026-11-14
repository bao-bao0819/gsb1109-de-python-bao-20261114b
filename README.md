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

## Checking every possible reading

`load` and `parse` stop at the first handler that matches. When a document's body begins with something that also looks like metadata (a JSON paragraph, say), several handlers may be able to explain its beginning. `frontmatter.probe` lists all of them:

```python
>>> with open('tests/json/hello-json.md', 'rb') as f:
...     report = frontmatter.probe(f.read())
>>> [(type(c.handler).__name__, c.consumed, c.body_start) for c in report]
[('JSONHandler', (0, 71), 71)]
>>> report[0].content.startswith('\nTitle')
True

```

`consumed` is the byte interval `(start, end)` the handler explains as metadata, and `body_start` is the byte offset where that handler says the body begins. `content` and `content_bytes` are the exact original slice after `body_start` — no whitespace is stripped, so the first blank line stays in the body.

`frontmatter.interpret` returns a single reading, or raises `AmbiguousFormatError` when candidates cut the body at different offsets or disagree about metadata. Candidates that differ only in metadata key order are treated as equivalent. To resolve an ambiguity, pass `handler=` (or `index=`) explicitly, or ask the user which format to use from the error's `.interpretations` list:

```python
>>> from frontmatter import AmbiguousFormatError
>>> with open('tests/json/hello-json.md', 'rb') as f:
...     reading = frontmatter.interpret(f.read())
>>> type(reading.handler).__name__
'JSONHandler'
>>> reading.content.startswith('\nTitle')
True

```
