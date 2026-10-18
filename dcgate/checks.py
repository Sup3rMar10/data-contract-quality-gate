"""Row validation, schema drift and the rolling-history z-score."""
import math
import re
import statistics
from collections import Counter
from datetime import datetime, timezone

_INT = re.compile(r"[+-]?[0-9]+")


def parse_ts(v):
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_value(v, t, coerce):
    """Return (ok, parsed). coerce=True means CSV input: strings are parsed to the declared type."""
    if coerce and isinstance(v, str) and t in ("integer", "number", "boolean"):
        s = v.strip()
        if t == "integer":
            return (True, int(s)) if _INT.fullmatch(s) else (False, None)
        if t == "number":
            try:
                f = float(s)
            except ValueError:
                return False, None
            return math.isfinite(f), f
        return (True, s.lower() == "true") if s.lower() in ("true", "false") else (False, None)
    if t == "string":
        return isinstance(v, str), v
    if t == "boolean":
        return isinstance(v, bool), v
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool), v
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v), v
    d = parse_ts(v)
    return d is not None, d


def _v(col, rule, detail):
    return {"column": col, "rule": rule, "detail": detail}


def validate_rows(contract, rows, refs, coerce):
    """Split rows into valid and quarantined. A quarantined row carries EVERY rule it violates."""
    valid, quarantined = [], []
    counts, by_col = Counter(), Counter()
    seen = {n: set() for n, c in contract.columns.items() if c.unique}
    for i, row in enumerate(rows):
        viol = []
        for name, col in contract.columns.items():
            v = row.get(name)
            if v is None:
                if not col.nullable:
                    viol.append(_v(name, "null", "value is null or missing"))
                continue
            ok, p = parse_value(v, col.type, coerce)
            if not ok:
                viol.append(_v(name, "type", f"expected {col.type}, got {type(v).__name__}"))
                continue
            if col.allowed is not None and p not in col.allowed:
                viol.append(_v(name, "allowed", f"{v!r} not in allowed values"))
            if col.min is not None and p < col.min:
                viol.append(_v(name, "range", f"{v!r} < min {col.min}"))
            if col.max is not None and p > col.max:
                viol.append(_v(name, "range", f"{v!r} > max {col.max}"))
            if col.pattern is not None and not col.pattern.fullmatch(p):
                viol.append(_v(name, "pattern", f"{v!r} does not match {col.pattern.pattern}"))
            if name in seen:
                if p in seen[name]:
                    viol.append(_v(name, "unique", f"duplicate value {v!r}"))
                else:
                    seen[name].add(p)
            if col.ref is not None and str(v) not in refs[col.ref[0]]:
                viol.append(_v(name, "ref", f"{v!r} not found in {col.ref[0]}.{col.ref[1]}"))
        if viol:
            quarantined.append({"row_index": i, "row": row, "violations": viol})
            for r in {x["rule"] for x in viol}:
                counts[r] += 1
            for x in viol:
                by_col[f"{x['column']}.{x['rule']}"] += 1
        else:
            valid.append(row)
    return valid, quarantined, dict(counts), dict(by_col)


_COMPAT = {"string": {"string", "timestamp"}, "integer": {"integer"}, "number": {"integer", "number"},
           "boolean": {"boolean"}, "timestamp": {"timestamp"}}


def infer(v, coerce):
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        if coerce:
            s = v.strip()
            if _INT.fullmatch(s):
                return "integer"
            if s.lower() in ("true", "false"):
                return "boolean"
            try:
                if math.isfinite(float(s)):
                    return "number"
            except ValueError:
                pass
        return "timestamp" if parse_ts(v) else "string"
    return "other"


def detect_drift(contract, rows, coerce):
    """added = in data not contract (warning); removed / retyped = breaking."""
    observed = set()
    for r in rows:
        observed.update(r)
    added = sorted(observed - set(contract.columns))
    removed = sorted(set(contract.columns) - observed)
    retyped = []
    for name, col in contract.columns.items():
        if name not in observed:
            continue
        c = Counter(infer(r[name], coerce) for r in rows if r.get(name) is not None)
        if not c:
            continue
        dom, n = c.most_common(1)[0]
        if not ((col.type == "string" and coerce) or dom in _COMPAT[col.type]):
            retyped.append({"column": name, "expected": col.type, "observed": dom, "share": round(n / sum(c.values()), 3)})
    return {"added": added, "removed": removed, "retyped": retyped}


def zscore(x, values, floor_abs=0.0, floor_rel=0.0):
    """z of x against history; the sd is floored so a constant history cannot divide by zero."""
    mean = statistics.fmean(values)
    sd = max(statistics.stdev(values), floor_abs, floor_rel * abs(mean))
    if sd <= 0:
        raise ValueError("zero sd floor")
    return (x - mean) / sd
