#!/usr/bin/env python

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

'''Intel HEX recovery utility for a lost extended address record.'''

import json
import sys

VERSION = '2.3.0'

USAGE = '''hexrecover: recover data when an extended address record is missing.
Usage:
    python hexrecover.py [options] FILE

Options:
    -h, --help                  this help message.
    -v, --version               version info.
    -p, --page=SEGMENT          possible page segment; repeatable.
    -b, --page-base=ADDRESS     possible page base address; repeatable.
    -w, --witness=ADDR:VALUE    absolute calibration byte; repeatable.
    -l, --witness-line=ADDR:LINE
                                absolute calibration line reference; repeatable.
    -t, --record-type=TYPE      lost record type: 4 (linear) or 2 (segment).
    --start-line=N              first line of the damaged data block.
    --end-line=N                line after the damaged block.
    --switch-line=N             next explicit address switch line.
    --strict                    fail when witnesses leave no feasible page.
    --json                      emit the full report as JSON.
    -o, --output=FILE           write the conservative archive as HEX.
'''

def _int(value):
    return int(value, 0)

def _pair(text, what):
    try:
        address, value = text.split(':', 1)
        return _int(address), _int(value)
    except (TypeError, ValueError):
        raise ValueError('invalid %s: %r' % (what, text))


def main(argv=None):
    import getopt

    if argv is None:
        argv = sys.argv[1:]
    pages, page_bases, witnesses = [], [], []
    record_type = None
    start_line = end_line = switch_line = None
    strict = as_json = False
    output = None
    try:
        opts, args = getopt.gnu_getopt(
            argv, 'hvp:b:w:l:t:o:',
            ['help', 'version', 'page=', 'page-base=', 'witness=',
             'witness-line=', 'record-type=', 'start-line=', 'end-line=',
             'switch-line=', 'strict', 'json', 'output='])
        for option, value in opts:
            if option in ('-h', '--help'):
                print(USAGE)
                return 0
            elif option in ('-v', '--version'):
                print(VERSION)
                return 0
            elif option in ('-p', '--page'):
                pages.append(_int(value))
            elif option in ('-b', '--page-base'):
                page_bases.append(_int(value))
            elif option in ('-w', '--witness'):
                witnesses.append(_pair(value, 'witness'))
            elif option in ('-l', '--witness-line'):
                address, line = _pair(value, 'witness line')
                witnesses.append({'address': address, 'line': line})
            elif option in ('-t', '--record-type'):
                record_type = _int(value)
            elif option == '--start-line':
                start_line = _int(value)
            elif option == '--end-line':
                end_line = _int(value)
            elif option == '--switch-line':
                switch_line = _int(value)
            elif option == '--strict':
                strict = True
            elif option == '--json':
                as_json = True
            elif option in ('-o', '--output'):
                output = value
    except getopt.GetoptError:
        error = sys.exc_info()[1]
        sys.stderr.write(str(error) + '\n')
        sys.stderr.write(USAGE + '\n')
        return 1
    except ValueError as error:
        sys.stderr.write(str(error) + '\n')
        return 1

    if len(args) != 1:
        sys.stderr.write('ERROR: specify exactly one HEX file.\n')
        sys.stderr.write(USAGE + '\n')
        return 1
    if not pages and not page_bases:
        sys.stderr.write('ERROR: give --page or --page-base at least once.\n')
        return 1

    from intelhex.recover import recover_hex, WitnessConflictError
    try:
        result = recover_hex(
            args[0], page_segments=pages or None, page_bases=page_bases or None,
            witnesses=witnesses, record_type=record_type,
            start_line=start_line, end_line=end_line, switch_line=switch_line,
            strict=strict)
    except WitnessConflictError as error:
        sys.stderr.write(str(error) + '\n')
        if error.result is not None:
            sys.stderr.write('content after the next explicit switch:\n')
            for start, end in error.result.archive.segments():
                sys.stderr.write('  0x%X-0x%X\n' % (start, end - 1))
        return 2

    if output:
        result.archive.write_hex_file(output)
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print('feasible pages: %s' % (
            ', '.join('0x%04X' % p.segment for p in result.feasible_pages)
            or 'none'))
        for page, failing, reasons in result.rejected_pages:
            print('rejected page 0x%04X: %s (%s)' % (
                page.segment,
                ', '.join(w.describe() for w in failing) or 'no witness',
                ', '.join(reasons)))
        if result.has_conflict:
            print('conflict witnesses: %s' % ', '.join(
                w.describe() for w in result.conflict_witnesses))
        for line in result.data_lines:
            print('line %s offset 0x%X: %s' % (
                line.line, line.offset,
                ', '.join('0x%X-0x%X' % (start, end - 1)
                          for start, end in line.possible_ranges) or 'none'))
        print('archived bytes: %d' % result.counts.get('archived_bytes', 0))
    return 2 if result.has_conflict else 0


if __name__ == '__main__':
    sys.exit(main())
