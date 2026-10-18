"""analyze() measures a batch; judge() applies thresholds. Splitting them lets the evaluation
tune thresholds on a dev split without re-reading any rows."""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .checks import detect_drift, parse_value, validate_rows, zscore
from .contract import CannotJudge, InputError

KINDS = ("type", "null", "allowed", "range", "pattern", "unique", "ref")
WINDOW, MIN_HISTORY = 14, 5


@dataclass
class Thresholds:
    rate: dict = field(default_factory=lambda: {k: 0.01 for k in KINDS})
    null_z: float = 4.0
    volume_z: float = 3.5
    fresh_z: float = 3.5

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict) or not set(d) <= {"rate", "null_z", "volume_z", "fresh_z"}:
            raise InputError("thresholds must be an object with keys rate, null_z, volume_z, fresh_z")
        t = cls()
        if "rate" in d:
            if not isinstance(d["rate"], dict) or not set(d["rate"]) <= set(KINDS):
                raise InputError(f"threshold rate keys must be among {KINDS}")
            t.rate.update(d["rate"])
        for k in ("null_z", "volume_z", "fresh_z"):
            if k in d:
                setattr(t, k, d[k])
        nums = list(t.rate.values()) + [t.null_z, t.volume_z, t.fresh_z]
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or x != x for x in nums):
            raise InputError("threshold values must be numbers")
        return t

    def to_dict(self):
        return {"rate": dict(self.rate), "null_z": self.null_z, "volume_z": self.volume_z, "fresh_z": self.fresh_z}


@dataclass
class Analysis:
    dataset: str
    rows_in: int
    valid: list = field(default_factory=list)
    quarantined: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    by_col: dict = field(default_factory=dict)
    schema: dict = field(default_factory=dict)
    null_rates: dict = field(default_factory=dict)
    age_hours: float | None = None
    scores: dict = field(default_factory=dict)
    null_col: str | None = None
    hard: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def summary(self):
        return {"rows": self.rows_in, "null_rates": self.null_rates, "age_hours": self.age_hours}


def check_history(history):
    if history is None:
        return []
    ok = isinstance(history, list) and all(
        isinstance(h, dict) and isinstance(h.get("rows"), int) and isinstance(h.get("null_rates"), dict) for h in history)
    if not ok:
        raise InputError("history must be a list of {rows, null_rates, age_hours} objects")
    return history


def analyze(contract, rows, *, refs=None, history=None, now=None, coerce=False):
    refs = refs or {}
    for c in contract.columns.values():
        if c.ref and c.ref[0] not in refs:
            raise CannotJudge(f"column {c.name} references dataset {c.ref[0]!r} but no reference data was supplied")
    hist = check_history(history)[-WINDOW:]
    now = now or datetime.now(timezone.utc)
    n = len(rows)
    an = Analysis(contract.dataset, n)
    if n == 0:
        an.hard.append(("empty_batch", "batch has zero rows"))
        return an
    an.valid, an.quarantined, an.counts, an.by_col = validate_rows(contract, rows, refs, coerce)
    an.schema = detect_drift(contract, rows, coerce)
    if an.schema["removed"] or an.schema["retyped"]:
        an.hard.append(("schema_breaking", f"removed={an.schema['removed']} retyped={an.schema['retyped']}"))
    if an.schema["added"]:
        an.warnings.append(("schema_additive", f"new columns {an.schema['added']}"))
    if n < contract.min_rows or (contract.max_rows is not None and n > contract.max_rows):
        an.hard.append(("volume_band", f"{n} rows outside [{contract.min_rows}, {contract.max_rows}]"))
    an.null_rates = {k: sum(1 for r in rows if r.get(k) is None) / n for k, c in contract.columns.items() if c.nullable}
    if contract.fresh_col:
        ts = [p for r in rows for ok, p in [parse_value(r.get(contract.fresh_col), "timestamp", coerce)] if ok]
        if ts:
            an.age_hours = (now - max(ts)).total_seconds() / 3600
        if an.age_hours is None or an.age_hours > contract.fresh_max:
            an.hard.append(("freshness_sla", f"age {an.age_hours} h > SLA {contract.fresh_max} h"))
    for k in KINDS:
        an.scores[f"rate:{k}"] = an.counts.get(k, 0) / n
    if len(hist) < MIN_HISTORY:
        an.warnings.append(("history_insufficient", f"{len(hist)} < {MIN_HISTORY} prior batches; z-score checks skipped"))
        return an
    best = (0.0, None)
    for name, rate in an.null_rates.items():
        vals = [h["null_rates"][name] for h in hist if name in h["null_rates"]]
        if len(vals) >= MIN_HISTORY:
            z = zscore(rate, vals, floor_abs=0.005)
            if z > best[0]:
                best = (z, name)
    an.scores["null_z"], an.null_col = best
    an.scores["volume_z"] = abs(zscore(n, [h["rows"] for h in hist], floor_rel=0.02))
    ages = [h["age_hours"] for h in hist if isinstance(h.get("age_hours"), (int, float))]
    if an.age_hours is not None and len(ages) >= MIN_HISTORY:
        an.scores["fresh_z"] = zscore(an.age_hours, ages, floor_abs=0.5)
    return an


def judge(an, th):
    fails = [{"code": c, "detail": d} for c, d in an.hard]
    s = an.scores
    for k in KINDS:
        r = s.get(f"rate:{k}")
        if r is not None and r > th.rate[k]:
            fails.append({"code": f"violation_rate:{k}", "detail": f"{r:.4f} > {th.rate[k]:.4f}"})
    if s.get("null_z") is not None and s["null_z"] > th.null_z:
        fails.append({"code": f"null_spike:{an.null_col}", "detail": f"z={s['null_z']:.2f} > {th.null_z}"})
    if s.get("volume_z") is not None and s["volume_z"] > th.volume_z:
        fails.append({"code": "volume_z", "detail": f"|z|={s['volume_z']:.2f} > {th.volume_z}"})
    if s.get("fresh_z") is not None and s["fresh_z"] > th.fresh_z:
        fails.append({"code": "freshness_z", "detail": f"z={s['fresh_z']:.2f} > {th.fresh_z}"})
    warns = [{"code": c, "detail": d} for c, d in an.warnings]
    if an.quarantined and not fails:
        warns.append({"code": "quarantined_rows", "detail": f"{len(an.quarantined)} rows quarantined, under thresholds"})
    return {
        "dataset": an.dataset, "status": "fail" if fails else "pass", "exit_code": 1 if fails else 0,
        "rows_in": an.rows_in, "rows_valid": len(an.valid), "rows_quarantined": len(an.quarantined),
        "violation_row_counts": an.counts, "violations_by_column": an.by_col, "schema": an.schema,
        "null_rates": an.null_rates, "age_hours": an.age_hours,
        "scores": {k: round(v, 4) for k, v in s.items()}, "failures": fails, "warnings": warns,
    }
