API Reference
=============

.. |defcon_compat| replace:: For defcon API compatibility only.

Core Objects
------------

Font
^^^^

.. autoclass:: ufoLib2.objects.Font
   :members:
   :undoc-members:

Info
^^^^

.. autoclass:: ufoLib2.objects.Info
   :members:
   :undoc-members:

Features
^^^^^^^^

.. autoclass:: ufoLib2.objects.features.Features
   :members:
   :undoc-members:

DataSet
^^^^^^^

.. autoclass:: ufoLib2.objects.dataSet.DataSet
   :inherited-members:
   :undoc-members:
   :exclude-members: list_contents, read_data, write_data, remove_data

LayerSet
^^^^^^^^

.. autoclass:: ufoLib2.objects.LayerSet
   :members:
   :undoc-members:
   :exclude-members: loadLayer

Layer
^^^^^

.. autoclass:: ufoLib2.objects.Layer
   :members:
   :undoc-members:

Archive
^^^^^^^

.. autofunction:: ufoLib2.archive.build_archive

.. autoclass:: ufoLib2.archive.Archive
   :members:
   :undoc-members:

.. autoclass:: ufoLib2.archive.LayerArchive
   :members:
   :undoc-members:

.. autoclass:: ufoLib2.archive.ArchiveRequest
   :members:
   :undoc-members:

.. autoclass:: ufoLib2.archive.Cycle
   :members:
   :undoc-members:

.. autoclass:: ufoLib2.archive.ArchiveIssue
   :members:
   :undoc-members:

.. autoclass:: ufoLib2.archive.Substitution
   :members:
   :undoc-members:

.. autoclass:: ufoLib2.archive.Character
   :members:
   :undoc-members:

.. autoclass:: ufoLib2.archive.GlyphName
   :members:
   :undoc-members:

.. autoclass:: ufoLib2.archive.GlyphSource
   :members:
   :undoc-members:

Glyph
^^^^^

.. autoclass:: ufoLib2.objects.Glyph
   :members:
   :undoc-members:

Component
^^^^^^^^^

.. autoclass:: ufoLib2.objects.Component
   :members:
   :undoc-members:

Contour
^^^^^^^

.. autoclass:: ufoLib2.objects.Contour
   :members:
   :undoc-members:

Point
^^^^^

.. autoclass:: ufoLib2.objects.Point
   :members:
   :undoc-members:

Anchor
^^^^^^

.. autoclass:: ufoLib2.objects.Anchor
   :members:
   :undoc-members:

Guideline
^^^^^^^^^

.. autoclass:: ufoLib2.objects.Guideline
   :members:
   :undoc-members:

ImageSet
^^^^^^^^

.. autoclass:: ufoLib2.objects.imageSet.ImageSet
   :inherited-members:
   :undoc-members:
   :exclude-members: list_contents, read_data, write_data, remove_data

Image
^^^^^

.. autoclass:: ufoLib2.objects.Image
   :members:
   :undoc-members:

Miscellaneous objects
^^^^^^^^^^^^^^^^^^^^^

.. autoclass:: ufoLib2.objects.misc.BoundingBox
   :members:
   :undoc-members:

Types
-----

.. automodule:: ufoLib2.typing
   :members:
   :undoc-members:

Pens
----

.. automodule:: ufoLib2.pointPens.glyphPointPen
   :members:
   :undoc-members:

Constants
---------

.. automodule:: ufoLib2.constants
   :members:
   :undoc-members:

Exceptions
----------

.. automodule:: ufoLib2.errors
   :members:
   :undoc-members:
