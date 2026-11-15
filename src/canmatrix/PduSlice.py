# -*- coding: utf-8 -*-
import typing

if typing.TYPE_CHECKING:
    from canmatrix.Frame import Frame
    from canmatrix.Pdu import Pdu

import attr


@attr.s
class PduSlice(object):
    """
    One PDU occurrence sliced out of a captured container-frame payload.

    Each slice is a single "row" for ingestion: it points back to the outer
    frame, keeps the occurrence order inside that frame and the raw byte
    range of the PDU in the original payload.
    """

    frame = attr.ib()  # type: Frame
    sequence = attr.ib()  # type: int
    pdu_id = attr.ib()  # type: int
    # the dictionary Pdu, or None if the pdu id is unknown
    pdu = attr.ib(default=None)  # type: typing.Optional[Pdu]
    # byte offset of this PDU (its header) in the frame payload
    start_byte = attr.ib(default=0)  # type: int
    # byte offset of this PDU's payload in the frame payload
    payload_start_byte = attr.ib(default=0)  # type: int
    # byte offset (exclusive) of the end of this PDU's payload
    end_byte = attr.ib(default=0)  # type: int
    # raw payload fragment of this PDU
    raw = attr.ib(default=b"")  # type: bytes
