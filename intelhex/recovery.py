"""Recovery of missing (erased) bytes on HEX pages protected with row and
column XOR checksums.

A production metering page is a rectangular grid of bytes.  Every row ends
with a byte holding the XOR of the other bytes in that row and every column
ends with a byte holding the XOR of the other bytes in that column; the
corner cell is shared by the two global (checksum-of-checksums) equations.
Fields are laid out on the grid and the raw image read back from the device
may contain holes (addresses absent from the HEX file).

Each checksum equation is a linear equation over GF(2):

    XOR of the unknown cells of the line  =  XOR of the known cells.

The row and column equations together form one linear system whose eight
bit planes share the same coefficient matrix, so Gaussian elimination is
run once with one byte per right-hand side.  A variable is reported as
inferred only when it is forced to one value in *every* completion of the
consistent system (its reduced row contains no free variable), which is
exactly the "identical across all completions" rule.  Read values are kept
and reported separately.

When checksums contradict each other, every row/column involved in the
contradiction is reported and the offending equations are dropped before a
second elimination pass; fields that never touch a dropped equation are
recovered normally on the same page.
"""

__docformat__ = "javadoc"

__all__ = [
    'READ',
    'INFERRED',
    'FieldLayout',
    'PageLayout',
    'FieldResult',
    'RecoveryReport',
    'LayoutError',
    'recover_page',
    'recover_pages',
    'apply_recovery',
]

from intelhex.compat import range_g

#: Origin marker for a byte present in the source HEX image.
READ = 'read'
#: Origin marker for a byte uniquely reconstructed from the checksums.
INFERRED = 'inferred'


class LayoutError(ValueError):
    '''Invalid page/field layout specification.'''


class FieldLayout(object):
    '''Description of one field placed on a page.

    @ivar name:     unique field name.
    @ivar base:     absolute address of the field's first byte.
    @ivar length:   number of bytes occupied by the field.
    '''

    __slots__ = ('name', 'base', 'length')

    def __init__(self, name, base, length):
        if length <= 0:
            raise LayoutError(
                "field %r has non-positive length %d" % (name, length))
        if base < 0:
            raise LayoutError(
                "field %r has negative base address 0x%X" % (name, base))
        self.name = name
        self.base = base
        self.length = length

    def addresses(self):
        '''Addresses occupied by the field, in layout order.'''
        return range_g(self.base, self.base + self.length)

    def __repr__(self):
        return 'FieldLayout(%r, 0x%X, %d)' % (
            self.name, self.base, self.length)


class PageLayout(object):
    '''Rectangular page layout with row and column XOR checksums.

    @ivar name:               page name.
    @ivar base:               absolute address of the first grid cell.
    @ivar width:              number of columns (including the row-checksum
                              column).
    @ivar height:             number of rows (including the column-checksum
                              row).
    @ivar fields:             tuple of L{FieldLayout} placed on the page.
    @ivar row_checksum_col:   column index holding the per-row checksums.
    @ivar col_checksum_row:   row index holding the per-column checksums.
    '''

    __slots__ = ('name', 'base', 'width', 'height', 'fields',
                 'row_checksum_col', 'col_checksum_row')

    def __init__(self, name, base, width, height, fields,
                 row_checksum_col=None, col_checksum_row=None):
        if width < 2 or height < 2:
            raise LayoutError(
                "page %r must be at least 2x2, got %dx%d"
                % (name, width, height))
        if base < 0:
            raise LayoutError(
                "page %r has negative base address 0x%X" % (name, base))
        if row_checksum_col is None:
            row_checksum_col = width - 1
        if col_checksum_row is None:
            col_checksum_row = height - 1
        if not (0 <= row_checksum_col < width):
            raise LayoutError(
                "page %r: row checksum column %d outside 0..%d"
                % (name, row_checksum_col, width - 1))
        if not (0 <= col_checksum_row < height):
            raise LayoutError(
                "page %r: column checksum row %d outside 0..%d"
                % (name, col_checksum_row, height - 1))

        prepared = []
        seen_names = set()
        for field in fields:
            if not isinstance(field, FieldLayout):
                field = FieldLayout(*field)
            if field.name in seen_names:
                raise LayoutError(
                    "page %r: duplicate field name %r"
                    % (name, field.name))
            seen_names.add(field.name)
            prepared.append(field)

        self.name = name
        self.base = base
        self.width = width
        self.height = height
        self.fields = tuple(prepared)
        self.row_checksum_col = row_checksum_col
        self.col_checksum_row = col_checksum_row

        self._validate_cells()

    @property
    def endaddr(self):
        '''First address after the grid.'''
        return self.base + self.width * self.height

    def coordinates(self, addr):
        '''Grid (row, column) of an absolute address on the page.'''
        offset = addr - self.base
        return divmod(offset, self.width)

    def row_address(self, row):
        '''Absolute address of the first cell of a grid row.'''
        return self.base + row * self.width

    def column_address(self, col):
        '''Absolute address of the first cell of a grid column.'''
        return self.base + col

    def is_checksum_cell(self, row, col):
        '''Whether a grid cell stores a row/column/global checksum.'''
        return (col == self.row_checksum_col
                or row == self.col_checksum_row)

    def _validate_cells(self):
        seen = set()
        page_end = self.endaddr
        for field in self.fields:
            if field.base < self.base or field.base + field.length > page_end:
                raise LayoutError(
                    "page %r: field %r (0x%X+%d) escapes page range "
                    "0x%X..0x%X"
                    % (self.name, field.name, field.base, field.length,
                       self.base, page_end - 1))
            for addr in field.addresses():
                row, col = self.coordinates(addr)
                if self.is_checksum_cell(row, col):
                    raise LayoutError(
                        "page %r: field %r claims checksum cell at 0x%X "
                        "(row %d, col %d)"
                        % (self.name, field.name, addr, row, col))
                if addr in seen:
                    raise LayoutError(
                        "page %r: field %r overlaps at address 0x%X "
                        "(row %d, col %d) claimed by another field"
                        % (self.name, field.name, addr, row, col))
                seen.add(addr)

    def cells(self):
        '''Map every grid address to its coordinates and checksum flag.

        @return: dict mapping address to (row, col, is_checksum_cell).
        '''
        cells = {}
        for row in range_g(0, self.height):
            for col in range_g(0, self.width):
                addr = self.base + row * self.width + col
                cells[addr] = (row, col,
                               self.is_checksum_cell(row, col))
        return cells

    def __repr__(self):
        return 'PageLayout(%r, 0x%X, %d, %d, %d fields)' % (
            self.name, self.base, self.width, self.height, len(self.fields))


class FieldResult(object):
    '''Per-field outcome of a recovery run.

    @ivar name:          field name.
    @ivar read:          dict of addresses/bytes actually present in the
                         source image.
    @ivar inferred:      dict of addresses/bytes uniquely reconstructed.
    @ivar missing:       sorted addresses that remain unknown because the
                         surviving constraints do not determine them.
    @ivar affected:      True when the field touches a contradicted row or
                         column, so some checksum information had to be
                         discarded.
    '''

    __slots__ = ('name', 'read', 'inferred', 'missing', 'affected')

    def __init__(self, name):
        self.name = name
        self.read = {}
        self.inferred = {}
        self.missing = []
        self.affected = False

    @property
    def ok(self):
        '''True when every field byte is available and no checksum is bad.'''
        return not self.missing and not self.affected

    def __repr__(self):
        return ('FieldResult(%r, read=%d, inferred=%d, missing=%d, '
                'affected=%r)') % (
            self.name, len(self.read), len(self.inferred),
            len(self.missing), self.affected)


class RecoveryReport(object):
    '''Result of recovering one page.

    @ivar layout:          L{PageLayout} used for recovery.
    @ivar recovered:       L{intelhex.IntelHex} containing every byte that is
                           known afterwards (read and inferred alike).
    @ivar origins:         dict mapping every recovered address to L{READ} or
                           L{INFERRED}.
    @ivar field_results:   dict of field name to L{FieldResult}.
    @ivar bad_rows:        sorted row indices whose row XOR is contradicted.
    @ivar bad_columns:     sorted column indices whose column XOR is
                           contradicted.
    @ivar free_cells:      sorted addresses that no surviving constraint can
                           determine.
    '''

    def __init__(self, layout, recovered, origins, field_results,
                 bad_rows, bad_columns, free_cells):
        self.layout = layout
        self.recovered = recovered
        self.origins = origins
        self.field_results = field_results
        self.bad_rows = bad_rows
        self.bad_columns = bad_columns
        self.free_cells = free_cells

    @property
    def has_contradictions(self):
        '''True when at least one row/column checksum had to be discarded.'''
        return bool(self.bad_rows or self.bad_columns)

    @property
    def inferred(self):
        '''Dict of all inferred addresses/bytes, in address order.'''
        return dict((addr, self.recovered[addr])
                    for addr in self.origins
                    if self.origins[addr] == INFERRED)

    def origin(self, addr):
        '''L{READ} / L{INFERRED} for an address, or None if still unknown.'''
        return self.origins.get(addr)

    def is_inferred(self, addr):
        '''Whether an address was reconstructed rather than read.'''
        return self.origins.get(addr) == INFERRED

    def contradictions(self):
        '''Human-readable description of every contradicted checksum.'''
        lines = []
        layout = self.layout
        for row in self.bad_rows:
            lines.append(
                "page %r: row %d checksum contradicted "
                "(row starts at 0x%X, checksum column %d)"
                % (layout.name, row, layout.row_address(row),
                   layout.row_checksum_col))
        for col in self.bad_columns:
            lines.append(
                "page %r: column %d checksum contradicted "
                "(column starts at 0x%X, checksum row %d)"
                % (layout.name, col, layout.column_address(col),
                   layout.col_checksum_row))
        return lines

    def __repr__(self):
        return ('RecoveryReport(%r, inferred=%d, bad_rows=%r, '
                'bad_columns=%r)') % (
            self.layout.name,
            sum(1 for o in self.origins.values() if o == INFERRED),
            self.bad_rows, self.bad_columns)


# --------------------------------------------------------------------------
# GF(2) linear solver
# --------------------------------------------------------------------------

def _build_system(layout, cells, known, disabled):
    '''Build the row/column XOR equations of one page.

    Every equation has the form::

        XOR(mask of unknown variables) = XOR(known bytes of the line)

    @return: (system rows as [mask, rhs byte, provenance mask],
              var id -> address, address -> var id)
    '''
    unknown_addrs = sorted(addr for addr in cells if addr not in known)
    var_of_addr = dict((addr, vid)
                       for vid, addr in enumerate(unknown_addrs))
    addr_of_var = dict((vid, addr)
                       for addr, vid in var_of_addr.items())

    lines = []
    for row in range_g(0, layout.height):
        lines.append(('row', row))
    for col in range_g(0, layout.width):
        lines.append(('column', col))

    # The row and column equations share exactly one linear dependency:
    # XOR of all row equations equals XOR of all column equations, because
    # every cell occurs once on each side.  Keeping all of them makes the
    # provenance of any single corruption spread over every equation.  The
    # column equation over the row-checksum column is therefore dropped;
    # its cells are row checksums and remain covered by the row equations.
    dropped = layout.height + layout.row_checksum_col

    system = []
    for cid, (axis, index) in enumerate(lines):
        if cid in disabled or cid == dropped:
            continue
        mask = 0
        rhs = 0
        if axis == 'row':
            addrs = (layout.base + index * layout.width + col
                     for col in range_g(0, layout.width))
        else:
            addrs = (layout.base + row * layout.width + index
                     for row in range_g(0, layout.height))
        for addr in addrs:
            if addr in known:
                rhs ^= known[addr]
            else:
                mask |= 1 << var_of_addr[addr]
        system.append([mask, rhs, 1 << cid])
    return system, addr_of_var, var_of_addr


def _eliminate(system):
    '''Reduce the system to reduced row echelon form over GF(2).

    Coefficient masks and provenance masks are Python integers and the
    right-hand side is one byte, so XOR eliminates all eight bit planes and
    tracks the originating equations in a single pass.

    @return: (pivot rows as (mask, rhs, provenance) sorted by pivot bit,
              set of provenance masks of contradictory equations)
    '''
    pivot_rows = {}
    contradictions = set()
    for mask, rhs, prov in system:
        while mask:
            pivot = mask.bit_length() - 1
            pivot_row = pivot_rows.get(pivot)
            if pivot_row is None:
                pivot_rows[pivot] = [mask, rhs, prov]
                break
            mask ^= pivot_row[0]
            rhs ^= pivot_row[1]
            prov ^= pivot_row[2]
        if mask == 0 and rhs:
            contradictions.add(prov)

    # Back-substitute so every pivot column occurs in exactly one row
    # (reduced row echelon form).  Clearing pivot columns in ascending
    # order keeps each pivot row free of every lower pivot bit, so no bit
    # above the current pivot is ever reintroduced.
    ordered = sorted(pivot_rows.items())
    for position, (pivot, pivot_row) in enumerate(ordered):
        for _other_pivot, other in ordered[position + 1:]:
            if other[0] & (1 << pivot):
                other[0] ^= pivot_row[0]
                other[1] ^= pivot_row[1]
                other[2] ^= pivot_row[2]
    rows = [row for _pivot, row in ordered]
    return rows, contradictions


def _solve(layout, cells, known, disabled):
    '''Solve one page while ignoring the given constraint ids.

    Constraint ids are stable between rounds: rows take 0..height-1 and
    columns height..height+width-1 in layout order.

    @return: (inferred addr -> byte,
              set of contradiction provenance masks,
              addresses left undetermined).  Each provenance mask lists the
              constraints whose combined equations are inconsistent; a mask
              with a single bit localizes a failure to exactly one line.
    '''
    system, addr_of_var, _var_of_addr = _build_system(
        layout, cells, known, disabled)
    _rows, contradictions = _eliminate(system)

    pivot_columns = set(row[0].bit_length() - 1 for row in _rows)
    free_columns = [vid for vid in addr_of_var if vid not in pivot_columns]
    free_mask = 0
    for vid in free_columns:
        free_mask |= 1 << vid

    inferred = {}
    for mask, rhs, _prov in _rows:
        if mask & free_mask:
            # The value depends on a free variable: not identical across
            # all completions, so it must not be inferred.
            continue
        vid = mask.bit_length() - 1
        inferred[addr_of_var[vid]] = rhs

    free_addrs = sorted(addr_of_var[vid]
                        for vid in addr_of_var
                        if addr_of_var[vid] not in inferred)
    return inferred, contradictions, free_addrs


def _bits_of(mask):
    '''Iterate the set-bit positions of an integer mask.'''
    while mask:
        bit = mask & (-mask)
        yield bit.bit_length() - 1
        mask ^= bit


def _resolve_contradictions(layout, cells, known):
    '''Identify every checksum line involved in a contradiction.

    Gaussian elimination records the provenance of every equation.  A
    contradiction whose provenance is a single constraint means that line
    fails against bytes actually read (or values already proven by other
    lines); it is unambiguously bad and can be dropped.  Dropping such a
    line may expose further single-line failures, so the process is
    repeated.  A contradiction whose provenance spans several lines is an
    indistinguishable group: any one of those checksums could be corrupt
    and produce the same observations, so every involved row and column is
    reported.

    @return: set of bad constraint ids.
    '''
    disabled = set()
    while True:
        _inferred, contradictions, _free = _solve(
            layout, cells, known, disabled)
        singleton = set()
        joint = set()
        for provenance in contradictions:
            cids = set(_bits_of(provenance)) - disabled
            if len(cids) == 1:
                singleton.update(cids)
            else:
                joint.update(cids)
        if singleton:
            # Removing an unambiguously bad line can expose further
            # single-line failures; re-solve before blaming any group whose
            # attribution is still shared with other lines.
            disabled |= singleton
            continue
        return disabled | joint


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _read_known(ih, cells):
    known = {}
    for addr in cells:
        if addr in ih:
            value = ih[addr]
            if not (0 <= value <= 0xFF):
                raise LayoutError(
                    "byte at 0x%X is outside 0..255: %r" % (addr, value))
            known[addr] = value
    return known


def recover_page(ih, layout):
    '''Recover the missing bytes of one page.

    Read bytes and inferred bytes are kept separate in the returned report;
    a byte is inferred only when every completion consistent with the
    non-contradicted checksums assigns it the same value.  Contradicted row
    and column checksums are reported and discarded, while unrelated fields
    of the same page are still fully recovered.

    @param ih:      L{intelhex.IntelHex} with the raw page image; holes are
                    addresses absent from the object.
    @param layout:  L{PageLayout} describing the grid and its fields.
    @return:        L{RecoveryReport}.
    '''
    from intelhex import IntelHex

    cells = layout.cells()
    known = _read_known(ih, cells)

    bad = _resolve_contradictions(layout, cells, known)

    # Final solve with every contradicted line removed, so no inferred byte
    # can derive from a checksum that failed.
    inferred, residual, free_addrs = _solve(layout, cells, known, bad)
    if residual:
        # Defensive: removing constraints cannot normally create failures.
        for provenance in residual:
            bad.update(_bits_of(provenance))
        inferred, _residual, free_addrs = _solve(
            layout, cells, known, bad)

    bad_rows = sorted(index for cid in bad
                      if cid < layout.height for index in (cid,))
    bad_columns = sorted(cid - layout.height
                         for cid in bad if cid >= layout.height)
    bad_row_set = set(bad_rows)
    bad_col_set = set(bad_columns)

    recovered = IntelHex()
    origins = {}
    for addr, value in known.items():
        recovered[addr] = value
        origins[addr] = READ
    for addr, value in inferred.items():
        recovered[addr] = value
        origins[addr] = INFERRED

    field_results = {}
    for field in layout.fields:
        result = FieldResult(field.name)
        for addr in field.addresses():
            row, col = layout.coordinates(addr)
            if row in bad_row_set or col in bad_col_set:
                result.affected = True
            if addr in known:
                result.read[addr] = known[addr]
            elif addr in inferred:
                result.inferred[addr] = inferred[addr]
            else:
                result.missing.append(addr)
        field_results[field.name] = result

    return RecoveryReport(
        layout=layout,
        recovered=recovered,
        origins=origins,
        field_results=field_results,
        bad_rows=bad_rows,
        bad_columns=bad_columns,
        free_cells=free_addrs,
    )


def recover_pages(ih, layouts):
    '''Recover several independent pages from one HEX image.

    @param ih:      L{intelhex.IntelHex} holding the raw images.
    @param layouts: iterable of L{PageLayout}.
    @return:        list of L{RecoveryReport}, in the given layout order.
                    A contradiction on one page never affects another page.
    '''
    return [recover_page(ih, layout) for layout in layouts]


def apply_recovery(ih, report):
    '''Return a copy of a HEX image with inferred bytes filled in.

    Only inferred bytes are added; existing bytes are never overwritten and
    contradicted pages simply keep their remaining holes.

    @param ih:      source L{intelhex.IntelHex}.
    @param report:  L{RecoveryReport} produced for the same image.
    @return:        new L{intelhex.IntelHex} object.
    '''
    from intelhex import IntelHex

    repaired = IntelHex(ih)
    for addr, origin in report.origins.items():
        if origin == INFERRED and addr not in ih:
            repaired[addr] = report.recovered[addr]
    return repaired
