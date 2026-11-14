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


Migrating
---------

.. autofunction:: frontmatter.migrate

.. autofunction:: frontmatter.migrate_file

.. autofunction:: frontmatter.restore

.. autofunction:: frontmatter.restore_file

.. autofunction:: frontmatter.verify

.. autofunction:: frontmatter.verify_file

.. autofunction:: frontmatter.artifact_dumps

.. autofunction:: frontmatter.artifact_loads

.. autoclass:: frontmatter.MigrationArtifact
    :members:

.. autofunction:: frontmatter.encode_metadata

.. autofunction:: frontmatter.decode_metadata


Post objects
------------

.. autoclass:: frontmatter.Post
    :members:
    :special-members: __getitem__, __setitem__, __delitem__


Handlers
--------

.. autoclass:: frontmatter.default_handlers.BaseHandler
    :members: 

.. autoclass:: frontmatter.default_handlers.YAMLHandler

.. autoclass:: frontmatter.default_handlers.JSONHandler

.. autoclass:: frontmatter.default_handlers.TOMLHandler
