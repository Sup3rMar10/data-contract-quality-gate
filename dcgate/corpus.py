"""Seeded synthetic corpus: fictional 'orders' batches with background noise and injected faults.
No real data; ids are made up (ORD-nnnnnn, CUST-nnnn, SAVEnn)."""
import random
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
CUSTOMERS = {f"CUST-{i:04d}" for i in range(1, 501)}
CUST_LIST = sorted(CUSTOMERS)

ORDERS_CONTRACT = {
    "dataset": "orders",
    "columns": {
        "order_id": {"type": "string", "unique": True, "pattern": "ORD-[0-9]{6}"},
        "customer_id": {"type": "string", "pattern": "CUST-[0-9]{4}",
                        "references": {"dataset": "customers", "column": "customer_id"}},
        "amount": {"type": "number", "min": 0.01, "max": 5000},
        "quantity": {"type": "integer", "min": 1, "max": 50},
        "status": {"type": "string", "allowed": ["new", "paid", "shipped", "cancelled"]},
        "coupon_code": {"type": "string", "nullable": True, "pattern": "SAVE[0-9]{2}"},
        "created_at": {"type": "timestamp"},
    },
    "freshness": {"column": "created_at", "max_age_hours": 26},
    "volume": {"min_rows": 300, "max_rows": 1200},
}

_STATUS = ["new", "paid", "paid", "paid", "shipped", "shipped", "cancelled"]
_fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_batch(rng, now=NOW, n=None):
    """One healthy batch. Background noise (a real pipeline is never perfectly clean):
    0.3% out-of-range amounts, 0.1% repeated order ids, 0.2% unknown customers."""
    n = n or max(350, int(rng.gauss(600, 40)))
    end = now - timedelta(hours=rng.uniform(1, 11))
    base = rng.randrange(0, 900000)
    rows = []
    for i in range(n):
        row = {
            "order_id": f"ORD-{base + i:06d}",
            "customer_id": rng.choice(CUST_LIST),
            "amount": round(min(4999.0, max(1.0, rng.lognormvariate(4.2, 0.8))), 2),
            "quantity": rng.randint(1, 5),
            "status": rng.choice(_STATUS),
            "coupon_code": f"SAVE{rng.randint(10, 30)}" if rng.random() > 0.10 else None,
            "created_at": _fmt(end - timedelta(seconds=rng.uniform(0, 86400))),
        }
        if rng.random() < 0.003:
            row["amount"] = round(rng.uniform(5001, 9000), 2)
        if i and rng.random() < 0.001:
            row["order_id"] = rows[-1]["order_id"]
        if rng.random() < 0.002:
            row["customer_id"] = f"CUST-9{rng.randint(0, 999):03d}"
        rows.append(row)
    return rows


def _pick(rng, rows, frac):
    return rng.sample(range(len(rows)), max(1, int(frac * len(rows))))


def f_null_spike(rows, rng, sev):
    live = [i for i, r in enumerate(rows) if r["coupon_code"] is not None]
    for i in rng.sample(live, int((0.02 + 0.28 * sev) * len(rows))):
        rows[i]["coupon_code"] = None
    return rows


def f_duplicate_keys(rows, rng, sev):
    for i in _pick(rng, rows, 0.002 + 0.098 * sev):
        rows[i]["order_id"] = rows[rng.randrange(len(rows))]["order_id"]
    return rows


def f_type_drift(rows, rng, sev):
    for i in _pick(rng, rows, 0.002 + 0.998 * sev):
        rows[i]["amount"] = str(rows[i]["amount"])
    return rows


def f_orphan_fk(rows, rng, sev):
    for i in _pick(rng, rows, 0.003 + 0.147 * sev):
        rows[i]["customer_id"] = f"CUST-9{rng.randint(0, 999):03d}"
    return rows


def f_stale(rows, rng, sev):
    d = timedelta(hours=2 + 98 * sev)
    for r in rows:
        r["created_at"] = _fmt(datetime.fromisoformat(r["created_at"]) - d)
    return rows


def f_volume_drop(rows, rng, sev):
    keep = 0.15 + 0.80 * (1 - sev)
    return [rows[i] for i in sorted(rng.sample(range(len(rows)), int(keep * len(rows))))]


def f_column_removed(rows, rng, sev):
    for r in rows:
        del r["status"]
    return rows


def f_column_added(rows, rng, sev):
    for r in rows:
        r["promo_flag"] = rng.random() < 0.5
    return rows


def f_dist_shift(rows, rng, sev):
    """Amounts scaled 1.2x-1.8x: every value stays inside the allowed range, so no rule fires."""
    k = 1.2 + 0.6 * sev
    for r in rows:
        r["amount"] = round(min(4999.0, r["amount"] * k), 2) if r["amount"] <= 5000 else r["amount"]
    return rows


# name -> (injector, expected failure-code prefixes; "" = any failure, "warn:" = warning only)
FAULTS = {
    "null_spike": (f_null_spike, ("null_spike",)),
    "duplicate_keys": (f_duplicate_keys, ("violation_rate:unique",)),
    "type_drift": (f_type_drift, ("schema_breaking", "violation_rate:type")),
    "orphan_fk": (f_orphan_fk, ("violation_rate:ref",)),
    "stale_data": (f_stale, ("freshness",)),
    "volume_drop": (f_volume_drop, ("volume",)),
    "column_removed": (f_column_removed, ("schema_breaking",)),
    "column_added": (f_column_added, ("warn:schema_additive",)),
    "dist_shift": (f_dist_shift, ("",)),
}


def make_batch(split, kind, i):
    """Deterministic batch for (split, kind, i); kind 'clean' or a FAULTS key. Returns (rows, severity)."""
    rng = random.Random(f"{split}-{kind}-{i}")
    rows = clean_batch(rng)
    if kind == "clean":
        return rows, None
    sev = rng.random()
    return FAULTS[kind][0](rows, rng, sev), sev
