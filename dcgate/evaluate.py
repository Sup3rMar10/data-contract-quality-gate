"""Measure the gate on the seeded corpus. Thresholds are tuned ONLY on the dev split (the 99th
percentile of each score over dev clean batches) and then applied unchanged to a held-out split."""
import math
from collections import Counter

from .contract import parse_contract
from .corpus import CUSTOMERS, FAULTS, NOW, ORDERS_CONTRACT, make_batch
from .gate import KINDS, Thresholds, analyze, judge

REFS = {"customers": CUSTOMERS}


def make_history(contract, n=20):
    """Rolling history: summaries of n clean batches (separate seeds from dev and held-out)."""
    return [analyze(contract, make_batch("hist", "clean", i)[0], refs=REFS, now=NOW).summary() for i in range(n)]


def build_split(split, n_clean, n_fault, contract, hist):
    items = []
    for kind in ["clean", *FAULTS]:
        for i in range(n_clean if kind == "clean" else n_fault):
            rows, sev = make_batch(split, kind, i)
            an = analyze(contract, rows, refs=REFS, history=hist, now=NOW)
            an.valid, an.quarantined = [], []  # keep memory flat; scores are all the judge needs here
            items.append((kind, sev, an))
    return items


def tune(dev_items, alpha=0.01):
    clean = [an for k, _, an in dev_items if k == "clean"]

    def q(key):
        vals = sorted(an.scores[key] for an in clean if key in an.scores)
        return vals[min(len(vals) - 1, math.ceil((1 - alpha) * len(vals)) - 1)]

    return Thresholds(rate={k: q(f"rate:{k}") for k in KINDS}, null_z=q("null_z"), volume_z=q("volume_z"), fresh_z=q("fresh_z"))


def detected(kind, rep):
    codes = [f["code"] for f in rep["failures"]]
    exp = FAULTS[kind][1]
    if exp[0].startswith("warn:"):
        return rep["status"] == "pass" and exp[0][5:] in [w["code"] for w in rep["warnings"]]
    return any(c.startswith(e) for c in codes for e in exp)


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def measure(items, th):
    out = {"clean_n": 0, "false_alarms": 0, "alarm_codes": Counter(), "faults": {}}
    for kind, sev, an in items:
        rep = judge(an, th)
        if kind == "clean":
            out["clean_n"] += 1
            out["false_alarms"] += rep["status"] == "fail"
            for f in rep["failures"]:
                out["alarm_codes"][f["code"].split(":")[0] if f["code"].startswith("null") else f["code"]] += 1
        else:
            out["faults"].setdefault(kind, []).append((sev, detected(kind, rep)))
    return out


def rate(hits):
    return sum(h for _, h in hits) / len(hits) if hits else float("nan")


def run_eval(n_clean=200, n_fault=40):
    contract = parse_contract(ORDERS_CONTRACT)
    hist = make_history(contract)
    dev = build_split("dev", n_clean, n_fault, contract, hist)
    held = build_split("held", n_clean, n_fault, contract, hist)
    th = tune(dev)
    return {"thresholds": th, "dev": measure(dev, th), "held": measure(held, th), "n_clean": n_clean, "n_fault": n_fault}


def format_table(res):
    th, dev, held = res["thresholds"], res["dev"], res["held"]
    L = [f"Thresholds tuned on dev only ({res['n_clean']} clean batches, alpha=1%):",
         "  violation-rate: " + " ".join(f"{k}>{v:.4f}" for k, v in th.rate.items()),
         f"  null_z>{th.null_z:.2f}  volume_|z|>{th.volume_z:.2f}  freshness_z>{th.fresh_z:.2f}", "",
         f"{'fault':<15}{'n':>4}{'dev':>7}{'held-out':>10}{'95% CI':>14}{'mild half':>11}{'severe half':>13}{'worst miss sev':>16}"]
    for kind in FAULTS:
        h, d = held["faults"][kind], dev["faults"][kind]
        sevd = sorted(s for s, _ in h)
        med = sevd[len(sevd) // 2]
        lo, hi = wilson(sum(x for _, x in h), len(h))
        mild, sev = [t for t in h if t[0] < med], [t for t in h if t[0] >= med]
        miss = [s for s, x in h if not x]
        L.append(f"{kind:<15}{len(h):>4}{rate(d):>7.0%}{rate(h):>10.0%}{f'{lo:.0%}-{hi:.0%}':>14}"
                 f"{rate(mild):>11.0%}{rate(sev):>13.0%}{(f'{max(miss):.2f}' if miss else '-'):>16}")
    for name, m in (("dev", dev), ("held-out", held)):
        L.append(f"false alarms on clean ({name}): {m['false_alarms']}/{m['clean_n']} = {m['false_alarms'] / m['clean_n']:.1%}"
                 f"  by detector: {dict(m['alarm_codes']) or 'none'}")
    L.append("column_added counts as detected when it WARNS and still passes. dist_shift counts any failure.")
    return "\n".join(L)
