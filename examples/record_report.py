#!/usr/bin/env python
"""Summarise recording appearances from retrieved MusicBrainz data.

The script reads release and recording XML responses that were already
retrieved from the web service, so it never makes a network request.

    $ ./record_report.py release.xml recording-relations.xml
    track positions: 13
    independent recordings: 7
    evidenced remaster versions: 3

Pass ``--json`` for the complete report including every counted position and
the formal relationships used as evidence.
"""
from __future__ import print_function
from __future__ import unicode_literals
import sys

from musicbrainzngs import report


def main():
    return report.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
