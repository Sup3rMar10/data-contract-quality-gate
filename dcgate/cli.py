"""CLI. Exit codes: 0 pass, 1 contract failure, 2 could not judge (anything else, including crashes)."""
import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone

from . import gate
from .contract import ContractError, InputError, CannotJudge, load_contract
from .loader import load_json, load_ref_values, load_rows


def _now(s):
    if not s:
        return datetime.now(timezone.utc)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise InputError(f"--now {s!r} is not an ISO timestamp")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def check(a):
    contract = load_contract(a.contract)
    rows, coerce = load_rows(a.data)
    specs = dict(s.split("=", 1) for s in a.ref if "=" in s)
    if len(specs) != len(a.ref):
        raise InputError("--ref must look like dataset=path")
    refs = {}
    for c in contract.columns.values():
        if c.ref and c.ref[0] in specs:
            refs[c.ref[0]] = load_ref_values(specs[c.ref[0]], c.ref[1])
    history = load_json(a.history, "history") if a.history and os.path.exists(a.history) else None
    th = gate.Thresholds.from_dict(load_json(a.thresholds, "thresholds")) if a.thresholds else gate.Thresholds()
    an = gate.analyze(contract, rows, refs=refs, history=history, now=_now(a.now), coerce=coerce)
    rep = gate.judge(an, th)
    if a.quarantine_out:
        _write(a.quarantine_out, "".join(json.dumps(q) + "\n" for q in an.quarantined))
    if a.report_out:
        _write(a.report_out, json.dumps(rep, indent=2))
    if a.update_history and rep["exit_code"] == 0 and a.history:
        _write(a.history, json.dumps(((history or []) + [an.summary()])[-50:]))
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        print(f"{rep['dataset']}: {rep['status'].upper()} (exit {rep['exit_code']}) rows={rep['rows_in']} "
              f"valid={rep['rows_valid']} quarantined={rep['rows_quarantined']}")
        for f in rep["failures"]:
            print(f"  FAIL {f['code']}: {f['detail']}")
        for w in rep["warnings"]:
            print(f"  WARN {w['code']}: {w['detail']}")
    return rep["exit_code"]


def _error_report(path, msg):
    if path:
        try:
            _write(path, json.dumps({"status": "error", "exit_code": 2, "error": msg}))
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dcgate", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate one batch against a contract")
    c.add_argument("--contract", required=True)
    c.add_argument("--data", required=True)
    c.add_argument("--ref", action="append", default=[], help="dataset=path of a reference file")
    c.add_argument("--history", help="JSON file of prior batch summaries")
    c.add_argument("--update-history", action="store_true", help="append this batch to --history if it passes")
    c.add_argument("--thresholds", help="JSON thresholds, e.g. from `demo --write-thresholds`")
    c.add_argument("--now", help="ISO timestamp used as 'now' for freshness (default: real now)")
    c.add_argument("--quarantine-out")
    c.add_argument("--report-out")
    c.add_argument("--json", action="store_true")
    d = sub.add_parser("demo", help="measure detection and false-alarm rates on the seeded corpus")
    d.add_argument("--quick", action="store_true", help="smaller corpus (50 clean / 10 per fault)")
    d.add_argument("--write-thresholds", help="write the dev-tuned thresholds to this JSON file")
    s = sub.add_parser("sample", help="write a sample contract, reference file, history and batches to a folder")
    s.add_argument("dir")
    a = ap.parse_args(argv)
    if a.cmd == "demo":
        from .evaluate import format_table, run_eval
        res = run_eval(50, 10) if a.quick else run_eval()
        print(format_table(res))
        if a.write_thresholds:
            _write(a.write_thresholds, json.dumps(res["thresholds"].to_dict(), indent=2))
        return 0
    if a.cmd == "sample":
        from .sample import write_sample
        write_sample(a.dir)
        print(f"wrote sample files to {a.dir}")
        return 0
    if getattr(a, "report_out", None) and os.path.exists(a.report_out):
        os.remove(a.report_out)  # a stale 'pass' report must never survive a failed run
    try:
        return check(a)
    except (ContractError, InputError, CannotJudge) as e:
        print(f"could not judge: {e}", file=sys.stderr)
        _error_report(a.report_out, str(e))
    except Exception as e:  # a bug in the gate is exit 2, never 0 and not mistaken for a data failure
        print(f"gate crashed: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _error_report(a.report_out, f"crash: {e}")
    return 2
