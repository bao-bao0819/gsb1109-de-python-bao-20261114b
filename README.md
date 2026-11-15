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

## Layered metadata inheritance

When content is organized in site, section and article layers, merge their
front matter with `frontmatter.merge_layers`. An `InheritancePolicy` declares
which fields may be explicitly cleared with `null` and which list fields merge
per entry identifier. The returned `Resolution` carries the effective metadata
plus the source layer of every value, and keeps parent-level removals apart
from child-level re-adds.

```python
>>> from frontmatter import InheritancePolicy, Layer, merge_layers
>>> policy = InheritancePolicy(
...     clearable_fields=["summary"],
...     keyed_lists=["references"],  # entries are identified by "id"
... )
>>> resolution = merge_layers([
...     Layer("site", {"layout": "book", "summary": "site",
...                    "references": [{"id": "r1", "title": "One"}]}),
...     Layer("section", {"summary": None,
...                       "references": [{"id": "r1", "_remove": True}]}),
...     Layer("article", {"title": "Ch 1",
...                       "references": [{"id": "r1", "title": "Two"}]}),
... ], policy)
>>> resolution.metadata["layout"]      # missing key inherits the parent
'book'
>>> "summary" in resolution.metadata   # explicit null cleared it
False
>>> resolution.sources["summary"]
FieldSource(layer='section', action='cleared')
>>> resolution.metadata["references"]  # removed by section, re-added by article
[{'id': 'r1', 'title': 'Two'}]
>>> [source.action for source in resolution.list_history["references"]["r1"]]
['set', 'cleared', 'set']

```

`MetadataIndex` holds the whole tree. Revising a section default reports only
the articles that actually inherit that field, so an override or an explicit
clear is never disturbed.

```python
>>> from frontmatter import MetadataIndex
>>> index = MetadataIndex({"layout": "book"}, policy=policy)
>>> _ = index.add_section("math", {"author": "Ada"})
>>> _ = index.add_article("math", "inheriting", {})
>>> _ = index.add_article("math", "overriding", {"author": "Grace"})
>>> affected = index.revise_section("math", {"author": "Katherine"})
>>> sorted(affected)
['inheriting']
>>> index.resolve("math", "overriding").metadata["author"]
'Grace'

```

