# data-contract-quality-gate

![ci](https://github.com/Sup3rMar10/data-contract-quality-gate/actions/workflows/ci.yml/badge.svg)

A stdlib-only **data contract** and **quality gate** for a pipeline batch: a JSON contract per dataset (types, nullability, uniqueness, allowed values, ranges, regex, referential integrity, freshness SLA, volume band), a gate that splits rows into **valid and quarantined** (each quarantined row lists *every* rule it broke), detects **schema drift** and **volume / freshness anomalies** against a rolling history, and exits **0 pass / 1 contract failure / 2 could not judge**. A failure of the gate itself can never look like a pass.

> **What is verified, and what is not.** 45 tests pass locally on Python 3.13 (stdlib only; pytest to run them), including exit-code tests through a real subprocess for 0, 1 and 2 (missing file, broken contract, missing reference data, injected crash). I mutation-checked 8 key behaviours (one literal break each, applied to a fresh copy): all 8 were caught, after one survivor (the stale-report deletion) got its own test. The GitHub Actions workflow (Linux, Python 3.11 and 3.13) ran green after the first push. **All data is synthetic and fictional** (one invented `orders` dataset, made-up ids). The numbers below show how this gate behaves on faults *I injected into a generator I wrote*; they do **not** show recall on real pipelines, real drift, seasonality, or a real false-alarm rate. Faults are drawn with random severity from very mild to severe, so some misses are expected by construction.

## Measured results

`python -m dcgate demo` (seeded, deterministic, ~15 s). Thresholds are tuned **only on the dev split** (99th percentile of each score over 200 dev clean batches) and applied unchanged to a held-out split (200 clean + 40 batches per fault). History for the z-scores is 20 clean batches from separate seeds. Each batch has about 600 rows and carries background noise (0.3% out-of-range amounts, 0.1% repeated ids, 0.2% unknown customers).

```
Thresholds tuned on dev only (200 clean batches, alpha=1%):
  violation-rate: type>0.0000 null>0.0000 allowed>0.0000 range>0.0093 pattern>0.0000 unique>0.0052 ref>0.0072
  null_z>3.34  volume_|z|>2.18  freshness_z>2.18

fault             n    dev  held-out        95% CI  mild half  severe half  worst miss sev
null_spike       40    92%       98%      87%-100%        95%         100%            0.07
duplicate_keys   40    98%       98%      87%-100%        95%         100%            0.02
type_drift       40   100%      100%      91%-100%       100%         100%               -
orphan_fk        40   100%       92%       80%-97%        85%         100%            0.03
stale_data       40    95%       98%      87%-100%        95%         100%            0.04
volume_drop      40    98%       90%       77%-96%        80%         100%            0.10
column_removed   40   100%      100%      91%-100%       100%         100%               -
column_added     40    90%       95%       83%-99%        90%         100%            0.19
dist_shift       40     5%        5%        1%-17%         5%           5%            0.99
false alarms on clean (dev): 11/200 = 5.5%  by detector: {range 2, null_spike 2, freshness_z 2, volume_z 2, ref 2, unique 2}
false alarms on clean (held-out): 14/200 = 7.0%  by detector: {range 2, volume_z 7, freshness_z 2, unique 2, ref 1}
```

- "Detected" means the gate failed **with the expected reason** (e.g. orphan keys must raise `violation_rate:ref`). `column_added` counts as detected when it only **warns** and still passes. `dist_shift` counts any failure.
- **What slips through:** `dist_shift` (all amounts scaled 1.2x to 1.8x, every value still inside the allowed range) is caught 5% of the time, which equals the false-alarm level, so effectively never. No rule in a contract can see it. The mild half of every fault is also missed more often (tiny duplicate or orphan fractions hide under the noise-level thresholds).
- **False alarms are not 1%.** Each detector targets 1%, but seven detectors add up to about 7% of clean batches on held-out data, and `volume_z` was 3.5% held-out against 1% on dev (20 history batches is a small sample for a standard deviation).
- The 5 to 10% misses on `column_added` are clean-noise alarms firing on top of a harmless warning.

## Run

```
python -m pip install pytest        # only needed for the tests
python -m pytest                    # 45 tests
python -m dcgate demo               # the table above (--quick for a small run)
python -m dcgate sample demo_data   # contract, customers.csv, history, a clean and a faulty batch
python -m dcgate check --contract demo_data/orders.contract.json --data demo_data/batch_orphans.jsonl ^
  --ref customers=demo_data/customers.csv --history demo_data/history.json --now 2026-01-15T12:00:00Z
```

Exit codes: `0` pass (warnings allowed), `1` contract failure, `2` could not judge (unreadable or ragged input, broken contract, missing reference data, bad history or thresholds, any crash in the gate). Options: `--quarantine-out`, `--report-out` (an old report is deleted at start, and an error report is written on exit 2), `--update-history` (appends only on pass), `--thresholds`, `--json`. Details: [STEPS.md](STEPS.md).

## Layout

```
dcgate/contract.py   contract parsing, strict (unknown keys, bad regex, bad bounds -> exit 2)
dcgate/checks.py     row validation and quarantine, schema drift, z-score with sd floor
dcgate/gate.py       analyze() measures a batch, judge() applies thresholds
dcgate/loader.py     CSV / JSONL / JSON readers (never skip a bad row silently)
dcgate/corpus.py     seeded batches and the nine fault injectors
dcgate/evaluate.py   dev-tuned thresholds, held-out measurement, Wilson intervals
dcgate/cli.py        check / demo / sample, exit-code policy
tests/               45 tests (rules, gate, real-subprocess CLI, evaluation)
```

## Known gaps

- No distribution-shift detector: the one fault the contract cannot express goes unseen (by design, shown above).
- One synthetic dataset and one generator; thresholds, noise and fault severities were chosen by me, so the rates are not transferable. No trend, seasonality or weekday effects in the history; z-scores assume a stationary baseline.
- 40 batches per fault gives wide intervals (see the CI column); the 7% clean false-alarm rate is high for a blocking gate.
- Uniqueness and referential checks are within one batch and in memory; no cross-batch duplicate detection, no streaming. Type drift uses the dominant observed type per column; CSV is coerced from strings, JSON is strict.
- Naive timestamps are treated as UTC. If the interpreter itself cannot start (for example a broken install), the exit code is whatever Python returns, not a clean 2; `KeyboardInterrupt` is non-zero but not mapped to 2.
- The mutation check is 8 hand-picked breaks run with `-x`, so it shows "caught", not how many tests catch each.
- Thresholds of 0 for type/null/allowed/pattern come from noise-free clean data; real data will need non-zero tolerances.
