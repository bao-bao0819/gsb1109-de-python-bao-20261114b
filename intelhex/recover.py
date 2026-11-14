# Copyright (c) 2026
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice,
#   this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# * Neither the name of the author nor the names of its contributors may be
#   used to endorse or promote products derived from this software without
#   specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

'''Recover HEX files with a lost extended address record.

The normal reader assumes every data record is preceded by a known extended
address record.  When one extended address line is lost, the following data
records are still checksum-valid but their upper address bits are unknown.

This module keeps every interpretation that is legal under a supplied list of
possible page segments and a set of absolute-address calibration witnesses.
It reports the addresses each data line may still occupy, and archives only
bytes whose address and value agree in every surviving interpretation.

Content after the next explicit address switch is unambiguous and is collected
even when the witnesses contradict each other.
'''

__docformat__ = "javadoc"

from array import array
from binascii import unhexlify
import os

from intelhex import (
    AddressOverlapError,
    DuplicateStartAddressRecordError,
    EOFRecordError,
    ExtendedLinearAddressRecordError,
    ExtendedSegmentAddressRecordError,
    HexRecordError,
    IntelHex,
    IntelHexError,
    RecordChecksumError,
    RecordLengthError,
    RecordTypeError,
    StartLinearAddressRecordError,
    StartSegmentAddressRecordError,
    )
from intelhex.compat import StringIO
from intelhex.compat import asbytes
from intelhex.compat import asstr
from intelhex.compat import dict_items_g


#: Bytes covered by an extended linear address (type 04) page.
LINEAR_PAGE_SIZE = 0x10000

#: Address granularity of an extended segment address (type 02) page.
SEGMENT_PAGE_SIZE = 0x10


class HexRecoveryError(IntelHexError):
    '''Base error for ambiguous-address recovery.'''


class WitnessConflictError(HexRecoveryError):
    '''No page segment satisfies all calibration witnesses.'''

    def __init__(self, result):
        self.result = result
        self.witnesses = result.conflict_witnesses
        lines = ', '.join(w.describe() for w in self.witnesses)
        if not lines:
            lines = 'none'
        IntelHexError.__init__(
            self,
            'No feasible page segment; conflicting witnesses: %s' % lines)


class Witness(object):
    '''One calibration witness.

    There are two accepted forms:

    * byte witness: an absolute ``address`` and the ``value`` found there;
    * line witness: an absolute ``address`` and the source ``line`` that must
      cover that address (``value`` is then ``None``).

    ``line`` on a byte witness also identifies the witness line for
    diagnostics.  Use ``label`` when no line number is available.
    '''

    __slots__ = ('address', 'value', 'line', 'label', 'kind')

    def __init__(self, address, value=None, line=None, label=None):
        self.address = int(address)
        self.value = None if value is None else int(value)
        self.line = line
        self.label = label
        self.kind = 'byte' if self.value is not None else 'line'
        if self.address < 0:
            raise ValueError('witness address must be non-negative')
        if self.value is not None and not 0 <= self.value <= 0xFF:
            raise ValueError('witness value must fit in one byte')
        if self.kind == 'line' and self.line is None:
            raise ValueError('line witness needs a source line')

    def describe(self):
        parts = []
        if self.label is not None:
            parts.append(str(self.label))
        if self.line is not None:
            parts.append('line %s' % self.line)
        if not parts:
            parts.append('0x%X' % self.address)
        location = ', '.join(parts)
        if self.value is None:
            return '%s @ 0x%X' % (location, self.address)
        return '%s (0x%X=0x%02X)' % (location, self.address, self.value)

    def to_dict(self):
        return {'address': self.address,
                'value': self.value,
                'line': self.line,
                'label': self.label,
                'kind': self.kind,
                }

    def __repr__(self):
        return 'Witness(%r, %r, line=%r, label=%r)' % (
            self.address, self.value, self.line, self.label)


class PageOption(object):
    """One possible value of the lost extended address record.

    ``segment`` is the record payload (upper 16-bit linear address for type
    04 or upper segment address for type 02).  ``base`` is the byte address
    of record offset zero.  A segment record addresses up to 64 KiB starting
    at ``base`` even though consecutive segment values differ by 16 bytes.
    """

    __slots__ = ('segment', 'base', 'record_type', 'page_size', 'window_size')

    def __init__(self, segment, record_type=4):
        self.segment = int(segment)
        self.record_type = int(record_type)
        if self.record_type == 4:
            self.page_size = LINEAR_PAGE_SIZE
            self.window_size = LINEAR_PAGE_SIZE
        elif self.record_type == 2:
            self.page_size = SEGMENT_PAGE_SIZE
            self.window_size = LINEAR_PAGE_SIZE
        else:
            raise ValueError('record_type must be 2 or 4')
        if not 0 <= self.segment <= 0xFFFF:
            raise ValueError('page segment must fit in 16 bits')
        self.base = self.segment * self.page_size

    def to_dict(self):
        return {'segment': self.segment,
                'base': self.base,
                'record_type': self.record_type,
                'page_size': self.page_size,
                'window_size': self.window_size,
                }

    def __eq__(self, other):
        return (isinstance(other, PageOption) and
                self.segment == other.segment and
                self.record_type == other.record_type)

    def __hash__(self):
        return hash((self.segment, self.record_type))

    def __repr__(self):
        return 'PageOption(segment=0x%04X, base=0x%X)' % (
            self.segment, self.base)


class DataLinePossibility(object):
    '''Address possibilities for one data record in the damaged region.

    ``possible_ranges`` are the half-open address ranges reachable through
    pages that satisfy every witness.  ``candidate_ranges`` are the ranges
    of all supplied candidate pages, including rejected ones.
    '''

    __slots__ = ('line', 'offset', 'data', 'page_ranges',
                 'possible_ranges', 'candidate_ranges', 'archived_ranges',
                 'archived_bytes')

    def __init__(self, line, offset, data, page_ranges, possible_ranges,
                 candidate_ranges, archived_ranges, archived_bytes):
        self.line = line
        self.offset = offset
        self.data = data
        self.page_ranges = page_ranges
        self.possible_ranges = possible_ranges
        self.candidate_ranges = candidate_ranges
        self.archived_ranges = archived_ranges
        self.archived_bytes = archived_bytes

    def contains_address(self, address):
        return any(start <= address < end
                   for start, end in self.possible_ranges)

    def to_dict(self):
        return {'line': self.line,
                'offset': self.offset,
                'length': len(self.data),
                'data': list(self.data),
                'page_ranges': [{'segment': page.segment,
                                 'start': start,
                                 'end': end,
                                 } for page, start, end in self.page_ranges],
                'possible_ranges': [list(x) for x in self.possible_ranges],
                'candidate_ranges': [list(x) for x in self.candidate_ranges],
                'archived_ranges': [list(x) for x in self.archived_ranges],
                'archived_bytes': self.archived_bytes,
                }

    def __repr__(self):
        return 'DataLinePossibility(line=%r, offset=0x%X, ranges=%r)' % (
            self.line, self.offset, self.possible_ranges)


class RecoveryResult(object):
    '''Outcome of :func:`recover_hex`.

    ``archive`` is an :class:`intelhex.IntelHex` holding only bytes whose
    address and value are identical in every legal interpretation.  Bytes
    after the next explicit address switch are always included.
    '''

    def __init__(self, archive, data_lines, feasible_pages, rejected_pages,
                 conflict_witnesses, unmatched_witnesses, ambiguous_start_line,
                 ambiguous_end_line, switch_line, warnings, counts,
                 conflict_core=()):
        self.archive = archive
        self.data_lines = tuple(data_lines)
        self.feasible_pages = tuple(feasible_pages)
        self.rejected_pages = tuple(rejected_pages)
        self.conflict_witnesses = tuple(conflict_witnesses)
        self.conflict_core = tuple(conflict_core)
        self.unmatched_witnesses = tuple(unmatched_witnesses)
        self.ambiguous_start_line = ambiguous_start_line
        self.ambiguous_end_line = ambiguous_end_line
        self.switch_line = switch_line
        self.warnings = tuple(warnings)
        self.counts = dict(counts)

    @property
    def has_ambiguous_region(self):
        return self.ambiguous_start_line is not None

    @property
    def has_conflict(self):
        return bool(self.has_ambiguous_region and not self.feasible_pages)

    def line(self, line):
        ''':return: the :class:`DataLinePossibility` for a source line.'''
        for item in self.data_lines:
            if item.line == line:
                return item
        raise KeyError('line %r is not in the ambiguous region' % (line,))

    def archived_segments(self):
        return self.archive.segments()

    def to_dict(self):
        start_addr = self.archive.start_addr
        return {
            'ambiguous': {
                'start_line': self.ambiguous_start_line,
                'end_line': self.ambiguous_end_line,
                'switch_line': self.switch_line,
                },
            'pages': {
                'feasible': [p.to_dict() for p in self.feasible_pages],
                'rejected': [{'page': p.to_dict(),
                              'witnesses': [w.to_dict() for w in witnesses],
                              'reasons': list(reasons),
                              } for p, witnesses, reasons in self.rejected_pages],
                },
            'conflict_witnesses': [w.to_dict()
                                   for w in self.conflict_witnesses],
            'conflict_core': [w.to_dict() for w in self.conflict_core],
            'unmatched_witnesses': [w.to_dict()
                                    for w in self.unmatched_witnesses],
            'data_lines': [r.to_dict() for r in self.data_lines],
            'archive': {
                'segments': [list(x) for x in self.archive.segments()],
                'start_addr': dict(start_addr) if start_addr else None,
                },
            'counts': dict(self.counts),
            'warnings': list(self.warnings),
            }

    def __repr__(self):
        return ('RecoveryResult(feasible_pages=%r, data_lines=%d, '
                'conflict=%r)' % (self.feasible_pages,
                                  len(self.data_lines),
                                  self.has_conflict))


class _HexLine(object):
    __slots__ = ('line', 'kind', 'offset', 'data', 'value')

    def __init__(self, line, kind, offset, data, value=None):
        self.line = line
        self.kind = kind
        self.offset = offset
        self.data = data
        self.value = value

    def __repr__(self):
        return '_HexLine(line=%r, kind=%r, offset=0x%X)' % (
            self.line, self.kind, self.offset)


def _decode_line(s, line):
    '''Decode one line, using the same diagnostics as IntelHex.'''
    s = s.rstrip('\r\n')
    if not s:
        return None
    if s[0] != ':':
        raise HexRecordError(line=line)
    try:
        bin = array('B', unhexlify(asbytes(s[1:])))
    except (TypeError, ValueError):
        raise HexRecordError(line=line)
    if len(bin) < 5:
        raise HexRecordError(line=line)

    record_length = bin[0]
    if len(bin) != 5 + record_length:
        raise RecordLengthError(line=line)

    addr = bin[1] * 256 + bin[2]
    record_type = bin[3]
    if not 0 <= record_type <= 5:
        raise RecordTypeError(line=line)
    if sum(bin) & 0xFF:
        raise RecordChecksumError(line=line)

    data = bytes(bytearray(bin[4:4 + record_length]))
    value = None

    if record_type == 0:
        pass
    elif record_type == 1:
        if record_length != 0:
            raise EOFRecordError(line=line)
    elif record_type == 2:
        if record_length != 2 or addr != 0:
            raise ExtendedSegmentAddressRecordError(line=line)
        value = bin[4] * 256 + bin[5]
    elif record_type == 4:
        if record_length != 2 or addr != 0:
            raise ExtendedLinearAddressRecordError(line=line)
        value = bin[4] * 256 + bin[5]
    elif record_type == 3:
        if record_length != 4 or addr != 0:
            raise StartSegmentAddressRecordError(line=line)
        value = {'CS': bin[4] * 256 + bin[5],
                 'IP': bin[6] * 256 + bin[7],
                 }
    elif record_type == 5:
        if record_length != 4 or addr != 0:
            raise StartLinearAddressRecordError(line=line)
        value = {'EIP': (bin[4] * 16777216 + bin[5] * 65536 +
                         bin[6] * 256 + bin[7]),
                 }

    return _HexLine(line, record_type, addr, data, value)


def _read_lines(source):
    if getattr(source, 'read', None) is not None:
        return source, False
    if isinstance(source, getattr(os, 'PathLike', ())) and \
            hasattr(source, '__fspath__'):
        return open(os.fspath(source), 'r'), True
    if isinstance(source, (bytes, bytearray)):
        source = asstr(bytes(source))
    if isinstance(source, str):
        if not source or '\n' in source or source.lstrip().startswith(':'):
            return StringIO(source), False
        return open(source, 'r'), True
    return source, False


def _parse(source):
    stream, close = _read_lines(source)
    records = []
    start_seen = False
    try:
        for line_no, text in enumerate(stream, 1):
            rec = _decode_line(text, line_no)
            if rec is None:
                continue
            if rec.kind in (3, 5):
                if start_seen:
                    raise DuplicateStartAddressRecordError(line=line_no)
                start_seen = True
            records.append(rec)
            if rec.kind == 1:
                break
    finally:
        if close:
            stream.close()
    return records


def _normalise_witnesses(witnesses):
    if witnesses is None:
        return []
    if isinstance(witnesses, Witness):
        return [witnesses]
    if isinstance(witnesses, dict):
        if 'address' in witnesses or 'addr' in witnesses:
            witnesses = [witnesses]
        else:
            witnesses = sorted(dict_items_g(witnesses))
    elif (isinstance(witnesses, (tuple, list)) and len(witnesses) == 2 and
            isinstance(witnesses[0], int) and
            isinstance(witnesses[1], int)):
        witnesses = [witnesses]

    result = []
    for index, item in enumerate(witnesses):
        if isinstance(item, Witness):
            result.append(item)
            continue
        line = None
        label = None
        if isinstance(item, dict):
            address = item.get('address', item.get('addr'))
            value = item.get('value', item.get('byte'))
            line = item.get('line', item.get('witness_line'))
            label = item.get('label', item.get('name', item.get('id')))
            if address is None:
                raise ValueError('witness needs an address')
            if value is None and line is None:
                raise ValueError('witness needs a value or a source line')
        elif isinstance(item, (tuple, list)):
            if len(item) < 2:
                raise ValueError('witness tuple needs address and value')
            address, value = item[0], item[1]
            if len(item) > 2:
                label = item[2]
            if len(item) > 3:
                line = item[3]
        else:
            raise ValueError('unsupported witness: %r' % (item,))

        if label is None:
            label = 'witness[%d]' % index
        if isinstance(value, (bytes, bytearray)):
            for offset, byte in enumerate(value):
                result.append(Witness(address + offset, byte,
                                      line=line, label=label))
        else:
            result.append(Witness(address, value, line=line, label=label))
    return result


def _page_options(page_segments, page_bases, record_type):
    if record_type not in (2, 4):
        raise ValueError('record_type must be 2 (segment) or 4 (linear)')
    page_size = LINEAR_PAGE_SIZE if record_type == 4 else SEGMENT_PAGE_SIZE
    if page_segments is None and page_bases is None:
        raise ValueError('page_segments is required')
    if page_segments is None:
        page_segments = [int(base) // page_size for base in page_bases]
        if any(int(base) % page_size for base in page_bases):
            raise ValueError('page base is not aligned to page size')

    pages = []
    seen = set()
    for value in page_segments:
        if isinstance(value, PageOption):
            page = PageOption(value.segment, value.record_type)
        else:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError('page segment must be an integer')
            if value > 0xFFFF:
                if value % page_size:
                    raise ValueError('page base is not aligned to page size')
                page = PageOption(value // page_size, record_type)
            else:
                page = PageOption(value, record_type)
        if page.record_type != record_type:
            raise ValueError('page record_type differs from record_type')
        if page not in seen:
            seen.add(page)
            pages.append(page)
    if not pages:
        raise ValueError('page_segments must not be empty')
    pages.sort(key=lambda p: p.base)
    return pages


def _find_region(records, start_line, end_line, switch_line):
    data_indices = [i for i, r in enumerate(records) if r.kind == 0]
    if not data_indices:
        return None

    first_switch = next((i for i, r in enumerate(records)
                         if r.kind in (2, 4)), len(records))
    if start_line is not None:
        start = next((i for i in data_indices if records[i].line >= start_line),
                     None)
        if start is None:
            return None
    else:
        leading = [i for i in data_indices if i < first_switch]
        if not leading:
            return None
        start = leading[0]

    stop = len(records)
    if end_line is not None:
        stop = next((i for i, r in enumerate(records)
                     if r.line >= end_line), len(records))
    elif switch_line is not None:
        stop = next((i for i, r in enumerate(records)
                     if r.line >= switch_line), len(records))

    switch = next((i for i in range(start + 1, stop)
                   if records[i].kind in (2, 4)), None)
    end = switch if switch is not None else stop
    return start, end, switch


def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if start == end:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return tuple(merged)


def _collect_relative(data_records):
    '''Map page-relative offset to (byte, line), rejecting overlaps.'''
    relative = {}
    for rec in data_records:
        for index, byte in enumerate(rec.data):
            offset = rec.offset + index
            if offset in relative:
                raise AddressOverlapError(address=offset, line=rec.line)
            relative[offset] = (byte, rec.line)
    return relative


def _page_buffers(pages, relative):
    return [dict((page.base + offset, value)
                 for offset, (value, _line) in dict_items_g(relative))
            for page in pages]


def _record_buffer(base, rec):
    return dict((base + rec.offset + index, value)
                for index, value in enumerate(rec.data))


def _append_known(target, buffer, line):
    for address in sorted(buffer):
        if address in target and target[address] != buffer[address]:
            raise AddressOverlapError(address=address, line=line)
        target[address] = buffer[address]


def _witness_status(witnesses, pages, buffers, known, data_records=()):
    '''Return status[p_index][w_index].

    Each column is ``sat``, ``violated``, ``outside`` or ``absent``.  A
    byte witness constrains a page when its absolute address maps to a known
    byte of this damaged block: either a byte of a data line under that page,
    or a byte already placed in the unambiguous archive.  A line witness
    constrains a page whenever the referenced data line exists, because it
    pins that line's absolute address.
    '''
    status = []
    for page, buffer in zip(pages, buffers):
        row = []
        for witness in witnesses:
            if witness.kind == 'line':
                rec = next((r for r in data_records
                            if r.line == witness.line), None)
                if rec is None:
                    row.append('absent')
                else:
                    start = page.base + rec.offset
                    row.append('sat' if start <= witness.address <
                               start + len(rec.data) else 'violated')
                continue
            if witness.address in known:
                row.append('sat' if known[witness.address] == witness.value
                           else 'violated')
            elif not page.base <= witness.address < (
                    page.base + page.window_size):
                row.append('outside')
            elif witness.address in buffer:
                row.append('sat' if buffer[witness.address] == witness.value
                           else 'violated')
            else:
                row.append('absent')
        status.append(row)
    return status


def _blocking(status_value):
    return status_value == 'violated'


def _violations(status, page_index, witnesses):
    return [w for w, value in zip(witnesses, status[page_index])
            if _blocking(value)]


def _minimal_conflict_core(witnesses, pages, status):
    '''Return a minimal witness subset that blocks every candidate page.

    Each witness in the result is one calibration line whose position or
    value rules out page interpretations; together they leave no feasible
    page.
    '''
    page_count = len(pages)
    core = []
    remaining = set(range(page_count))
    while remaining:
        best = None
        best_blocked = set()
        for index, witness in enumerate(witnesses):
            blocked = set(p for p in remaining
                          if _blocking(status[p][index]))
            if len(blocked) > len(best_blocked):
                best, best_blocked = witness, blocked
        if best is None:
            break
        core.append(best)
        remaining -= best_blocked
    for witness in list(core):
        trial = [w for w in core if w is not witness]
        if not trial:
            continue
        blocked = set()
        for p in range(page_count):
            if any(_blocking(status[p][i])
                   for i, w in enumerate(witnesses)
                   if any(w is x for x in trial)):
                blocked.add(p)
        if len(blocked) == page_count:
            core = trial
    return core


def _contiguous(addresses):
    ranges = []
    start = end = None
    for address in sorted(addresses):
        if start is None:
            start = end = address
        elif address == end + 1:
            end = address
        else:
            ranges.append((start, end + 1))
            start = end = address
    if start is not None:
        ranges.append((start, end + 1))
    return ranges


def recover_hex(source, page_segments=None, witnesses=(), record_type=None,
                start_line=None, end_line=None, switch_line=None, strict=False,
                possible_pages=None, calibration=None, pages=None,
                page_bases=None, damaged_lines=None, line_witnesses=None,
                next_switch=None, damaged_start=None, damaged_end=None,
                page_segment=None, calibration_witnesses=None):
    '''Recover a HEX stream whose extended address record was lost.

    @param  source          file name, file-like object, iterable of lines, or
                            a string containing HEX text.
    @param  page_segments   possible 16-bit payloads of the lost record.
                            Values above 0xFFFF are accepted as byte base
                            addresses and converted to the record payload.
    @param  witnesses       absolute-address calibration witnesses.  Accepts
                            a mapping ``{address: byte}``, a sequence of
                            ``(address, byte[, label[, line]])`` tuples, dicts
                            with ``address``/``value``/``label``/``line``,
                            :class:`Witness` objects, or line witnesses as
                            ``{'address': addr, 'line': source_line}``.
    @param  record_type     type of the lost record: 4 (linear) or 2
                            (segment).  If omitted, inferred from the next
                            explicit address switch, defaulting to 4.
    @param  start_line      optional 1-based first source line of the damaged
                            data block.  Without it, recovery conservatively
                            uses the leading data records before the first
                            explicit address switch.
    @param  end_line        optional 1-based source line where the damaged
                            block ends (exclusive).
    @param  switch_line     optional 1-based line of the next explicit address
                            switch that ends the damaged block.
    @param  strict          raise :class:`WitnessConflictError` when no page
                            survives the witnesses.
    @param  possible_pages  alias for ``page_segments``.
    @param  calibration     alias for ``witnesses``.
    @param  pages           alias for ``page_segments``.
    @param  page_bases      possible page base addresses, converted to the
                            record payload for ``record_type``.
    @param  damaged_lines   ``(start_line, end_line)`` pair.
    @param  line_witnesses  mapping ``{source_line: absolute_address}`` or
                            sequence of ``(source_line, absolute_address)``.
    @param  next_switch     alias for ``switch_line``.
    @param  damaged_start   alias for ``start_line``.
    @param  damaged_end     alias for ``end_line``.
    @param  page_segment    singular alias for ``page_segments``.
    @param  calibration_witnesses
                            alias for ``witnesses``.

    @return                 :class:`RecoveryResult`.
    '''
    if page_segments is None:
        page_segments = possible_pages
    if page_segments is None:
        page_segments = pages
    if page_segments is None and page_segment is not None:
        page_segments = [page_segment]
    if not witnesses:
        witnesses = calibration
    if not witnesses:
        witnesses = calibration_witnesses
    if start_line is None and damaged_start is not None:
        start_line = damaged_start
    if end_line is None and damaged_end is not None:
        end_line = damaged_end
    if switch_line is None and next_switch is not None:
        switch_line = next_switch
    if damaged_lines is not None:
        if start_line is None:
            start_line = damaged_lines[0]
        if len(damaged_lines) > 1 and end_line is None:
            end_line = damaged_lines[1]

    witnesses = _normalise_witnesses(witnesses)
    if line_witnesses:
        if isinstance(line_witnesses, dict):
            line_witnesses = sorted(dict_items_g(line_witnesses))
        for line, address in line_witnesses:
            witnesses.append(Witness(address, line=line,
                                     label='line %s' % line))
    if isinstance(source, IntelHex):
        buffer = StringIO()
        source.write_hex_file(buffer)
        buffer.seek(0)
        source = buffer
    records = _parse(source)
    region = _find_region(records, start_line, end_line, switch_line)

    if record_type is None:
        if region is not None and region[2] is not None:
            record_type = records[region[2]].kind
        else:
            record_type = 4

    archive = IntelHex()
    warnings = []
    counts = {'before': 0, 'ambiguous': 0, 'after_switch': 0,
              'archived_bytes': 0}

    start_addr = None
    for rec in records:
        if rec.kind in (3, 5):
            start_addr = rec.value
    archive.start_addr = start_addr

    if region is None and page_segments is None and page_bases is None:
        page_segments = [0]
    if region is None:
        warnings.append('no_missing_extended_address_detected')
        base = None
        for rec in records:
            if rec.kind in (2, 4):
                base = rec.value * (LINEAR_PAGE_SIZE if rec.kind == 4
                                    else SEGMENT_PAGE_SIZE)
            elif rec.kind == 0:
                if base is None:
                    warnings.append('data_without_address_switch_ignored')
                    continue
                _append_known(archive._buf, _record_buffer(base, rec), rec.line)
                counts['before'] += len(rec.data)
        result = RecoveryResult(archive, [], [], [], [], [], None, None,
                                None, warnings, counts)
        if strict and result.has_conflict:
            raise WitnessConflictError(result)
        return result

    pages = _page_options(page_segments, page_bases, record_type)
    start, end, switch = region
    before_records = records[:start]
    region_records = records[start:end]
    after_records = records[end:]
    switch_line = records[switch].line if switch is not None else None
    if region_records:
        ambiguous_start_line = region_records[0].line
        ambiguous_end_line = region_records[-1].line
    else:
        ambiguous_start_line = ambiguous_end_line = None

    base = None
    for rec in before_records:
        if rec.kind in (2, 4):
            base = rec.value * (LINEAR_PAGE_SIZE if rec.kind == 4
                                else SEGMENT_PAGE_SIZE)
        elif rec.kind == 0:
            if base is None:
                raise HexRecoveryError(
                    'data record at line %d has no known page' % rec.line)
            _append_known(archive._buf, _record_buffer(base, rec), rec.line)
            counts['before'] += len(rec.data)

    base = None
    for rec in after_records:
        if rec.kind in (2, 4):
            base = rec.value * (LINEAR_PAGE_SIZE if rec.kind == 4
                                else SEGMENT_PAGE_SIZE)
        elif rec.kind == 0:
            if base is None:
                raise HexRecoveryError(
                    'data record at line %d has no known page' % rec.line)
            _append_known(archive._buf, _record_buffer(base, rec), rec.line)
            counts['after_switch'] += len(rec.data)

    data_records = [r for r in region_records if r.kind == 0]
    relative = _collect_relative(data_records)
    counts['ambiguous'] = len(relative)
    buffers = _page_buffers(pages, relative)
    status = _witness_status(witnesses, pages, buffers, archive._buf,
                             data_records)

    feasible = []
    rejected = []
    witness_ok_indices = []
    for index, page in enumerate(pages):
        violated = _violations(status, index, witnesses)
        known_conflicts = sorted(address for address, value
                                 in dict_items_g(buffers[index])
                                 if address in archive._buf and
                                 archive._buf[address] != value)
        reasons = []
        if violated:
            reasons.append('witness_violation')
        else:
            witness_ok_indices.append(index)
        if known_conflicts:
            reasons.append('known_data_conflict')
        if reasons:
            rejected.append((page, tuple(violated), tuple(reasons)))
        else:
            feasible.append(page)

    unmatched = []
    for index, witness in enumerate(witnesses):
        if all(status[p][index] in ('outside', 'absent')
               for p in range(len(pages))):
            unmatched.append(witness)

    conflict = []
    conflict_core = []
    if not witness_ok_indices:
        conflict_core = _minimal_conflict_core(witnesses, pages, status)
        core_ids = set(id(w) for w in conflict_core)
        conflict = [w for w in witnesses if id(w) in core_ids]
    elif not feasible:
        warnings.append('no_feasible_page_after_known_data_checks')

    feasible_buffers = _page_buffers(feasible, relative)
    common = {}
    if feasible_buffers:
        common = dict(feasible_buffers[0])
        for buffer in feasible_buffers[1:]:
            common = dict((address, value)
                          for address, value in dict_items_g(common)
                          if address in buffer and buffer[address] == value)

    for address in sorted(common):
        if address in archive._buf and archive._buf[address] != common[address]:
            del common[address]
    _append_known(archive._buf, common, 0)
    counts['archived_bytes'] = len(common)

    data_lines = []
    for rec in data_records:
        page_ranges = []
        for page in feasible:
            page_ranges.append((page, page.base + rec.offset,
                                page.base + rec.offset + len(rec.data)))
        candidate_ranges = [(page.base + rec.offset,
                             page.base + rec.offset + len(rec.data))
                            for page in pages]
        archived_addresses = []
        for index, value in enumerate(rec.data):
            offset = rec.offset + index
            addresses = set(page.base + offset for page in feasible)
            if len(addresses) == 1:
                address = next(iter(addresses))
                if archive._buf.get(address) == value:
                    archived_addresses.append(address)
        data_lines.append(DataLinePossibility(
            rec.line, rec.offset, rec.data,
            tuple(page_ranges),
            _merge_ranges([(start_, end_) for page, start_, end_
                           in page_ranges]),
            _merge_ranges(candidate_ranges),
            _merge_ranges(_contiguous(archived_addresses)),
            len(archived_addresses),
            ))
        counts['archived_line_bytes'] = (counts.get('archived_line_bytes', 0)
                                         + len(archived_addresses))

    result = RecoveryResult(
        archive, data_lines, feasible, rejected, conflict, unmatched,
        ambiguous_start_line, ambiguous_end_line, switch_line,
        warnings, counts, conflict_core=conflict_core)
    if strict and result.has_conflict:
        raise WitnessConflictError(result)
    return result

