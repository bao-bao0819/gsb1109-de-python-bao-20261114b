"""Core implementation of the traceable measurement word table."""

from intelhex import IntelHex


class DuplicateByteError(Exception):
    """Raised when both channel definitions claim the same physical byte.

    The error never picks a channel: it records every affected physical byte
    together with the two competing claims and every sample whose word uses
    such a byte.
    """

    def __init__(self, conflicts, samples):
        self.conflicts = conflicts
        self.samples = samples
        lines = ['Both definitions claim the same physical byte(s):']
        for address, first, second in conflicts:
            lines.append(
                '  physical 0x%X: %s vs %s'
                % (address, self._describe(first), self._describe(second))
            )
        lines.append('Affected samples:')
        for sample in samples:
            lines.append('  %s' % sample)
        super(DuplicateByteError, self).__init__('\n'.join(lines))

    @staticmethod
    def _describe(claim):
        channel, region_name, local_address = claim
        return '%s byte at local 0x%X (%s)' % (
            channel, local_address, region_name,
        )


class CalibrationRegion(object):
    """One calibration segment of a channel image.

    @ivar name             human readable name of the calibration region
    @ivar physical_start   first physical address covered by the region
    @ivar local_start      local image address mapped at ``physical_start``
    @ivar length           number of physical bytes covered
    """

    def __init__(self, physical_start, local_start, length, name):
        self.name = name
        self.physical_start = physical_start
        self.local_start = local_start
        self.length = length
        self.physical_end = physical_start + length
        self.local_end = local_start + length

    def contains(self, physical_address):
        return self.physical_start <= physical_address < self.physical_end

    def to_local(self, physical_address):
        return self.local_start + (physical_address - self.physical_start)


class ChannelDefinition(object):
    """A sparse HEX image together with its calibration / swap description.

    @ivar name       channel name, used verbatim in every provenance record
    @ivar hex_data   :class:`intelhex.IntelHex` holding the sparse bytes
    @ivar regions    list of :class:`CalibrationRegion`
    @ivar swap       mapping ``physical_address -> swapped local address``
                     for the board address range where channels are swapped
    """

    def __init__(self, name, hex_data, regions, swapped_ranges=None):
        self.name = name
        self.hex_data = hex_data
        self.regions = self._normalise_regions(regions)
        self.swap = self._build_swap_map(swapped_ranges)
        self._region_index = self._build_region_index()

    @staticmethod
    def _normalise_regions(regions):
        normalised = []
        for region in regions:
            if isinstance(region, CalibrationRegion):
                normalised.append(region)
            else:
                physical_start, local_start, length = region[:3]
                name = region[3] if len(region) > 3 else 'region'
                normalised.append(
                    CalibrationRegion(
                        physical_start, local_start, length, name
                    )
                )
        normalised.sort(key=lambda region: region.physical_start)
        for earlier, later in zip(normalised[:-1], normalised[1:]):
            if later.physical_start < earlier.physical_end:
                raise ValueError(
                    'Calibration regions of %r overlap at physical '
                    'address 0x%X' % (later.name, later.physical_start)
                )
        return normalised

    @staticmethod
    def _build_swap_map(swapped_ranges):
        swap = {}
        if not swapped_ranges:
            return swap
        for physical_start, local_start, length in swapped_ranges:
            for offset in range(length):
                swap[physical_start + offset] = local_start + offset
        return swap

    def _build_region_index(self):
        index = []
        for region in self.regions:
            for offset in range(region.length):
                index.append(region.physical_start + offset)
        return index

    def physical_addresses(self):
        """All physical addresses covered by this definition, in order."""
        return list(self._region_index)

    def physical_claims(self):
        """Map every exported byte to its physical address claim.

        Only bytes actually present in the sparse HEX image are claims:
        a calibrated but unpopulated address does not claim ownership.

        @return  dict ``physical_address -> (local_address, region)``
        """
        claims = {}
        for physical_address in self._region_index:
            local_address, region = self.resolve(physical_address)
            if local_address in self.hex_data:
                claims[physical_address] = (local_address, region)
        return claims

    def resolve(self, physical_address):
        """Resolve a physical address to a local image address.

        @return  tuple ``(local_address, region)``; the swapped mapping, when
                 present, takes precedence over the plain calibration.
        """
        if physical_address in self.swap:
            local_address = self.swap[physical_address]
            region = self._find_region(physical_address)
            if region is None:
                raise KeyError(
                    'Swap entry of %s at physical address 0x%X lies outside '
                    'every calibration region'
                    % (self.name, physical_address)
                )
            return local_address, region
        region = self._find_region(physical_address)
        if region is None:
            raise KeyError(
                '%s has no calibration region for physical address 0x%X'
                % (self.name, physical_address)
            )
        return region.to_local(physical_address), region

    def _find_region(self, physical_address):
        for region in self.regions:
            if region.contains(physical_address):
                return region
        return None

    def byte_at(self, physical_address):
        """Return the stored byte for a physical address, or ``None``.

        ``None`` means the byte is genuinely absent from the sparse image.
        A stored byte equal to the padding value is returned as that value,
        so a measured fill byte is never confused with a missing byte.
        """
        local_address, _ = self.resolve(physical_address)
        if local_address in self.hex_data:
            return self.hex_data[local_address]
        return None

    def provenance(self, physical_address):
        """Build the provenance record of one physical byte."""
        local_address, region = self.resolve(physical_address)
        return self._provenance(physical_address, local_address, region)

    def provenance_or_none(self, physical_address):
        """Like :meth:`provenance` but return ``None`` outside all regions."""
        try:
            local_address, region = self.resolve(physical_address)
        except KeyError:
            return None
        return self._provenance(physical_address, local_address, region)

    def _provenance(self, physical_address, local_address, region):
        swapped = physical_address in self.swap
        return ByteProvenance(
            channel=self.name,
            physical_address=physical_address,
            local_address=local_address,
            region=region.name,
            swapped=swapped,
            present=local_address in self.hex_data,
            value=self.hex_data[local_address]
            if local_address in self.hex_data else None,
        )


class ByteProvenance(object):
    """Origin information for a single physical byte."""

    __slots__ = (
        'channel', 'physical_address', 'local_address', 'region',
        'swapped', 'present', 'value',
    )

    def __init__(self, channel, physical_address, local_address, region,
                 swapped, present, value):
        self.channel = channel
        self.physical_address = physical_address
        self.local_address = local_address
        self.region = region
        self.swapped = swapped
        self.present = present
        self.value = value

    def __repr__(self):
        state = 'present=0x%02X' % self.value if self.present else 'missing'
        swapped = ' swapped' if self.swapped else ''
        return (
            '<ByteProvenance %s physical=0x%X local=0x%X region=%s%s %s>'
            % (self.channel, self.physical_address, self.local_address,
               self.region, swapped, state)
        )


class Sample(object):
    """One 16-bit measurement word with full byte-level provenance."""

    __slots__ = (
        'index', 'address', 'word', 'high_present', 'low_present',
        'high', 'low',
    )

    def __init__(self, index, address, high_provenance, low_provenance):
        self.index = index
        self.address = address
        self.high = high_provenance
        self.low = low_provenance
        self.high_present = high_provenance.present
        self.low_present = low_provenance.present
        high_byte = high_provenance.value if high_provenance.present else 0
        low_byte = low_provenance.value if low_provenance.present else 0
        self.word = (high_byte << 8) | low_byte

    @property
    def complete(self):
        """True only when both bytes are present in the sparse images."""
        return self.high_present and self.low_present

    @property
    def high_missing(self):
        """True when the high byte is genuinely absent."""
        return not self.high_present

    def __repr__(self):
        flags = []
        if self.high_missing:
            flags.append('high-missing')
        if not self.low_present:
            flags.append('low-missing')
        flag_text = (' [%s]' % ','.join(flags)) if flags else ''
        return (
            'Sample(index=%d, address=0x%X, word=0x%04X%s)'
            % (self.index, self.address, self.word, flag_text)
        )


class WordTable(object):
    """Ordered, traceable table of 16-bit samples."""

    def __init__(self, samples, high_definition, low_definition,
                 high_fill_value=0xFF, low_fill_value=0xFF):
        self.samples = list(samples)
        self.high_definition = high_definition
        self.low_definition = low_definition
        self.high_fill_value = high_fill_value
        self.low_fill_value = low_fill_value

    def __iter__(self):
        return iter(self.samples)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        return self.samples[index]

    def words(self):
        """All assembled words; missing bytes contribute a zero fill."""
        return [sample.word for sample in self.samples]

    def complete_samples(self):
        return [sample for sample in self.samples if sample.complete]

    def incomplete_samples(self):
        return [sample for sample in self.samples if not sample.complete]

    def high_fill_measured_samples(self):
        """Samples whose measured high byte really equals the fill value."""
        return [
            sample for sample in self.samples
            if sample.high_present
            and sample.high.value == self.high_fill_value
        ]

    def low_fill_measured_samples(self):
        """Samples whose measured low byte really equals the fill value."""
        return [
            sample for sample in self.samples
            if sample.low_present
            and sample.low.value == self.low_fill_value
        ]

    def high_missing_samples(self):
        """Samples whose high byte is genuinely absent."""
        return [sample for sample in self.samples
                if not sample.high_present]

    def low_missing_samples(self):
        """Samples whose low byte is genuinely absent."""
        return [sample for sample in self.samples
                if not sample.low_present]

    def to_trace_rows(self):
        """Return plain dictionaries suitable for logs / CSV / reports."""
        rows = []
        for sample in self.samples:
            rows.append({
                'index': sample.index,
                'address': sample.address,
                'word': sample.word,
                'high_present': sample.high_present,
                'high_value': sample.high.value,
                'high_channel': sample.high.channel,
                'high_local_address': sample.high.local_address,
                'high_region': sample.high.region,
                'high_swapped': sample.high.swapped,
                'low_present': sample.low_present,
                'low_value': sample.low.value,
                'low_channel': sample.low.channel,
                'low_local_address': sample.low.local_address,
                'low_region': sample.low.region,
                'low_swapped': sample.low.swapped,
            })
        return rows


def channel_from_hex_file(name, path, regions, swapped_ranges=None):
    """Load a sparse HEX file into a :class:`ChannelDefinition`.

    @param name             channel name used in every provenance record
    @param path             path or readable file object of the HEX file
    @param regions          calibration regions (see ChannelDefinition)
    @param swapped_ranges   optional channel swap ranges
    """
    return ChannelDefinition(
        name=name,
        hex_data=IntelHex(path),
        regions=regions,
        swapped_ranges=swapped_ranges,
    )


def build_word_table(high_definition, low_definition,
                     word_physical_start=None, word_count=None,
                     high_fill_value=0xFF, low_fill_value=0xFF):
    """Assemble a traceable 16-bit word table from two channel definitions.

    Words occupy consecutive physical addresses; word ``i`` uses the low byte
    at ``word_physical_start + 2*i`` and the high byte at ``+ 2*i + 1``.  Two
    calibration regions may meet exactly on a word boundary, so a word can
    legitimately span two regions; every byte keeps its own region provenance.

    @param high_definition       :class:`ChannelDefinition` of high bytes
    @param low_definition        :class:`ChannelDefinition` of low bytes
    @param word_physical_start   first physical byte address of the table.
                                 Defaults to the lowest covered address.
    @param word_count            number of words to assemble.  Defaults to
                                 covering the full union of both definitions.
    @param high_fill_value       value used to flag measured fill bytes
    @param low_fill_value        value used to flag measured fill bytes

    @raise DuplicateByteError if both definitions own the same physical byte;
           the error lists every conflict and every affected sample.
    """
    _detect_duplicate_bytes(high_definition, low_definition)

    covered = _covered_physical_range(
        high_definition, low_definition, word_physical_start
    )
    start, physical_end = covered
    if word_count is None:
        if (physical_end - start) % 2:
            raise ValueError(
                'Covered physical range 0x%X..0x%X does not end on a word '
                'boundary; specify word_count explicitly.'
                % (start, physical_end - 1)
            )
        count = (physical_end - start) // 2
    else:
        count = word_count

    samples = []
    for index in range(count):
        low_address = start + 2 * index
        high_address = low_address + 1
        low_provenance = low_definition.provenance_or_none(low_address)
        high_provenance = high_definition.provenance_or_none(high_address)
        if low_provenance is None:
            low_provenance = _uncovered_provenance(
                low_definition.name, low_address)
        if high_provenance is None:
            high_provenance = _uncovered_provenance(
                high_definition.name, high_address)
        samples.append(
            Sample(index, low_address, high_provenance, low_provenance)
        )

    return WordTable(
        samples, high_definition, low_definition,
        high_fill_value=high_fill_value,
        low_fill_value=low_fill_value,
    )


def _detect_duplicate_bytes(high_definition, low_definition):
    def claim_map(definition):
        mapping = {}
        for physical_address, (local_address, region) \
                in definition.physical_claims().items():
            mapping[physical_address] = (
                definition.name, region.name, local_address
            )
        return mapping

    high_claims = claim_map(high_definition)
    low_claims = claim_map(low_definition)
    conflict_addresses = sorted(set(high_claims) & set(low_claims))
    if not conflict_addresses:
        return

    conflicts = [
        (address, high_claims[address], low_claims[address])
        for address in conflict_addresses
    ]
    affected_samples = _samples_touching_addresses(
        conflict_addresses, high_definition, low_definition
    )
    raise DuplicateByteError(conflicts, affected_samples)


def _samples_touching_addresses(conflict_addresses,
                                high_definition, low_definition):
    start, _ = _covered_physical_range(
        high_definition, low_definition, None
    )
    samples = []
    for base in sorted({address & ~1 for address in conflict_addresses}):
        low_provenance = low_definition.provenance_or_none(base)
        high_provenance = high_definition.provenance_or_none(base + 1)
        if low_provenance is None:
            low_provenance = _uncovered_provenance(
                low_definition.name, base)
        if high_provenance is None:
            high_provenance = _uncovered_provenance(
                high_definition.name, base + 1)
        samples.append(
            Sample((base - start) // 2, base,
                   high_provenance, low_provenance)
        )
    return samples


def _uncovered_provenance(channel, physical_address):
    return ByteProvenance(
        channel=channel,
        physical_address=physical_address,
        local_address=None,
        region='<uncovered>',
        swapped=False,
        present=False,
        value=None,
    )


def _covered_physical_range(high_definition, low_definition, explicit_start):
    addresses = (
        list(high_definition.physical_claims())
        + list(low_definition.physical_claims())
    )
    if not addresses:
        raise ValueError('Both channel definitions are empty.')
    minimum = min(addresses)
    maximum = max(addresses)
    if explicit_start is None:
        # Word bases are even; the lowest claimed byte may itself be a high
        # byte sitting at an odd address, so align down to the word base.
        start = minimum & ~1
    else:
        start = explicit_start
        if start > minimum:
            raise ValueError(
                'word_physical_start 0x%X skips covered bytes from 0x%X'
                % (start, minimum)
            )
    return start, maximum + 1
