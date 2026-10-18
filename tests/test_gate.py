"""Drift, anomaly detection against rolling history, and the judge."""
import random
from datetime import timedelta

import pytest

from dcgate.checks import detect_drift, zscore
from dcgate.contract import CannotJudge, InputError, parse_contract
from dcgate.corpus import CUSTOMERS, NOW, ORDERS_CONTRACT, clean_batch
from dcgate.gate import Thresholds, analyze, judge

CON = parse_contract(ORDERS_CONTRACT)
HIST = [{"rows": 600 + (i % 5) * 10, "null_rates": {"coupon_code": 0.10 + (i % 3) * 0.01},
         "age_hours": 6.0 + (i % 4) * 0.5} for i in range(14)]


def base(n=600, age=6.0):
    """Clean rows with the background noise removed, so thresholds can be tested exactly."""
    rows = [r for r in clean_batch(random.Random(11), n=n + 20)
            if r["amount"] <= 5000 and r["customer_id"] in CUSTOMERS][:n]
    seen = set()
    rows = [r for r in rows if not (r["order_id"] in seen or seen.add(r["order_id"]))]
    return set_age(rows, age)


def set_age(rows, hours):
    for r in rows:
        r["created_at"] = (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return rows


def run(rows, hist=HIST, th=None):
    return judge(analyze(CON, rows, refs={"customers": CUSTOMERS}, history=hist, now=NOW), th or Thresholds())


def codes(rep, key="failures"):
    return [f["code"] for f in rep[key]]


def test_clean_batch_passes():
    rep = run(base())
    assert rep["status"] == "pass" and rep["exit_code"] == 0 and not rep["failures"]


# ---- schema drift ----
def test_additive_column_is_a_warning_not_a_failure():
    rows = base()
    for r in rows:
        r["promo_flag"] = True
    rep = run(rows)
    assert rep["exit_code"] == 0 and "schema_additive" in codes(rep, "warnings")


def test_removed_column_is_breaking():
    rows = base()
    for r in rows:
        del r["status"]
    rep = run(rows)
    assert rep["exit_code"] == 1 and "schema_breaking" in codes(rep)


def test_retyped_column_is_breaking_and_csv_numbers_are_not_retyped():
    rows = base()
    for r in rows:
        r["amount"] = str(r["amount"])
    assert detect_drift(CON, rows, coerce=False)["retyped"][0]["column"] == "amount"
    assert "schema_breaking" in codes(run(rows))
    assert detect_drift(CON, [{**r, "quantity": str(r["quantity"])} for r in base()], coerce=True)["retyped"] == []


# ---- anomalies vs rolling history ----
def test_zscore_sd_floor_prevents_divide_by_zero():
    assert zscore(0.3, [0.1] * 10, floor_abs=0.005) == pytest.approx(40.0)
    with pytest.raises(ValueError):
        zscore(1, [1.0] * 5)


def test_short_history_warns_and_skips_z_checks():
    rep = run(base()[:420], hist=HIST[:3])
    assert "history_insufficient" in codes(rep, "warnings") and "volume_z" not in codes(rep)
    assert "volume_z" not in rep["scores"]


def test_volume_drop_inside_the_band_is_caught_by_z_and_below_band_by_band():
    assert "volume_z" in codes(run(base()[:420]))
    assert "volume_band" in codes(run(base()[:200]))
    assert run(base(610))["status"] == "pass"


def test_null_spike_in_nullable_column_is_caught_by_z():
    rows = base()
    for r in rows[: int(0.35 * len(rows))]:
        r["coupon_code"] = None
    assert "null_spike:coupon_code" in codes(run(rows))


def test_freshness_sla_and_anomaly_are_separate_checks():
    assert "freshness_sla" in codes(run(base(age=30)))
    rep = run(base(age=15))  # inside the 26 h SLA but far outside the 6-8 h history
    assert "freshness_z" in codes(rep) and "freshness_sla" not in codes(rep)


def test_rate_threshold_boundary_is_strict_greater_than():
    rows = base(500)[:400]
    for r in rows[:4]:
        r["status"] = "bogus"  # exactly 1.00% of rows
    th = Thresholds.from_dict({"rate": {"allowed": 0.01}})
    assert "violation_rate:allowed" not in codes(run(rows, th=th))
    rows[4]["status"] = "bogus"
    assert "violation_rate:allowed" in codes(run(rows, th=th))


def test_empty_batch_fails():
    rep = run([])
    assert rep["exit_code"] == 1 and codes(rep) == ["empty_batch"]


def test_missing_reference_dataset_cannot_be_judged():
    with pytest.raises(CannotJudge):
        analyze(CON, base(), refs={}, history=HIST, now=NOW)


def test_bad_thresholds_and_history_are_input_errors():
    for bad in ({"nope": 1}, {"null_z": "high"}, {"rate": {"bogus": 0.1}}):
        with pytest.raises(InputError):
            Thresholds.from_dict(bad)
    with pytest.raises(InputError):
        analyze(CON, base(), refs={"customers": CUSTOMERS}, history=[{"rows": "x"}], now=NOW)
