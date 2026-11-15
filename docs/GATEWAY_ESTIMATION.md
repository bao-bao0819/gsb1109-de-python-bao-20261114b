Gateway archive traffic estimation
==================================

This package estimates the original total response-body traffic of a gateway
from two sampled HTTP archives.

Design
------

The gateway writes *complete HTTP transactions*, not individual response
lines.  Each transaction is identified by a stable id supplied by the archive
(or, if absent, by a SHA-256 digest of its raw request bytes).  The same
transaction may be selected by both routes, so the two archives must be
deduplicated by that id before estimation.

There are two independent sampling frames:

* a **path frame**, which selects a transaction according to its parsed
  request path;
* a **status frame**, which selects a transaction according to its final
  response status.

For transaction ``i`` let

* ``p_path_i`` be its path-frame inclusion probability;
* ``p_status_i`` be its status-frame inclusion probability;
* ``p_both_i`` be the probability that both frames select it.

When the two routes sample independently, ``p_both_i = p_path_i * p_status_i``.
If the archives share randomness, supply a joint schedule explicitly.
The union inclusion probability is

    pi_i = p_path_i + p_status_i - p_both_i

The **Horvitz-Thompson** estimate of a class total of received response-body
bytes ``y_i`` is

    total_hat = sum_i y_i / pi_i

where the sum is over the observed union (one term per deduplicated
transaction).  Transactions are sampled independently, so the unbiased
variance estimate is

    var_hat = sum_i y_i^2 * (1 - pi_i) / pi_i^2

There are no covariance terms because different transactions are independent.
A transaction that both routes must collect has ``pi_i = 1``: it appears
exactly once in the union, gets weight one, and contributes exactly zero to
the variance.

The estimate is per class and for the overall union.  A class whose
transactions can have ``pi_i = 0`` is reported as **not estimable**, never as
zero.  Likewise, a class with no sampled transaction is not estimable; an
empty archive is not evidence of zero traffic.

Processing the wire
-------------------

Every transaction is parsed with h11.  The final status is the last
``h11.Response`` event; interim ``1xx`` responses before it are recorded but
do not determine the class.  A ``101 Switching Protocols`` is treated as the
final status when no later ``Response`` appears.

``y_i`` is the sum of the lengths of the ``h11.Data`` events actually
produced.  It is **not** the declared ``Content-Length`` and **not** the raw
size of the response bytes.  Thus a truncated body contributes only the
bytes that arrived, and chunk framing, HEAD responses, 204/304 responses, and
close-delimited HTTP/1.0 bodies follow h11's framing exactly.

Archive format
--------------

Archives may be JSON arrays, JSON-lines/NDJSON files, directories of JSON
records, or directories of paired raw wire files.

A JSON record can provide raw bytes directly::

    {
      "transaction_id": "abc123",
      "request": "GET /api/users HTTP/1.1\r\nHost: example.test\r\n\r\n",
      "response": "HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\npayload"
    }

Strings are UTF-8 encoded (falling back to latin-1), which preserves arbitrary
HTTP bodies.  For binary-unsafe data use an encoding object::

    {"request": {"base64": "R0VUIC8gSFRUUC8xLjENCg0K"},
     "response": {"hex": "485454502f312e3120323030..."}}

``base64:...`` and ``hex:...`` string prefixes are also accepted.  Records can
instead point at files relative to the record::

    {"transaction_id": "abc", "request_file": "abc.request",
     "response_file": "abc.response"}

A paired directory contains, for example, ``abc.request`` and
``abc.response``; ``.req``/``.res``, ``.resp``, and ``.http`` variants are
recognized.

Optional metadata fields ``path``, ``final_status``, and ``body_bytes`` are
checked against h11's parsed result.  A mismatch is an error, not a silent
override.  When the same transaction occurs in both archives, the raw and
parsed data must agree.

Sampling plan
-------------

A plan is a JSON object with two rate schedules and an optional joint
schedule::

    {
      "path": {
        "default": 0.25,
        "rules": [
          {"match": "prefix:/api/", "probability": 0.10},
          {"match": "/health", "probability": 1}
        ]
      },
      "status": {
        "default": 0.5,
        "rules": [
          {"match": "5xx", "probability": 1}
        ]
      },
      "joint": {"default": null}
    }

Rules are first-match-wins.  Patterns may be exact strings, glob patterns
(``*``, ``?``, ``[...]``), ``prefix:...``, or regular expressions prefixed
with ``re:``.  Status patterns may be exact codes (``404``), classes
(``4xx``), ranges (``400-499``), globs, or ``re:...``.

``joint.default: null`` means independent routes.  A number fixes
``p_both``; ``rules`` can give exact joint probabilities for a path/status
pair; ``relative`` multiplies the independent product, and
``relative_rules`` overrides it for matching pairs.

Use exact fractions such as ``"1/3"`` for probabilities that must stay exact.
Certainty probabilities become integer weights and zero variance.

Command line
-------------

::

    python -m gateway_traffic \
        --path-archive path_archive/ \
        --status-archive status_archive/ \
        --plan plan.json

Output is a text table by default.  ``--format json`` emits the complete
machine-readable report; ``--format csv`` emits class summaries.  Add
``--transactions-output transactions.csv`` for the per-transaction table
containing the transaction identity, both marginal probabilities, the joint
probability, the union probability, and the weight.

The text output includes transaction identities and all probability columns.
The JSON output contains the same data under ``transactions``.
