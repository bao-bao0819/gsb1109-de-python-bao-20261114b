"""Render analysis results as text, JSON, or CSV."""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Mapping, Optional, TextIO

from .sampling import EstimateReport


def _format_number(value: Optional[Any]) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _format_probability(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def summary_dict(report: EstimateReport, merged_counts: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    payload = report.as_dict()
    if merged_counts is not None:
        payload["archive_counts"] = dict(merged_counts)
    return payload


def render_text(
    report: EstimateReport,
    *,
    merged_counts: Optional[Mapping[str, int]] = None,
    show_transactions: bool = True,
    stream: Optional[TextIO] = None,
) -> str:
    lines: List[str] = []
    lines.append("Gateway sampled-traffic estimate")
    lines.append("")
    lines.append("Unit: response body bytes received, summed from h11 Data events")
    lines.append(f"Status grouping: {report.group_by}")
    if merged_counts:
        lines.append(
            "Archive counts: path-only={path_only}, status-only={status_only}, "
            "both={both}, deduplicated union={union}".format(**merged_counts)
        )
    lines.append("")
    header = (
        f"{'class':<8} {'observed':>9} {'certainty':>10} "
        f"{'estimated bytes':>18} {'variance':>20} {'std err':>14}  status"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for item in report.classes:
        lines.append(
            f"{item.group:<8} {item.observed_transactions:>9} "
            f"{item.certainty_transactions:>10} "
            f"{_format_number(item.estimated_bytes):>18} "
            f"{_format_number(item.variance):>20} "
            f"{_format_number(item.standard_error):>14}  "
            f"{'estimable' if item.estimable else item.reason}"
        )
    overall = report.overall
    lines.append("-" * len(header))
    lines.append(
        f"{'overall':<8} {overall.observed_transactions:>9} "
        f"{overall.certainty_transactions:>10} "
        f"{_format_number(overall.estimated_bytes):>18} "
        f"{_format_number(overall.variance):>20} "
        f"{_format_number(overall.standard_error):>14}  "
        f"{'estimable' if overall.estimable else overall.reason}"
    )
    if show_transactions:
        lines.append("")
        lines.append("Transaction identities and inclusion probabilities")
        tx_header = (
            f"{'transaction':<24} {'path':<24} {'method':<7} {'status':>6} "
            f"{'body':>10} {'route':<6} {'p_path':>10} {'p_status':>10} "
            f"{'p_both':>10} {'p_union':>10} {'weight':>10}"
        )
        lines.append(tx_header)
        lines.append("-" * len(tx_header))
        for tx in report.transactions:
            if tx.in_path_archive and tx.in_status_archive:
                route = "both"
            elif tx.in_path_archive:
                route = "path"
            else:
                route = "status"
            lines.append(
                f"{tx.transaction_id[:24]:<24} {tx.path[:24]:<24} "
                f"{(tx.method or '-'):<7} {tx.final_status:>6} {tx.body_bytes:>10} "
                f"{route:<6} {_format_probability(tx.p_path):>10} "
                f"{_format_probability(tx.p_status):>10} "
                f"{_format_probability(tx.p_both):>10} "
                f"{_format_probability(tx.p_inclusion):>10} "
                f"{_format_probability(tx.weight):>10}"
            )
    text = "\n".join(lines) + "\n"
    if stream is not None:
        stream.write(text)
    return text


def render_json(
    report: EstimateReport,
    *,
    merged_counts: Optional[Mapping[str, int]] = None,
    stream: Optional[TextIO] = None,
) -> str:
    text = json.dumps(summary_dict(report, merged_counts), indent=2, ensure_ascii=False) + "\n"
    if stream is not None:
        stream.write(text)
    return text


_CSV_FIELDS = (
    "group",
    "observed_transactions",
    "certainty_transactions",
    "estimated_bytes",
    "variance",
    "standard_error",
    "estimable",
    "zero_inclusion",
    "reason",
)


def render_csv(
    report: EstimateReport,
    *,
    merged_counts: Optional[Mapping[str, int]] = None,
    stream: Optional[TextIO] = None,
) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for item in list(report.classes) + [report.overall]:
        writer.writerow({field: getattr(item, field) for field in _CSV_FIELDS})
    text = output.getvalue()
    if stream is not None:
        stream.write(text)
    return text


def render_transactions_csv(report: EstimateReport, *, stream: Optional[TextIO] = None) -> str:
    fields = (
        "transaction_id",
        "path",
        "method",
        "final_status",
        "status_class",
        "body_bytes",
        "in_path_archive",
        "in_status_archive",
        "p_path",
        "p_status",
        "p_both",
        "p_inclusion",
        "weight",
        "contribution_bytes",
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for item in report.transactions:
        writer.writerow(item.as_dict())
    text = output.getvalue()
    if stream is not None:
        stream.write(text)
    return text
