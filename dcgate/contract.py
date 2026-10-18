"""Data contract: parse and strictly validate a JSON contract.

Any defect (unknown key, bad regex, bad type) raises ContractError, which the CLI
turns into exit code 2 ("could not judge"). A typo must never silently disable a rule.
"""
import json
import re
from dataclasses import dataclass


class ContractError(Exception):
    """The contract itself is broken."""


class InputError(Exception):
    """A data, reference, history or config file is unreadable or malformed."""


class CannotJudge(Exception):
    """The gate lacks something it needs to give a verdict."""


TYPES = ("string", "integer", "number", "boolean", "timestamp")
COL_KEYS = {"type", "nullable", "unique", "allowed", "min", "max", "pattern", "references"}
TOP_KEYS = {"dataset", "columns", "freshness", "volume"}


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = False
    unique: bool = False
    allowed: list | None = None
    min: float | None = None
    max: float | None = None
    pattern: "re.Pattern | None" = None
    ref: tuple | None = None


@dataclass
class Contract:
    dataset: str
    columns: dict
    fresh_col: str | None = None
    fresh_max: float | None = None
    min_rows: int = 1
    max_rows: int | None = None


def _num(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise ContractError(f"{what} must be a number")
    return v


def parse_contract(d):
    if not isinstance(d, dict):
        raise ContractError("contract must be a JSON object")
    extra = set(d) - TOP_KEYS
    if extra:
        raise ContractError(f"unknown contract keys: {sorted(extra)}")
    name = d.get("dataset")
    if not isinstance(name, str) or not name:
        raise ContractError("'dataset' must be a non-empty string")
    cols = d.get("columns")
    if not isinstance(cols, dict) or not cols:
        raise ContractError("'columns' must be a non-empty object")
    out = {}
    for cname, spec in cols.items():
        if not isinstance(spec, dict):
            raise ContractError(f"column {cname}: spec must be an object")
        extra = set(spec) - COL_KEYS
        if extra:
            raise ContractError(f"column {cname}: unknown keys {sorted(extra)}")
        t = spec.get("type")
        if t not in TYPES:
            raise ContractError(f"column {cname}: type must be one of {TYPES}")
        col = Column(cname, t)
        for flag in ("nullable", "unique"):
            v = spec.get(flag, False)
            if not isinstance(v, bool):
                raise ContractError(f"column {cname}: {flag} must be true or false")
            setattr(col, flag, v)
        if "allowed" in spec:
            a = spec["allowed"]
            if not isinstance(a, list) or not a:
                raise ContractError(f"column {cname}: allowed must be a non-empty list")
            col.allowed = a
        for k in ("min", "max"):
            if k in spec:
                if t not in ("integer", "number"):
                    raise ContractError(f"column {cname}: {k} needs a numeric type")
                setattr(col, k, _num(spec[k], f"column {cname}: {k}"))
        if col.min is not None and col.max is not None and col.min > col.max:
            raise ContractError(f"column {cname}: min > max")
        if "pattern" in spec:
            if t != "string":
                raise ContractError(f"column {cname}: pattern needs type string")
            try:
                col.pattern = re.compile(spec["pattern"])
            except (re.error, TypeError) as e:
                raise ContractError(f"column {cname}: bad pattern ({e})")
        if "references" in spec:
            r = spec["references"]
            ok = isinstance(r, dict) and set(r) == {"dataset", "column"} and all(isinstance(v, str) for v in r.values())
            if not ok:
                raise ContractError(f"column {cname}: references needs string 'dataset' and 'column'")
            col.ref = (r["dataset"], r["column"])
        out[cname] = col
    c = Contract(name, out)
    fr = d.get("freshness")
    if fr is not None:
        if not isinstance(fr, dict) or set(fr) != {"column", "max_age_hours"}:
            raise ContractError("freshness needs exactly 'column' and 'max_age_hours'")
        col = out.get(fr["column"])
        if col is None or col.type != "timestamp":
            raise ContractError("freshness column must be a declared timestamp column")
        c.fresh_col = fr["column"]
        c.fresh_max = _num(fr["max_age_hours"], "max_age_hours")
        if c.fresh_max <= 0:
            raise ContractError("max_age_hours must be > 0")
    vol = d.get("volume")
    if vol is not None:
        if not isinstance(vol, dict) or not set(vol) <= {"min_rows", "max_rows"}:
            raise ContractError("volume allows only 'min_rows' and 'max_rows'")
        c.min_rows = vol.get("min_rows", 1)
        c.max_rows = vol.get("max_rows")
        for v in (c.min_rows, c.max_rows):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise ContractError("volume bounds must be integers")
        if c.min_rows < 1 or (c.max_rows is not None and c.max_rows < c.min_rows):
            raise ContractError("volume band must satisfy 1 <= min_rows <= max_rows")
    return c


def load_contract(path):
    try:
        with open(path, encoding="utf-8") as f:
            return parse_contract(json.load(f))
    except (OSError, ValueError) as e:
        if isinstance(e, ContractError):
            raise
        raise ContractError(f"cannot read contract {path}: {e}")
