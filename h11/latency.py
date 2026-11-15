"""Public entry point for :mod:`h11` latency-bound helpers.

The implementation lives in :mod:`h11._latency`; importing it here keeps the
public import path readable::

    from h11.latency import ByteChunk, analyze_latency
"""

from ._latency import (
    REQUEST,
    RESPONSE,
    Boundary,
    ByteChunk,
    Chunk,
    InterimResponse,
    LatencyAnalyzer,
    LatencyCalculator,
    LatencyRange,
    LatencyTracker,
    ObservationWindow,
    ObservedChunk,
    RequestLatency,
    RequestLatencyEstimate,
    TimeWindow,
    analyze_latency,
    compute_latency_bounds,
)

__all__ = [
    "REQUEST",
    "RESPONSE",
    "Boundary",
    "ByteChunk",
    "Chunk",
    "InterimResponse",
    "LatencyAnalyzer",
    "LatencyCalculator",
    "LatencyRange",
    "LatencyTracker",
    "ObservationWindow",
    "ObservedChunk",
    "RequestLatency",
    "RequestLatencyEstimate",
    "TimeWindow",
    "analyze_latency",
    "compute_latency_bounds",
]
