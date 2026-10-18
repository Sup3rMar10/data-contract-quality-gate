"""Read batches (CSV / JSONL / JSON array), reference columns and history. Unreadable or
ragged input raises InputError (exit 2); rows are never silently skipped."""
import csv
import json

from .contract import InputError


def load_rows(path):
    """Return (rows, coerce). CSV values are strings (coerce=True), empty string means null."""
    p = str(path).lower()
    try:
        if p.endswith(".csv"):
            with open(path, encoding="utf-8-sig", newline="") as f:
                data = list(csv.reader(f))
            if not data:
                raise InputError(f"{path}: empty file, no header")
            head = data[0]
            if len(set(head)) != len(head):
                raise InputError(f"{path}: duplicate column names in header")
            rows = []
            for n, rec in enumerate(data[1:], start=2):
                if len(rec) != len(head):
                    raise InputError(f"{path}: line {n} has {len(rec)} fields, header has {len(head)}")
                rows.append({h: (v if v != "" else None) for h, v in zip(head, rec)})
            return rows, True
        if p.endswith((".jsonl", ".ndjson")):
            rows = []
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, start=1):
                    if line.strip():
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            raise InputError(f"{path}: line {n} is not valid JSON")
        elif p.endswith(".json"):
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
        else:
            raise InputError(f"{path}: unsupported extension (use .csv, .jsonl, .json)")
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise InputError(f"cannot read {path}: {e}")
    except ValueError as e:
        if isinstance(e, InputError):
            raise
        raise InputError(f"{path}: invalid JSON ({e})")
    if not isinstance(rows, list):
        raise InputError(f"{path}: expected a list of rows")
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise InputError(f"{path}: row {i} is not an object")
    return rows, False


def load_ref_values(path, column):
    rows, _ = load_rows(path)
    if rows and column not in rows[0]:
        raise InputError(f"{path}: reference column {column!r} not found")
    return {str(r[column]) for r in rows if r.get(column) is not None}


def load_json(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise InputError(f"cannot read {what} {path}: {e}")
