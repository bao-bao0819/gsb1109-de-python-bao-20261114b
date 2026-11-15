# -*- coding: utf-8 -*-
import typing

import attr

from canmatrix.PduSlice import PduSlice


@attr.s
class PduContainerSplit(object):
    """
    Result of splitting a captured container-frame payload into PDU slices.

    `records` holds the trustworthy prefix of PDU slices (one per PDU
    occurrence, in order of appearance). `truncated_at` is the byte offset
    in the frame payload from which the remainder is untrusted (because a
    PDU header there declared a length crossing the end of the frame), or
    None if the whole payload was consumed. Bytes at or after `truncated_at`
    must not be re-scanned for further PDUs.
    """

    records = attr.ib(factory=list)  # type: typing.MutableSequence[PduSlice]
    truncated_at = attr.ib(default=None)  # type: typing.Optional[int]
