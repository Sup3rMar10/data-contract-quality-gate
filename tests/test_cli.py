"""Exit codes through a real subprocess: 0 pass, 1 contract failure, 2 could not judge."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from dcgate.sample import write_sample

ROOT = Path(__file__).resolve().parent.parent


def run(*args, code=None):
    p = subprocess.run([sys.executable, "-m", "dcgate", *args], cwd=ROOT, capture_output=True, text=True, timeout=120)
    if code is not None:
        assert p.returncode == code, (p.returncode, p.stdout, p.stderr)
    return p


@pytest.fixture(scope="module")
def s(tmp_path_factory):
    d = tmp_path_factory.mktemp("sample")
    write_sample(str(d))
    return d


def check(s, batch, *extra):
    return ["check", "--contract", str(s / "orders.contract.json"), "--data", str(s / batch),
            "--ref", f"customers={s / 'customers.csv'}", "--history", str(s / "history.json"),
            "--now", "2026-01-15T12:00:00Z", *extra]


def test_clean_batch_exits_0(s):
    p = run(*check(s, "batch_clean.jsonl"), code=0)
    assert "PASS" in p.stdout


def test_orphan_keys_exit_1_and_quarantine_carries_the_rule(s, tmp_path):
    q = tmp_path / "q.jsonl"
    p = run(*check(s, "batch_orphans.jsonl", "--quarantine-out", str(q)), code=1)
    assert "violation_rate:ref" in p.stdout
    rows = [json.loads(line) for line in q.read_text(encoding="utf-8").splitlines()]
    assert rows and all(any(v["rule"] == "ref" for v in r["violations"]) for r in rows)


def test_missing_data_file_exits_2(s):
    p = run(*check(s, "does_not_exist.jsonl"), code=2)
    assert "could not judge" in p.stderr


def test_broken_contract_exits_2(s, tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text(json.dumps({"dataset": "t", "columns": {"a": {"type": "string", "nullible": True}}}), encoding="utf-8")
    run("check", "--contract", str(bad), "--data", str(s / "batch_clean.jsonl"), code=2)


def test_missing_reference_data_exits_2_not_0(s):
    p = run("check", "--contract", str(s / "orders.contract.json"), "--data", str(s / "batch_clean.jsonl"), code=2)
    assert "no reference data" in p.stderr


def test_crash_inside_the_gate_exits_2_never_0_or_1(s):
    code = ("import sys; from dcgate import cli, gate\n"
            "def boom(*a, **k): raise RuntimeError('injected')\n"
            "gate.analyze = boom\n"
            f"sys.exit(cli.main({check(s, 'batch_clean.jsonl')!r}))\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert p.returncode == 2 and "gate crashed" in p.stderr and "injected" in p.stderr


def test_stale_pass_report_is_replaced_by_an_error_report(s, tmp_path):
    r = tmp_path / "report.json"
    r.write_text(json.dumps({"status": "pass", "exit_code": 0}), encoding="utf-8")
    run(*check(s, "missing.jsonl", "--report-out", str(r)), code=2)
    assert json.loads(r.read_text(encoding="utf-8"))["status"] == "error"


def test_stale_report_is_deleted_even_when_the_run_dies_without_writing_one(s, tmp_path):
    r = tmp_path / "report.json"
    r.write_text(json.dumps({"status": "pass", "exit_code": 0}), encoding="utf-8")
    code = ("import sys; from dcgate import cli, gate\n"
            "def boom(*a, **k): raise KeyboardInterrupt()\n"
            "gate.analyze = boom\n"
            f"sys.exit(cli.main({check(s, 'batch_clean.jsonl', '--report-out', str(r))!r}))\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert p.returncode != 0 and not r.exists()


def test_history_is_appended_only_when_the_batch_passes(s, tmp_path):
    h = tmp_path / "h.json"
    h.write_text((s / "history.json").read_text(encoding="utf-8"), encoding="utf-8")
    n = len(json.loads(h.read_text(encoding="utf-8")))

    def go(batch):
        a = check(s, batch, "--update-history")
        a[a.index("--history") + 1] = str(h)
        return run(*a)

    assert go("batch_orphans.jsonl").returncode == 1
    assert len(json.loads(h.read_text(encoding="utf-8"))) == n
    assert go("batch_clean.jsonl").returncode == 0
    assert len(json.loads(h.read_text(encoding="utf-8"))) == n + 1


def test_csv_input_and_every_violated_rule_in_quarantine(tmp_path):
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"dataset": "t", "columns": {
        "id": {"type": "string", "pattern": "T-[0-9]+", "unique": True},
        "n": {"type": "integer", "min": 0, "max": 9}}}), encoding="utf-8")
    d = tmp_path / "d.csv"
    d.write_text("id,n\nT-1,5\nT-2,7\nbad,99\nT-1,x\n", encoding="utf-8")
    q = tmp_path / "q.jsonl"
    run("check", "--contract", str(c), "--data", str(d), "--quarantine-out", str(q), code=1)
    got = {json.loads(x)["row_index"]: sorted(v["rule"] for v in json.loads(x)["violations"])
           for x in q.read_text(encoding="utf-8").splitlines()}
    assert got == {2: ["pattern", "range"], 3: ["type", "unique"]}
    d.write_text("id,n\nT-1,5\nT-2,7\n", encoding="utf-8")
    run("check", "--contract", str(c), "--data", str(d), code=0)


def test_demo_quick_prints_the_table():
    p = run("demo", "--quick", code=0)
    assert "held-out" in p.stdout and "dist_shift" in p.stdout
