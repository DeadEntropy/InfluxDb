"""Tests for log_writer.py — generic CSV/version-file logging helpers."""

import csv

from thermal_control import log_writer as lw


# ── append_csv_row ───────────────────────────────────────────────────────────
def test_append_csv_row_writes_header_then_row(tmp_path):
    log = tmp_path / "mpc_decision_log.csv"
    lw.append_csv_row(log, {"a": 1, "b": 2})
    lw.append_csv_row(log, {"a": 3, "b": 4})
    rows = list(csv.DictReader(log.open()))
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_append_csv_row_migrates_on_schema_change(tmp_path):
    log = tmp_path / "mpc_decision_log.csv"
    lw.append_csv_row(log, {"a": 1, "b": 2})
    lw.append_csv_row(log, {"a": 1, "c": 3})   # schema changed: "b" replaced by "c"

    archived = [p for p in tmp_path.iterdir() if p != log]
    assert len(archived) == 1
    old_rows = list(csv.DictReader(archived[0].open()))
    assert old_rows == [{"a": "1", "b": "2"}]      # untouched under the old header

    new_rows = list(csv.DictReader(log.open()))
    assert new_rows == [
        {"a": "1", "c": ""},    # migrated: "b" dropped, "c" blank (never had a value)
        {"a": "1", "c": "3"},   # new row, fully populated under the new schema
    ]


# ── write_version_file ───────────────────────────────────────────────────────
def test_write_version_file(tmp_path):
    path = tmp_path / "mpc_version.txt"
    lw.write_version_file(path, "abc1234")
    assert path.read_text() == "abc1234\n"
