"""Bit-level merging of readings with per-byte validity masks.

An Intel HEX file stores whole bytes only.  Calibration readings, however,
often come with a validity mask: a byte says which bits of the neighbouring
value byte were actually read.  Two instruments can therefore disagree on a
single bit while still agreeing on all the other bits of the same address.

This module keeps that information instead of reducing it to one byte.  A
:class:`MaskedReading` is one reading from one source.  :class:`BitwiseMerger`
replays readings, keeping one evidence lane per source: a later reading may
supersede that source's earlier reading, but it never removes evidence that
belongs to another source.  :meth:`BitwiseMerger.result` then exposes, for
every address and bit, one of three states:

* agreed - every source that read the bit reports the same value;
* unread - no source read the bit;
* conflict - sources that read the bit report opposing values.

The three masks always partition a byte (``agreed | unread | conflict == 0xFF``)
and are disjoint.  An address with a conflicting bit is still reported with
all of its other bits intact.
"""

from collections import namedtuple
import json

from intelhex.compat import IntTypes

__all__ = [
    'BitState',
    'MaskedReading',
    'BitwiseMerger',
    'MaskedMergeResult',
    'merge_masked',
]

ALL_BITS = 0x0FF

#: Per-address result.  ``value`` contains only the bits selected by
#: ``agreed``; unresolved bits are left as zero in ``value``.
BitState = namedtuple('BitState', 'value agreed unread conflict')


def _check_byte(value, name):
    if not isinstance(value, IntTypes) or isinstance(value, bool) \
            or not 0 <= value <= 0xFF:
        raise ValueError('%s should be an integer in range 0..255' % name)
    return int(value)


def _check_address(addr):
    if isinstance(addr, bool):
        raise TypeError('Address should be an integer')
    if isinstance(addr, IntTypes):
        addr = int(addr)
    elif isinstance(addr, str):
        try:
            addr = int(addr, 0)
        except ValueError:
            raise TypeError('Address should be an integer or prefixed hex string')
    else:
        raise TypeError('Address should be an integer or hex string')
    if addr < 0:
        raise TypeError('Address should be >= 0.')
    return addr


def _reading_item(item):
    """Normalise one ``value``/``(value, mask)`` item."""
    if isinstance(item, (tuple, list)):
        if len(item) != 2:
            raise ValueError('Reading should be (value, mask) pair')
        value, mask = item
    elif isinstance(item, dict):
        try:
            value = item['value']
            mask = item['mask']
        except KeyError:
            raise ValueError("Reading dict should have 'value' and 'mask'")
    else:
        value = item
        mask = ALL_BITS
    return _check_byte(value, 'value'), _check_byte(mask, 'mask')


class MaskedReading(object):
    '''One masked calibration reading from a single source.

    Each address maps to a ``(value, mask)`` pair.  A set mask bit means that
    bit of ``value`` was really measured; a clear mask bit means it was not
    read.  Bits outside the mask are cleared in the stored value so that
    meaningless value bits can never manufacture a disagreement.
    '''

    def __init__(self, source, data=None, supersede=False, mask=None):
        if source is None:
            raise ValueError('source should not be None')
        try:
            hash(source)
        except TypeError:
            raise TypeError('source should be hashable')
        self.source = source
        self.supersede = bool(supersede)
        self._data = {}
        if data is not None:
            if mask is None:
                self.update(data)
            else:
                self._update_with_mask(data, mask)

    def _update_with_mask(self, values, mask):
        '''Add values supplied separately from their masks.'''
        from intelhex import IntelHex

        def value_items(obj):
            if isinstance(obj, IntelHex):
                return dict(obj._buf)
            if hasattr(obj, 'items'):
                return dict(obj.items())
            raise TypeError('values should be IntelHex or mapping')

        value_buf = value_items(values)
        if isinstance(mask, IntTypes) and not isinstance(mask, bool):
            mask_buf = None
        else:
            mask_buf = value_items(mask)
        extra = set(mask_buf or ()) - set(value_buf)
        if extra:
            raise ValueError(
                'Mask has addresses without values: %s'
                % ', '.join('0x%X' % a for a in sorted(extra)))
        for addr, value in value_buf.items():
            if mask_buf is None:
                item_mask = mask
            elif addr in mask_buf:
                item_mask = mask_buf[addr]
            else:
                raise ValueError('Mask is missing address 0x%X' % addr)
            self.set(addr, value, item_mask)
        return self

    def set(self, addr, value, mask=ALL_BITS):
        '''Record one byte and its validity mask.'''
        addr = _check_address(addr)
        value = _check_byte(value, 'value')
        mask = _check_byte(mask, 'mask')
        self._data[addr] = (value & mask, mask)
        return self

    def update(self, data):
        '''Add readings from a mapping or an iterable of address items.

        A sequence of ``(address, value)`` / ``(address, value, mask)``
        triples is also accepted, which is handy for literal test data.
        '''
        if isinstance(data, MaskedReading):
            items = data.items()
        elif hasattr(data, 'items'):
            items = data.items()
        else:
            items = data
        for item in items:
            if isinstance(item, (tuple, list)) and len(item) in (2, 3) \
                    and not isinstance(item[1], (tuple, list, dict)):
                addr, rest = item[0], item[1:]
                item = rest[0] if len(rest) == 1 else tuple(rest)
            else:
                addr, item = item
            addr = _check_address(addr)
            value, mask = _reading_item(item)
            self.set(addr, value, mask)
        return self

    def get(self, addr, default=None):
        '''Return ``(value, mask)`` for address, or *default* if absent.'''
        return self._data.get(_check_address(addr), default)

    def __getitem__(self, addr):
        return self._data[_check_address(addr)]

    def __setitem__(self, addr, item):
        value, mask = _reading_item(item)
        self.set(addr, value, mask)

    def __delitem__(self, addr):
        del self._data[_check_address(addr)]

    def __contains__(self, addr):
        return _check_address(addr) in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def items(self):
        return self._data.items()

    def addresses(self):
        '''Return occupied addresses in sorted order.'''
        return sorted(self._data)

    def copy(self):
        return MaskedReading(self.source, self._data, self.supersede)

    def to_dict(self):
        '''Return a plain ``{address: (value, mask)}`` dictionary.'''
        return dict(self._data)

    def to_json_dict(self):
        '''Return a JSON-ready dictionary, preserving source and supersession.'''
        return {
            'source': self.source,
            'supersede': self.supersede,
            'bytes': dict(
                ('0x%X' % addr, [value, mask])
                for addr, (value, mask) in self._data.items()
            ),
        }

    def to_json(self, **kwargs):
        return json.dumps(self.to_json_dict(), **kwargs)

    @classmethod
    def from_json(cls, payload, source=None, supersede=None):
        '''Build a reading from a JSON string or an already decoded mapping.

        The decoded form may be the output of :meth:`to_json_dict` or simply
        a mapping of addresses to values/pairs.  Explicit *source* and
        *supersede* arguments override values found in the payload.
        '''
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        if not hasattr(payload, 'items'):
            raise TypeError('payload should be a JSON object or mapping')
        data = payload
        if 'bytes' in payload or 'readings' in payload or 'data' in payload:
            if source is None:
                source = payload.get('source')
            if supersede is None:
                supersede = payload.get('supersede', False)
            data = payload.get('bytes', payload.get('readings',
                            payload.get('data')))
        if source is None:
            raise ValueError('source is required')
        return cls(source, data, supersede=bool(supersede))

    @classmethod
    def from_hex(cls, value_file, mask_file=None, source=None,
                 supersede=False, default_mask=None):
        '''Load a masked reading from one or two Intel HEX files.

        *value_file* supplies the values.  *mask_file* supplies one validity
        mask byte per address.  If it is omitted every value byte is treated
        as fully read.  If a mask file is given, every value address must have
        a mask unless *default_mask* is supplied; masks for addresses that
        have no value are rejected.
        '''
        from intelhex import IntelHex

        values = IntelHex(value_file)
        masks = None if mask_file is None else IntelHex(mask_file)
        if source is None:
            source = getattr(value_file, 'name', None) or str(value_file)
        return cls.from_intelhex(values, masks, source=source,
                                 supersede=supersede,
                                 default_mask=default_mask)

    @classmethod
    def from_intelhex(cls, values, masks=None, source=None,
                      supersede=False, default_mask=None):
        '''Load a masked reading from ``IntelHex`` objects or address dicts.

        *values* may be an :class:`intelhex.IntelHex` or a mapping of
        addresses to bytes.  *masks* uses the same forms; when omitted every
        value byte is treated as fully read.
        '''
        from intelhex import IntelHex

        def normalise(obj):
            if isinstance(obj, IntelHex):
                return dict(obj._buf)
            if hasattr(obj, 'items'):
                return dict((_check_address(a), _check_byte(v, 'value'))
                            for a, v in obj.items())
            raise TypeError('values should be IntelHex or mapping')

        value_buf = normalise(values)
        if masks is None:
            mask_buf = {}
        else:
            mask_buf = normalise(masks)
            extra = set(mask_buf) - set(value_buf)
            if extra:
                raise ValueError(
                    'Mask has addresses without values: %s'
                    % ', '.join('0x%X' % a for a in sorted(extra)))
            missing = set(value_buf) - set(mask_buf)
            if missing and default_mask is None:
                raise ValueError(
                    'Mask is missing addresses: %s'
                    % ', '.join('0x%X' % a for a in sorted(missing)))
        data = {}
        for addr, value in value_buf.items():
            if masks is None:
                mask = ALL_BITS
            else:
                mask = mask_buf.get(addr, default_mask)
            data[addr] = (value, mask)
        if source is None:
            source = getattr(values, 'name', None) or str(values)
        return cls(source, data, supersede=supersede)

    def __repr__(self):
        return 'MaskedReading(%r, %d bytes, supersede=%r)' % (
            self.source, len(self._data), self.supersede)


class BitwiseMerger(object):
    '''Replay masked readings and keep one evidence lane per source.

    Readings from different sources coexist.  A reading with
    ``supersede=True`` replaces all earlier readings of its own source; it
    cannot affect any other source's evidence.  A non-superseding reading is
    kept as an additional observation from the same source.
    '''

    def __init__(self):
        self._readings = {}
        self._sources = []

    def add(self, reading):
        '''Add a :class:`MaskedReading` (later supersession is per source).'''
        if not isinstance(reading, MaskedReading):
            raise TypeError('reading should be MaskedReading object')
        if reading.source not in self._readings:
            self._readings[reading.source] = []
            self._sources.append(reading.source)
        if reading.supersede:
            self._readings[reading.source] = [reading]
        else:
            self._readings[reading.source].append(reading)
        return self

    def add_reading(self, source, data=None, supersede=False):
        '''Convenience wrapper around ``add(MaskedReading(...))``.'''
        return self.add(MaskedReading(source, data, supersede=supersede))

    def add_file(self, value_file, mask_file=None, source=None,
                 supersede=False, default_mask=None):
        '''Load and add one masked reading from Intel HEX file(s).'''
        return self.add(MaskedReading.from_hex(
            value_file, mask_file=mask_file, source=source,
            supersede=supersede, default_mask=default_mask))

    def discard(self, source):
        '''Remove all evidence from one source.'''
        if source in self._readings:
            del self._readings[source]
            self._sources.remove(source)
        return self

    def clear(self):
        self._readings = {}
        self._sources = []
        return self

    def sources(self):
        '''Return source names in replay order.'''
        return list(self._sources)

    def readings(self, source=None):
        '''Return current readings, or readings of one source.'''
        if source is not None:
            return list(self._readings.get(source, ()))
        return [r for s in self._sources for r in self._readings[s]]

    def result(self):
        '''Compute the current per-bit consensus.'''
        addresses = set()
        for source in self._sources:
            for reading in self._readings[source]:
                addresses.update(reading._data)
        values = {}
        agreed = {}
        unread = {}
        conflict = {}
        evidence = {}
        for addr in sorted(addresses):
            observed = []
            for source in self._sources:
                for reading in self._readings[source]:
                    item = reading._data.get(addr)
                    if item is not None:
                        value, mask = item
                        observed.append((source, value, mask))
            value_byte = 0
            agreed_mask = 0
            unread_mask = 0
            conflict_mask = 0
            for bit in range(8):
                bit_mask = 1 << bit
                seen = set(value >> bit & 1
                           for source, value, mask in observed
                           if mask & bit_mask)
                if not seen:
                    unread_mask |= bit_mask
                elif len(seen) == 1:
                    agreed_mask |= bit_mask
                    value_byte |= (seen.pop() << bit)
                else:
                    conflict_mask |= bit_mask
            values[addr] = value_byte
            agreed[addr] = agreed_mask
            unread[addr] = unread_mask
            conflict[addr] = conflict_mask
            evidence[addr] = observed
        return MaskedMergeResult(values, agreed, unread, conflict,
                                 evidence, self.sources())


class MaskedMergeResult(object):
    '''Per-bit merge result for the addresses present in any reading.

    ``values`` holds the agreed value bits.  The ``agreed``, ``unread`` and
    ``conflict`` masks are disjoint and together cover every byte bit.  Bits
    in ``unread`` or ``conflict`` are zero in ``values``; they are not
    discarded along with the rest of the address.
    '''

    def __init__(self, values, agreed, unread, conflict, evidence=None,
                 sources=None):
        self._values = dict(values)
        self._agreed = dict(agreed)
        self._unread = dict(unread)
        self._conflict = dict(conflict)
        self._evidence = dict(evidence or {})
        self.sources = list(sources or ())

    def addresses(self):
        '''Return addresses in sorted order.'''
        return sorted(self._values)

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        return iter(self.addresses())

    def __contains__(self, addr):
        return _check_address(addr) in self._values

    def state(self, addr):
        '''Return a :class:`BitState` named tuple for one address.'''
        addr = _check_address(addr)
        return BitState(self._values[addr], self._agreed[addr],
                        self._unread[addr], self._conflict[addr])

    def __getitem__(self, addr):
        return self.state(addr)

    def value(self, addr):
        '''Return the agreed bits of one address (unresolved bits are zero).'''
        return self._values[_check_address(addr)]

    def agreed_mask(self, addr):
        '''Return the bit mask of agreed bits at one address.'''
        return self._agreed[_check_address(addr)]

    def unread_mask(self, addr):
        '''Return the bit mask of bits no source has read.'''
        return self._unread[_check_address(addr)]

    def conflict_mask(self, addr):
        '''Return the bit mask of bits with opposing evidence.'''
        return self._conflict[_check_address(addr)]

    def is_certain(self, addr):
        '''True when every bit of the address has an agreed value.'''
        return self.agreed_mask(addr) == ALL_BITS

    def bit_state(self, addr, bit):
        '''Return ``'0'``, ``'1'``, ``'unread'`` or ``'conflict'``.'''
        if not isinstance(bit, IntTypes) or isinstance(bit, bool) \
                or not 0 <= bit <= 7:
            raise ValueError('bit should be an integer in range 0..7')
        state = self.state(addr)
        mask = 1 << bit
        if state.conflict & mask:
            return 'conflict'
        if state.unread & mask:
            return 'unread'
        return str((state.value >> bit) & 1)

    def agreed_bits(self, addr):
        '''Return a list of bit numbers with agreed values.'''
        return _bits_of(self.agreed_mask(addr))

    def unread_bits(self, addr):
        '''Return a list of bit numbers not read by any source.'''
        return _bits_of(self.unread_mask(addr))

    def conflict_bits(self, addr):
        '''Return a list of bit numbers with opposing evidence.'''
        return _bits_of(self.conflict_mask(addr))

    def evidence(self, addr):
        '''Return ordered ``(source, value, mask)`` observations for an address.'''
        return list(self._evidence[_check_address(addr)])

    def bit_evidence(self, addr, bit):
        '''Return ``{0: [sources], 1: [sources]}`` for one bit.

        A source appears only when its mask actually covers the bit.  This
        makes it explicit which instruments support each side of a conflict.
        '''
        if not isinstance(bit, IntTypes) or isinstance(bit, bool) \
                or not 0 <= bit <= 7:
            raise ValueError('bit should be an integer in range 0..7')
        result = {0: [], 1: []}
        bit_mask = 1 << bit
        for source, value, mask in self.evidence(addr):
            if mask & bit_mask:
                result[(value >> bit) & 1].append(source)
        return result

    def conflict_values(self, addr):
        '''Return ``{bit: [distinct values]}`` for conflicting bits.'''
        result = {}
        for bit in self.conflict_bits(addr):
            seen = set(value >> bit & 1
                       for source, value, mask in self.evidence(addr)
                       if mask & (1 << bit))
            result[bit] = sorted(seen)
        return result

    def to_dict(self, with_evidence=True):
        '''Return a JSON-ready description of the merge result.'''
        result = {
            'sources': list(self.sources),
            'addresses': [],
        }
        for addr in self.addresses():
            state = self.state(addr)
            item = {
                'address': addr,
                'value': state.value,
                'agreed_mask': state.agreed,
                'unread_mask': state.unread,
                'conflict_mask': state.conflict,
            }
            if with_evidence:
                item['evidence'] = [
                    {'source': source, 'value': value, 'mask': mask}
                    for source, value, mask in self.evidence(addr)
                ]
            result['addresses'].append(item)
        return result

    def to_json(self, **kwargs):
        return json.dumps(self.to_dict(), **kwargs)

    def write_json(self, fobj):
        '''Write :meth:`to_dict` as JSON to a file name or file object.'''
        data = self.to_json(indent=2, sort_keys=True)
        if getattr(fobj, 'write', None) is not None:
            fobj.write(data)
            fobj.write('\n')
        else:
            with open(fobj, 'w') as f:
                f.write(data)
                f.write('\n')

    def to_intelhex(self):
        '''Return an :class:`intelhex.IntelHex` containing agreed bits only.

        Unread and conflicting bits are cleared, so this object is safe to
        write as a HEX file but is *not* a lossless view of the merge.  Use
        the masks on this result to see which bits are still unresolved.
        Addresses whose bits are all unresolved (no agreed bit) are omitted.
        '''
        from intelhex import IntelHex
        ih = IntelHex()
        for addr in self.addresses():
            if self._agreed[addr]:
                ih[addr] = self._values[addr]
        return ih

    def conflicts(self):
        '''Return ``{address: conflict_mask}`` for addresses with conflicts.'''
        return dict((addr, self._conflict[addr])
                    for addr in self.addresses() if self._conflict[addr])

    def unread(self):
        '''Return ``{address: unread_mask}`` for addresses with unread bits.'''
        return dict((addr, self._unread[addr])
                    for addr in self.addresses() if self._unread[addr])

    def certain_addresses(self):
        '''Return addresses where every bit is agreed.'''
        return [addr for addr in self.addresses()
                if self._agreed[addr] == ALL_BITS]

    def summarize(self):
        '''Return a compact text summary of the merge state.'''
        conflicts = self.conflicts()
        unread = self.unread()
        return ('%d addresses, %d with conflicts, %d with unread bits'
                % (len(self._values), len(conflicts), len(unread)))

    def __repr__(self):
        return 'MaskedMergeResult(%d addresses, %d conflicts)' % (
            len(self._values),
            sum(1 for a in self._values if self._conflict[a]))


def _bits_of(mask):
    return [bit for bit in range(8) if mask & (1 << bit)]


def merge_masked(readings):
    '''Merge an iterable of :class:`MaskedReading` objects in replay order.

    A single reading may also be passed directly.  Readings that carry the
    same source are grouped; a reading with ``supersede=True`` replaces that
    source's own earlier readings while leaving all other sources untouched.
    '''
    if isinstance(readings, (MaskedReading, dict)):
        readings = [readings]
    merger = BitwiseMerger()
    for reading in readings:
        if isinstance(reading, MaskedReading):
            merger.add(reading)
            continue
        if isinstance(reading, dict):
            source = reading.get('source', reading.get('name'))
            if source is None:
                raise ValueError("Reading dict requires a 'source'")
            data = reading.get('bytes', reading.get('readings',
                            reading.get('data')))
            supersede = reading.get('supersede', False)
            merger.add(MaskedReading(source, data, supersede=supersede))
            continue
        if isinstance(reading, (tuple, list)) and len(reading) in (2, 3):
            if len(reading) == 2:
                source, data = reading
                merger.add(MaskedReading(source, data))
            else:
                source, values, masks = reading
                merger.add(MaskedReading(source, values, mask=masks))
            continue
        merger.add(reading)
    return merger.result()

