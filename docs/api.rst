API
====

.. module:: frontmatter

Reading
-------

.. autofunction:: frontmatter.parse

.. autofunction:: frontmatter.check

.. autofunction:: frontmatter.checks

.. autofunction:: frontmatter.load

.. autofunction:: frontmatter.loads


Writing
-------

.. autofunction:: frontmatter.dump

.. autofunction:: frontmatter.dumps


Post objects
------------

.. autoclass:: frontmatter.Post
    :members:
    :special-members: __getitem__, __setitem__, __delitem__


Source locations
----------------

A parsed :class:`~frontmatter.Post` exposes a :class:`~frontmatter.SourceMap`
as ``post.source_map`` (YAML only; other handlers set it to ``None``). Use it
to map a metadata field path back to the raw byte range where the value was
written, including folded multi-line scalars, anchors/aliases and quoted keys
that contain dots, and to reverse-lookup the fields touched by a source line
or byte offset. Offsets stay byte-accurate for documents with a BOM or CRLF
line endings.

.. autoclass:: frontmatter.SourceMap
    :members:

.. autoclass:: frontmatter.Location
    :members:

.. autofunction:: frontmatter.build_source_map


Handlers
--------

.. autoclass:: frontmatter.default_handlers.BaseHandler
    :members: 

.. autoclass:: frontmatter.default_handlers.YAMLHandler

.. autoclass:: frontmatter.default_handlers.JSONHandler

.. autoclass:: frontmatter.default_handlers.TOMLHandler
