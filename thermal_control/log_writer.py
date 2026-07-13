"""
log_writer.py
─────────────
Generic file-logging helpers used by scheduler.py: append a row to a CSV log
(creating it, and safely migrating its schema, as needed), and stamp a
version string to disk. Each function takes its target path explicitly rather
than assuming a module-global constant, so they're directly unit-testable and
reusable by any future entry point.
"""

import csv
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_csv_schema(path: Path, new_fieldnames: list) -> None:
    """
    Roll an existing CSV log onto a new column schema: archive the file with a
    timestamp suffix, then rebuild it under the new header, replaying every
    old row (columns no longer present get dropped, new columns the old rows
    never had come out blank). Used by append_csv_row() when a record's keys
    no longer match the file's on-disk header, so a code change that adds or
    removes a logged field can't corrupt (misaligned columns) or discard the
    log's history.
    """
    archived = path.with_name(
        f"{path.stem}.{datetime.now():%Y%m%dT%H%M%S}{path.suffix}"
    )
    path.rename(archived)
    logger.warning(f"{path.name} schema changed — archived previous log to "
                    f"{archived.name}, migrating history to the new schema")

    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(archived, newline="") as src, open(tmp, "w", newline="") as dst:
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=new_fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            writer.writerow(row)
    tmp.replace(path)


def append_csv_row(path: Path, record: dict) -> None:
    """
    Append one row to a CSV log, creating the file (and its header) if
    needed. If `record`'s columns don't match an existing file's header, the
    log is migrated onto the new schema first via migrate_csv_schema() — old
    rows are preserved, not corrupted or discarded. Never raises: failures
    are logged and swallowed, since a logging failure must never break the
    caller's control loop.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(record.keys())

        if path.exists():
            with open(path, newline="") as f:
                existing_header = next(csv.reader(f), None)
            if existing_header is not None and existing_header != fieldnames:
                migrate_csv_schema(path, fieldnames)

        write_header = not path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(record)
    except Exception as exc:
        logger.warning(f"Log write failed ({path.name}): {exc}")


def write_version_file(path: Path, version: str) -> None:
    """Best-effort stamp of a version string to disk. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(version + "\n")
    except OSError as exc:
        logger.warning(f"could not write {path.name}: {exc}")
