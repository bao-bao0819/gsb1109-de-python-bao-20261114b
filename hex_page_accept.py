"""Page-wise acceptance comparison of two Intel HEX deliveries.

Two HEX files that describe the same memory image may differ in pure
text layout: records may be reordered and each data record may carry a
different byte count.  This module compares the *address content* of
two deliveries over fixed-size pages, so such layout noise is ignored,
while a changed start-address (entry point) declaration is still
detected and reported separately.

Holes (addresses with no data record) compare equal to explicit bytes
that match the pad value (0xFF by default), so a page explicitly
filled with 0xFF accepts a delivery where that page is simply absent.

The report distinguishes:

* ``identical``       - same content, same entry point;
* ``entry-only``      - only the entry point declaration differs;
* ``data-only``       - only page content differs;
* ``entry-and-data``  - both differ.

When the entry point differs, ``entry_bytes_changed`` tells whether the
memory content at the entry address (of either delivery) also changed
between the two files -- i.e. the change is not just a moved pointer.
"""

import argparse
import sys

import intelhex


def _entry_point(ih):
    """Normalize the start-address declaration to (kind, linear address).

    Returns None when the file declares no entry point.  The kind is
    kept so that a segment declaration and a linear declaration are not
    silently treated as the same record.
    """
    sa = ih.start_addr
    if not sa:
        return None
    if 'EIP' in sa:
        return ('EIP', sa['EIP'])
    return ('CS:IP', (sa['CS'] << 4) + sa['IP'])


def _format_entry(entry, start_addr):
    if entry is None:
        return '<none>'
    kind, linear = entry
    if kind == 'EIP':
        return 'EIP=0x%08X' % linear
    return 'CS:IP=0x%04X:0x%04X (linear 0x%05X)' % (
        start_addr['CS'], start_addr['IP'], linear)


def _page_bytes(ih, start, size, pad):
    """Return ``size`` bytes starting at ``start``, holes filled with pad."""
    old_padding = ih.padding
    ih.padding = pad
    try:
        return ih.tobinarray(start=start, size=size)
    finally:
        ih.padding = old_padding


class AcceptanceResult(object):
    """Outcome of a page-wise acceptance comparison."""

    def __init__(self, page_size, pad, pages, diff_pages,
                 entry1, entry2, start_addr1, start_addr2,
                 entry_bytes_changed):
        self.page_size = page_size
        self.pad = pad
        self.pages = pages                # total number of pages compared
        self.diff_pages = diff_pages      # list of per-page diff dicts
        self.entry1 = entry1              # normalized entry of file 1
        self.entry2 = entry2              # normalized entry of file 2
        self.start_addr1 = start_addr1    # raw start_addr dict of file 1
        self.start_addr2 = start_addr2    # raw start_addr dict of file 2
        self.entry_bytes_changed = entry_bytes_changed

    @property
    def data_equal(self):
        return not self.diff_pages

    @property
    def entry_equal(self):
        return self.entry1 == self.entry2

    @property
    def verdict(self):
        if self.data_equal and self.entry_equal:
            return 'identical'
        if self.data_equal:
            return 'entry-only'
        if self.entry_equal:
            return 'data-only'
        return 'entry-and-data'

    @property
    def ok(self):
        return self.verdict == 'identical'

    def format(self, name1='file1', name2='file2'):
        lines = [
            'page-wise acceptance report',
            '  file1: %s' % name1,
            '  file2: %s' % name2,
            '  page size: %d bytes, pad byte: 0x%02X' % (
                self.page_size, self.pad),
            '  pages compared: %d' % self.pages,
        ]
        if self.data_equal:
            lines.append('  data content: EQUAL')
        else:
            lines.append('  data content: DIFFERENT')
            for d in self.diff_pages:
                lines.append(
                    '    page %d (0x%0*X): %d byte(s) differ, '
                    'first at offset +0x%02X' % (
                        d['index'], len('%X' % (self.page_size - 1)) + 2,
                        d['start'], d['diff_count'], d['first_offset']))
        if self.entry_equal:
            lines.append('  entry point: EQUAL (%s)' % _format_entry(
                self.entry1, self.start_addr1))
        else:
            lines.append('  entry point: DIFFERENT')
            lines.append('    file1: %s' % _format_entry(
                self.entry1, self.start_addr1))
            lines.append('    file2: %s' % _format_entry(
                self.entry2, self.start_addr2))
            if self.entry_bytes_changed is not None:
                lines.append('    entry-pointed bytes: %s' % (
                    'CHANGED' if self.entry_bytes_changed else 'unchanged'))
        lines.append('  verdict: %s' % self.verdict)
        return '\n'.join(lines)


def compare(ih1, ih2, page_size=256, pad=0xFF):
    """Compare two IntelHex objects page by page.

    @param ih1        first IntelHex object
    @param ih2        second IntelHex object
    @param page_size  fixed page length in bytes
    @param pad        value a hole compares equal to (0xFF = erased flash)
    @return           AcceptanceResult
    """
    if page_size <= 0:
        raise ValueError('page_size must be positive')
    if not 0 <= pad <= 0xFF:
        raise ValueError('pad must be a byte value')

    lo = hi = None
    for ih in (ih1, ih2):
        if len(ih):
            first, last = ih.minaddr(), ih.maxaddr()
            lo = first if lo is None else min(lo, first)
            hi = last if hi is None else max(hi, last)

    pages = 0
    diff_pages = []
    if lo is not None:
        first_index = lo // page_size
        last_index = hi // page_size
        for index in range(first_index, last_index + 1):
            start = index * page_size
            data1 = _page_bytes(ih1, start, page_size, pad)
            data2 = _page_bytes(ih2, start, page_size, pad)
            pages += 1
            if data1 != data2:
                offsets = [i for i in range(page_size)
                           if data1[i] != data2[i]]
                diff_pages.append({
                    'index': index,
                    'start': start,
                    'diff_count': len(offsets),
                    'first_offset': offsets[0],
                })

    entry1 = _entry_point(ih1)
    entry2 = _entry_point(ih2)
    entry_bytes_changed = None
    if entry1 is not None and entry2 is not None:
        entry_addrs = set([entry1[1], entry2[1]])
        entry_bytes_changed = any(
            _page_bytes(ih1, addr, 1, pad)[0] !=
            _page_bytes(ih2, addr, 1, pad)[0]
            for addr in entry_addrs)

    return AcceptanceResult(
        page_size, pad, pages, diff_pages,
        entry1, entry2, ih1.start_addr, ih2.start_addr,
        entry_bytes_changed)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Page-wise acceptance comparison of two Intel HEX '
                    'files. Record layout (order, per-line byte count) '
                    'is ignored; holes compare equal to the pad byte.')
    parser.add_argument('file1', help='first Intel HEX file')
    parser.add_argument('file2', help='second Intel HEX file')
    parser.add_argument('--page-size', type=int, default=256,
                        help='fixed page length in bytes (default 256)')
    parser.add_argument('--pad', type=lambda s: int(s, 0), default=0xFF,
                        help='pad byte for holes (default 0xFF)')
    args = parser.parse_args(argv)

    ih1 = intelhex.IntelHex(args.file1)
    ih2 = intelhex.IntelHex(args.file2)
    result = compare(ih1, ih2, page_size=args.page_size, pad=args.pad)
    print(result.format(name1=args.file1, name2=args.file2))
    return 0 if result.ok else 1


if __name__ == '__main__':
    sys.exit(main())
