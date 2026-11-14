Traceable measurement word table
================================

A measurement device exports the high and low bytes of every 16-bit measured
value into two separate, sparse Intel HEX images.  Each image has its own
start-address calibration, and some boards additionally swap the two channels
over a configured address range.

The ``measurements`` package combines such a pair of images into an ordered
table of 16-bit samples while retaining the physical origin of every byte.

Key types
---------

``CalibrationRegion``
    Maps a physical address range onto a local HEX-image range.  Regions may
    meet exactly on a word boundary; a single word may therefore span two
    regions and each byte keeps its own region provenance.

``ChannelDefinition``
    Binds a sparse ``intelhex.IntelHex`` image to its calibration regions and
    optional swap ranges.  ``channel_from_hex_file`` loads one directly from
    a HEX file.

``Sample``
    One 16-bit word with a ``high`` and ``low`` ``ByteProvenance``.  Each
    provenance records the channel, physical address, local HEX address,
    calibration region, swap flag, presence flag and value.

``WordTable``
    The ordered result.  Use ``words()`` for the assembled values,
    ``to_trace_rows()`` for report/CSV-ready dictionaries, and the
    ``*_missing_samples`` / ``*_fill_measured_samples`` helpers for auditing.

Semantics
---------

* Presence is decided with HEX membership (``addr in hex_data``), never with
  the padding value.  A genuinely missing high byte
  (``high_present is False``, value ``None``) is therefore distinct from a
  measured high byte that happens to equal the fill value (present, value
  ``0xFF``).  Missing bytes contribute ``0`` to the assembled word.
* Only bytes actually exported by a sparse image are ownership claims.  Two
  definitions claiming the same physical byte raise ``DuplicateByteError``
  instead of silently choosing a channel.  The error lists every conflicting
  physical byte with both claims and every affected sample.
* Swap ranges take precedence over the plain calibration and are flagged on
  the resulting provenance (``swapped=True``).

Example
-------

.. code-block:: python

    from measurements import build_word_table, channel_from_hex_file

    low = channel_from_hex_file(
        'low', 'low.hex',
        regions=[(0x00, 0x1000, 8, 'low-cal')],
        swapped_ranges=[(0x04, 0x1020, 2)],
    )
    high = channel_from_hex_file(
        'high', 'high.hex',
        regions=[(0x00, 0x2000, 8, 'high-cal')],
    )

    table = build_word_table(high, low, word_physical_start=0x00,
                             word_count=4)
    for row in table.to_trace_rows():
        print(row)

A runnable end-to-end script that writes real sparse HEX files lives in
``examples/demo_wordtable.py``.  Region tuples are
``(physical_start, local_start, length, name)`` and swap range tuples are
``(physical_start, swapped_local_start, length)``.
