# -*- coding: utf-8 -*-
import json

import pytest
from click.testing import CliRunner

from canmatrix.cli.status import cli_status


OLD = {"0": "待机", "1": "待机", "2": "运行"}
NEW = {"0": "待机", "1": "维护", "2": "运行"}


def write_json(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.fixture
def inputs(tmp_path):
    old = write_json(tmp_path / "old.json", OLD)
    new = write_json(tmp_path / "new.json", NEW)
    report = write_json(tmp_path / "report.json", {"待机": 180, "运行": 20})
    return old, new, report


def test_cli_prints_ranges(inputs):
    old, new, report = inputs
    result = CliRunner().invoke(
        cli_status, ["--old", old, "--new", new, "--report", report])
    assert result.exit_code == 0, result.output
    assert "待机: 0 .. 180" in result.output
    assert "维护: 0 .. 180" in result.output
    assert "运行: 20 .. 20" in result.output


def test_cli_answers_joint_question(inputs):
    old, new, report = inputs
    runner = CliRunner()
    feasible = runner.invoke(
        cli_status, ["--old", old, "--new", new, "--report", report,
                     "--ask", "维护=100", "--ask", "待机=80"])
    assert feasible.exit_code == 0, feasible.output
    assert "yes" in feasible.output

    infeasible = runner.invoke(
        cli_status, ["--old", old, "--new", new, "--report", report,
                     "--ask", "维护=100", "--ask", "待机=100"])
    assert infeasible.exit_code == 0, infeasible.output
    assert infeasible.output.strip().endswith("no")


def test_cli_json_output_is_machine_readable(inputs):
    old, new, report = inputs
    result = CliRunner().invoke(
        cli_status, ["--old", old, "--new", new, "--report", report,
                     "--ask", "维护=100", "--ask", "待机=80", "--json-output"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ranges"]["维护"] == [0, 180]
    assert payload["coexist"] is True


def test_cli_reads_sectioned_report_and_subtotals(tmp_path):
    old = write_json(tmp_path / "old.json", OLD)
    new = write_json(tmp_path / "new.json", NEW)
    report = write_json(tmp_path / "report.json",
                        {"甲区": {"待机": 100}, "乙区": {"运行": 20}})
    subtotals = write_json(tmp_path / "subtotals.json",
                           [{"partition": "甲区", "code": 1, "count": 30}])
    membership = write_json(tmp_path / "membership.json",
                            {"0": "甲区", "1": "甲区", "2": "乙区"})
    result = CliRunner().invoke(
        cli_status, ["--old", old, "--new", new, "--report", report,
                     "--subtotals", subtotals, "--membership", membership,
                     "--section", "甲区"])
    assert result.exit_code == 0, result.output
    assert "维护: 30 .. 30" in result.output
    assert "待机: 70 .. 70" in result.output
    assert "运行: 0 .. 0" in result.output


def test_cli_rejects_bad_ask(inputs):
    old, new, report = inputs
    result = CliRunner().invoke(
        cli_status, ["--old", old, "--new", new, "--report", report,
                     "--ask", "维护"])
    assert result.exit_code == 2


def test_cli_rejects_unknown_status(inputs):
    old, new, report = inputs
    result = CliRunner().invoke(
        cli_status, ["--old", old, "--new", new, "--report", report,
                     "--ask", "停机=1"])
    assert result.exit_code == 1
    assert "停机" in result.output


def test_cli_rejects_inconsistent_inputs(inputs):
    old, new, report = inputs
    result = CliRunner().invoke(
        cli_status, ["--old", old, "--new", new, "--report", report,
                     "--subtotals", write_json(
                         __import__("pathlib").Path(report).with_name("sub.json"),
                         [{"code": 1, "count": 999}])])
    assert result.exit_code == 1
    assert "cannot all hold" in result.output


def test_cli_accepts_csv_value_tables(tmp_path):
    old = tmp_path / "old.csv"
    new = tmp_path / "new.csv"
    old.write_text("原码,文字\n0,待机\n1,待机\n2,运行\n", encoding="utf-8")
    new.write_text("原码,文字\n0,待机\n1,维护\n2,运行\n", encoding="utf-8")
    report = write_json(tmp_path / "report.json", {"待机": 30})
    result = CliRunner().invoke(
        cli_status, ["--old", str(old), "--new", str(new), "--report", report])
    assert result.exit_code == 0, result.output
    assert "维护: 0 .. 30" in result.output


def test_cli_accepts_canmatrix_json_value_tables(tmp_path):
    old = write_json(tmp_path / "old.json",
                     {"value_tables": {"State": OLD}})
    new = write_json(tmp_path / "new.json",
                     {"value_tables": {"State": NEW}})
    report = write_json(tmp_path / "report.json", {"待机": 10})
    result = CliRunner().invoke(
        cli_status, ["--old", old, "--new", new, "--report", report])
    assert result.exit_code == 0, result.output
    assert "待机: 0 .. 10" in result.output
