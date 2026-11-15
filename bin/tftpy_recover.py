#!/usr/bin/env python
# vim: ts=4 sw=4 et ai:
# -*- coding: utf8 -*-
"""Recover files from incomplete TFTP receive replicas.

Usage::

    tftpy_recover.py manifest.json -o output/
"""

import sys

from tftpy.TftpRecovery import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
