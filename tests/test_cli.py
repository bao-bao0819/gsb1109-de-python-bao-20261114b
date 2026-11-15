import json
from pathlib import Path

import pytest

from gateway_traffic.cli import main


def write_archives(base):
    base.mkdir()
    (base / "path").mkdir()
    (base / "status").mkdir()

    def record(transaction_id, path, status, body):
        request = f"GET {path} HTTP/1.1\r\nHost: e\r\n\r\n"
        response = (
            f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n\r\n{body}"
        )
        return {"transaction_id": transaction_id, "request": request, "response": response}

    shared = record("shared", "/shared", 200, "a" * 10)
    path_only = record("path-only", "/p", 200, "b" * 20)
    status_only = record("status-only", "/missing", 404, "c" * 5)
    certain = record("certain", "/err", 500, "d" * 7)
    (base / "path" / "archive.json").write_text(
        json.dumps([shared, path_only]), encoding="utf-8"
    )
    (base / "status" / "archive.json").write_text(
        json.dumps([shared, status_only, certain]), encoding="utf-8"
    )
    (base / "plan.json").write_text(
        json.dumps(
            {
                "path": {"default": 0.5},
                "status": {
                    "default": 0.5,
                    "rules": [
                        {"match": "5xx", "probability": 1},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return base


def test_cli_json_output(tmp_path, capsys):
    archives = write_archives(tmp_path / "archives")
    code = main(
        [
            "--path-archive",
            str(archives / "path"),
            "--status-archive",
            str(archives / "status"),
            "--plan",
            str(archives / "plan.json"),
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["archive_counts"] == {
        "path_only": 1,
        "status_only": 2,
        "both": 1,
        "union": 4,
    }
    four_xx = next(item for item in payload["classes"] if item["group"] == "4xx")
    assert four_xx["estimated_bytes"] == pytest.approx(5 / 0.75)
    five_xx = next(item for item in payload["classes"] if item["group"] == "5xx")
    assert five_xx["estimated_bytes"] == 7
    assert five_xx["variance"] == 0
    # The 200 and 404 responses have pi = 0.5 + 0.5 - 0.25 = 0.75; the 500
    # is matched by both routes (pi = 1).
    assert payload["overall"]["estimated_bytes"] == pytest.approx(
        (10 + 20 + 5) / 0.75 + 7
    )
    shared = next(
        item for item in payload["transactions"] if item["transaction_id"] == "shared"
    )
    assert shared["p_inclusion"] == pytest.approx(0.75)
    certain = next(
        item for item in payload["transactions"] if item["transaction_id"] == "certain"
    )
    assert certain["p_inclusion"] == 1
    assert certain["weight"] == 1


def test_cli_requires_a_sampling_plan(tmp_path):
    archives = write_archives(tmp_path / "archives")
    with pytest.raises(SystemExit) as info:
        main(
            [
                "--path-archive",
                str(archives / "path"),
                "--status-archive",
                str(archives / "status"),
            ]
        )
    assert info.value.code == 2


def test_cli_rate_rules(tmp_path, capsys):
    archives = write_archives(tmp_path / "archives")
    code = main(
        [
            "--path-archive",
            str(archives / "path"),
            "--status-archive",
            str(archives / "status"),
            "--path-default",
            "1/2",
            "--status-default",
            "0",
            "--status-rule",
            "404",
            "1/2",
            "--status-rule",
            "5xx",
            "1",
            "--format",
            "csv",
        ]
    )
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("group,observed_transactions")
    assert any(line.startswith("5xx,") for line in lines)


def test_cli_transactions_csv(tmp_path):
    archives = write_archives(tmp_path / "archives")
    summary = tmp_path / "summary.json"
    transactions = tmp_path / "transactions.csv"
    code = main(
        [
            "--path-archive",
            str(archives / "path"),
            "--status-archive",
            str(archives / "status"),
            "--plan",
            str(archives / "plan.json"),
            "--format",
            "json",
            "--output",
            str(summary),
            "--transactions-output",
            str(transactions),
        ]
    )
    assert code == 0
    assert json.loads(summary.read_text())["metric"].startswith("response body bytes")
    header = transactions.read_text().splitlines()[0]
    assert header.split(",")[0] == "transaction_id"
