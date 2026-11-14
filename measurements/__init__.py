"""Build traceable 16-bit measurement word tables from two sparse HEX files.

A measurement device exports the high and low bytes of every 16-bit measured
value into two separate (sparse) Intel HEX images.  Each image has its own
start-address calibration, and some boards additionally swap the two channels
over a configured address range.

This package combines such a pair of images into :class:`WordTable`, an
ordered table of 16-bit samples that keeps the physical origin (source HEX
file, byte address and calibration region) of every byte, distinguishes a
genuinely missing high byte from a measured high byte that merely equals the
fill value, and reports samples whose physical byte is claimed by both source
definitions instead of silently choosing one channel.
"""

from .wordtable import (
    ChannelDefinition,
    DuplicateByteError,
    Sample,
    WordTable,
    build_word_table,
    channel_from_hex_file,
)

__all__ = [
    'ChannelDefinition',
    'DuplicateByteError',
    'Sample',
    'WordTable',
    'build_word_table',
    'channel_from_hex_file',
]
