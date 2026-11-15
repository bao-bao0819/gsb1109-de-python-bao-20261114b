Explanations
============

How UFO data is read, written and validated
-------------------------------------------

ufoLib2 structures UFO data for easy programmatic access, but the actual data is read,
written and validated by :mod:`fontTools.ufoLib`. This helps keep the base code used by
various UFO libraries in one place.

ufoLib has the concept of lazy loading. Instead of loading everything eagerly up-front,
glyphs, images and data files are loaded into memory as they are accessed. This speeds
up access and saves memory when you only want to access or modify something specific.

You can choose lazy or eager loading at the top level in :meth:`.Font.open`. You can
load everything eagerly after the fact with :meth:`.Font.unlazify` or selectively with
:meth:`.LayerSet.unlazify`, :meth:`.Layer.unlazify`, :meth:`.ImageSet.unlazify` and
:meth:`.DataSet.unlazify`. Copying a font implicitly loads everything eagerly before.

You can choose to validate data during loading and saving at the top level in
:meth:`.Font.open` and :meth:`.Font.save`. This may help if you are working with faulty
data you want to fix programmatically.

Copying objects
---------------

All objects are made to support deep copies with the ``copy`` module from the standard
library. Any lazily loaded data on the way down will be loaded into memory for the
copy::

    import copy
    copy.deepcopy(font)
    copy.deepcopy(font.layers["myLayer"])
    copy.deepcopy(font["glyphName"])
    copy.deepcopy(font["glyphName"].contours[0])
    copy.deepcopy(font["glyphName"].contours[0].points[0])

Since ufoLib2 does not keep track of "parent" objects, the copied objects can be freely
inserted elsewhere::

    font["glyphNameCopy"] = copy.deepcopy(font["glyphName"])

Defcon's API can't be matched exactly
-------------------------------------

ufoLib2 is meant to be a thin wrapper around the UFO data model and intentionally does
not implement some of defcon's properties:

1. ufoLib2 does not keep track of "parents" of objects like defcon does. This makes it
   impossible to implement some methods that implicitly access the parent object, like
   defcon's ``bounds`` property on e.g. Glyph objects, which needs access to the
   parent layer to resolve components. ufoLib2 then implements similar methods that
   ask for a ``layer`` parameter.

2. ufoLib2 does not support notifications, as that concerns only font editing
   applications.

Handling of the ``public.objectLibs`` lib key
---------------------------------------------

ufoLib2 implements handling of ``public.objectLibs`` (see
https://github.com/unified-font-object/ufo-spec/issues/115), but instead of
attaching libs directly to the objects where they belong, they stay in a font's
and glyph's lib and have to be retrieved with :meth:`.Font.objectLib` and
:meth:`.Glyph.objectLib`, respectively.

Using a higher-up lib means objects can get libs on UFO v3 without format
changes, but complicates adding libs to the objects directly. Since ufoLib2
intentionally lacks defcon's parent-child object hierarchy, a guideline can't
follow a link to the containing parent and access its lib. Magically loading
and storing the ``public.objectLibs`` lib key on loading and saving a UFO adds
an uncomfortable amount of magic and edge-cases. What if a client itself
adds a ``public.objectLibs`` entry for an object that differs from what is in
that object's lib? Overwrite, ignore, error out? With the ``.objectLib``
method, this edge case does not exist.

Analyzing rounding errors when changing units-per-em
----------------------------------------------------

When coordinates are moved from one units-per-em (UPM) value to another and the
result is stored with integer coordinates, a point in a plain glyph is
quantized only once and typically stays well within tolerance. Points reached
through components behave differently: the component's affine transformation
(scale, rotation, reflection, ...) is applied to coordinates that were already
snapped to integers in the base glyph, so a sub-unit rounding error is carried
into every composite that uses the component and is amplified when the
component is enlarged.

The :mod:`ufoLib2.rounding` module compares two integer-coordinate pipelines,
both evaluated in the final composite glyph coordinate system of the target
UPM:

* **direct rounding** rounds the exact transformed point once, directly in the
  final coordinate system;
* **local-first rounding** rounds at every nesting level, applies the component
  transform and rounds again, i.e. it snaps each glyph locally before reusing it
  as a component.

The difference between the two pipelines is reported per point together with
the component chain that placed it, so the original source glyph stays
traceable through nested, rotated or reflected components. Acceptance is
reported per glyph instance rather than once for the base glyph::

    from ufoLib2 import Font
    from ufoLib2.rounding import export_rounding_report

    font = Font.open("MyFont.ufo")
    report = export_rounding_report(
        font,
        target_upm=2048,            # source UPM comes from font.info.unitsPerEm
        tolerance=1.0,              # max accepted point offset, target UPM units
        csv_path="rounding-errors.csv",
        json_path="rounding-errors.json",
        failing_only=True,          # CSV keeps only the glyphs that fail
    )

    for glyph_report in report.failingGlyphs:
        worst = glyph_report.worstPoint
        print(
            glyph_report.glyphName,
            glyph_report.maxPointOffset,
            worst.componentChain,    # e.g. ("agrave", "a")
        )

:func:`~ufoLib2.rounding.analyze_layer_rounding` works directly on a
:class:`.Layer` when the source UPM is not stored in the font info, and
:class:`~ufoLib2.rounding.LayerRoundingReport` exposes the same data
programmatically. A component graph that references itself raises
:class:`.CircularReferenceError`; a component that points at a missing glyph
raises ``KeyError``.
