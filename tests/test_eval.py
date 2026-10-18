"""Corpus determinism, safety of the synthetic data, threshold tuning, and honest outcomes."""
import re
from types import SimpleNamespace

from dcgate.corpus import FAULTS, make_batch
from dcgate.evaluate import detected, run_eval, tune, wilson


def test_corpus_is_deterministic_and_splits_use_different_seeds():
    assert make_batch("dev", "orphan_fk", 3) == make_batch("dev", "orphan_fk", 3)
    assert make_batch("dev", "clean", 3)[0] != make_batch("held", "clean", 3)[0]


def test_synthetic_data_contains_no_card_ssn_or_email_shapes():
    text = " ".join(str(r) for k in ("clean", "orphan_fk", "dist_shift") for r in make_batch("dev", k, 0)[0][:200])
    assert not re.search(r"\b\d{13,19}\b", text)
    assert not re.search(r"\b\d{3}-\d{2}-\d{4}\b", text)
    assert "@" not in text


def test_tune_picks_the_99th_percentile_of_dev_clean_scores_and_ignores_faults():
    clean = [("clean", None, SimpleNamespace(scores={"rate:type": 0.0, "rate:null": 0.0, "rate:allowed": 0.0,
                                                      "rate:range": float(i), "rate:pattern": 0.0, "rate:unique": 0.0,
                                                      "rate:ref": 0.0, "null_z": i / 10, "volume_z": 1.0, "fresh_z": 1.0}))
             for i in range(100)]
    faulty = [("type_drift", 0.5, SimpleNamespace(scores={"rate:range": 1e9}))]
    th = tune(clean + faulty)
    assert th.rate["range"] == 98.0 and th.null_z == 9.8 and th.rate["type"] == 0.0


def test_detected_semantics_for_warn_only_and_any_failure_faults():
    warn_only = {"status": "pass", "failures": [], "warnings": [{"code": "schema_additive"}]}
    assert detected("column_added", warn_only)
    assert not detected("column_added", {"status": "fail", "failures": [{"code": "volume_z"}], "warnings": []})
    assert not detected("dist_shift", {"status": "pass", "failures": [], "warnings": []})
    assert wilson(0, 0) == (0.0, 0.0) and set(FAULTS) >= {"null_spike", "stale_data", "volume_drop"}


def test_small_run_catches_hard_faults_and_misses_the_distribution_shift():
    res = run_eval(60, 12)
    h = res["held"]["faults"]
    frac = lambda k: sum(x for _, x in h[k]) / len(h[k])
    assert frac("column_removed") == 1.0 and frac("type_drift") >= 0.9 and frac("stale_data") >= 0.8
    assert frac("dist_shift") <= 0.25  # nothing in the contract can see a shift inside the allowed range
    assert res["held"]["false_alarms"] / res["held"]["clean_n"] < 0.25
