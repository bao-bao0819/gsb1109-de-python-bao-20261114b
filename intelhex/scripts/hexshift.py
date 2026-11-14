#!/usr/bin/env python

# Copyright (c) 2011-2018 Alexander Belchenko
# All rights reserved.
#
# Redistribution and use in source and binary forms,
# with or without modification, are permitted provided
# that the following conditions are met:
#
# * Redistributions of source code must retain
#   the above copyright notice, this list of conditions
#   and the following disclaimer.
# * Redistributions in binary form must reproduce
#   the above copyright notice, this list of conditions
#   and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# * Neither the name of the author nor the names
#   of its contributors may be used to endorse
#   or promote products derived from this software
#   without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING,
# BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
# AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY,
# OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
# AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Shift-aware diff of 2 hex files: confirm block moves of user-specified
regions and report the bytes that really changed after the moves.
"""

VERSION = '2.3.0'

USAGE = '''hexshift: shift-aware diff of 2 hex files.
Usage:
    python hexshift.py [options] OLD_FILE NEW_FILE

Options:
    -h, --help              this help message.
    -v, --version           version info.
    -r, --region START-END  movable region in OLD_FILE as hex addresses
                            (END inclusive); may be given multiple times.
    -f, --flag HEXBYTES     flag byte pattern (hex) proving a move when it
                            matches at the shifted location; may be given
                            multiple times.
    -o, --output FILE       write OLD_FILE with confirmed changes applied
                            to FILE (Intel HEX format).

A move of a region is confirmed when it is proven by flag words or by a
unique exact match of the whole region content (holes included). Repeated
constant data, no matter how long, cannot prove a move by itself.
'''

import sys


def _parse_region(s):
    """Parse 'START-END' hex address range, END inclusive."""
    parts = s.split('-')
    if len(parts) != 2:
        raise ValueError('bad region: %r (expected START-END)' % s)
    start, end = (int(p, 16) for p in parts)
    if start > end:
        raise ValueError('bad region: %r (START > END)' % s)
    return start, end


def _parse_flag(s):
    """Parse flag byte pattern from hex string."""
    s = s.replace(' ', '').replace('0x', '').replace('0X', '')
    if not s:
        raise ValueError('empty flag pattern')
    return bytes(bytearray.fromhex(s))


def _fmt_addr(addr):
    return '0x%08X' % addr


def _fmt_offset(offset):
    if offset < 0:
        return '-0x%X' % -offset
    return '+0x%X' % offset


def print_report(result, tofile=None):
    if tofile is None:
        tofile = sys.stdout
    w = tofile.write
    w('Confirmed shifts:\n')
    if result.confirmed:
        for shift in result.confirmed:
            w('  %s-%s -> %s-%s (shift %s)' % (
                _fmt_addr(shift.start), _fmt_addr(shift.end),
                _fmt_addr(shift.new_start), _fmt_addr(shift.new_end),
                _fmt_offset(shift.offset)))
            if shift.anchors:
                w(', anchored by flag(s) %s' % ', '.join(
                    f.hex().upper() for f in shift.anchors))
            else:
                w(', anchored by unique content')
            changed = shift.changed + shift.missing + shift.added
            if changed:
                w(', %d byte(s) differ after shift' % changed)
            w('\n')
    else:
        w('  (none)\n')
    w('Ambiguous regions (match not unique or not provable):\n')
    if result.ambiguous:
        for amb in result.ambiguous:
            w('  %s-%s: %s' % (_fmt_addr(amb.start), _fmt_addr(amb.end),
                               amb.reason))
            if amb.candidates:
                w(', candidate shift(s): %s' % ', '.join(
                    _fmt_offset(o) for o in amb.candidates))
            w('\n')
    else:
        w('  (none)\n')
    if result.not_found:
        w('Regions not found in new file:\n')
        for start, end in result.not_found:
            w('  %s-%s\n' % (_fmt_addr(start), _fmt_addr(end)))
    w('Residual differences after confirmed shifts:\n')
    if result.residual:
        for change in result.residual:
            def fmt(b):
                return '(hole)' if b is None else '0x%02X' % b
            w('  %s: %s -> %s\n' % (_fmt_addr(change.addr),
                                    fmt(change.old), fmt(change.new)))
    else:
        w('  (none)\n')
    violations = result.verify()
    if violations:
        w('Verification FAILED at %d certain address(es), first: %s\n' % (
            len(violations), ', '.join(_fmt_addr(a) for a in violations[:8])))
    else:
        w('Verification OK: old file + confirmed changes matches new file '
          'byte-for-byte at all certain addresses (holes included)\n')
    return violations


def main(argv=None):
    import getopt

    if argv is None:
        argv = sys.argv[1:]
    try:
        opts, args = getopt.gnu_getopt(argv, 'hvr:f:o:',
            ['help', 'version', 'region=', 'flag=', 'output='])

        regions = []
        flags = []
        output = None
        for o, a in opts:
            if o in ('-h', '--help'):
                print(USAGE)
                return 0
            elif o in ('-v', '--version'):
                print(VERSION)
                return 0
            elif o in ('-r', '--region'):
                regions.append(_parse_region(a))
            elif o in ('-f', '--flag'):
                flags.append(_parse_flag(a))
            elif o in ('-o', '--output'):
                output = a

    except getopt.GetoptError:
        e = sys.exc_info()[1]     # current exception
        sys.stderr.write(str(e)+"\n")
        sys.stderr.write(USAGE+"\n")
        return 1
    except ValueError:
        e = sys.exc_info()[1]     # current exception
        sys.stderr.write("ERROR: %s\n" % e)
        sys.stderr.write(USAGE+"\n")
        return 1

    if len(args) != 2:
        sys.stderr.write("ERROR: You should specify 2 files to compare.\n")
        sys.stderr.write(USAGE+"\n")
        return 1

    fname1, fname2 = args

    from intelhex import IntelHex, shift_diff
    ih_old = IntelHex(fname1)
    ih_new = IntelHex(fname2)
    result = shift_diff(ih_old, ih_new, regions=regions, flags=flags)
    violations = print_report(result)

    if output:
        result.apply().tofile(output, format='hex')

    if violations:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
