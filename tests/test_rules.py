"""Contract parsing, row validation and loaders."""
import math

import pytest

from dcgate.checks import validate_rows
from dcgate.contract import ContractError, InputError, load_contract, parse_contract
from dcgate.corpus import ORDERS_CONTRACT
from dcgate.loader import load_rows


def mk(**cols):
    return parse_contract({"dataset": "t", "columns": cols})


def viol(c, rows, refs=None, coerce=False):
    valid, quar, counts, _ = validate_rows(c, rows, refs or {}, coerce)
    return valid, quar, counts


def rules(q):
    return sorted((v["column"], v["rule"]) for v in q["violations"])


# ---- contract ----
def test_orders_contract_parses():
    c = parse_contract(ORDERS_CONTRACT)
    assert c.fresh_col == "created_at" and c.columns["customer_id"].ref == ("customers", "customer_id")


BAD_CONTRACTS = [
    {"dataset": "t", "columns": {"a": {"type": "string", "nullible": True}}},  # typo in a column key
    {"dataset": "t", "columns": {"a": {"type": "string"}}, "freshnes": {}},  # typo in a top-level key
    {"dataset": "t", "columns": {"a": {"type": "string", "pattern": "([a-z"}}},  # bad regex
    {"dataset": "t", "columns": {"a": {"type": "integer", "min": 5, "max": 1}}},
    {"dataset": "t", "columns": {"a": {"type": "decimal"}}},
    {"dataset": "t", "columns": {"a": {"type": "string"}}, "freshness": {"column": "a", "max_age_hours": 5}},
    {"dataset": "t", "columns": {"a": {"type": "string"}}, "volume": {"min_rows": 10, "max_rows": 5}},
]


def test_broken_contracts_are_rejected():
    for bad in BAD_CONTRACTS:
        with pytest.raises(ContractError):
            parse_contract(bad)


def test_missing_or_invalid_contract_file(tmp_path):
    with pytest.raises(ContractError):
        load_contract(tmp_path / "nope.json")
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError):
        load_contract(p)


# ---- validation ----
def test_null_only_violates_when_not_nullable():
    c = mk(a={"type": "string"}, b={"type": "string", "nullable": True})
    _, quar, counts = viol(c, [{"a": None, "b": None}, {"a": "x"}])
    assert [rules(q) for q in quar] == [[("a", "null")]] and counts == {"null": 1}


def test_strict_json_rejects_numeric_string_but_csv_coerces():
    c = mk(n={"type": "number"})
    assert viol(c, [{"n": "12.5"}])[2] == {"type": 1}
    assert viol(c, [{"n": "12.5"}], coerce=True)[2] == {}
    assert viol(c, [{"n": "abc"}], coerce=True)[2] == {"type": 1}


def test_bool_is_not_integer_and_nan_is_not_number():
    c = mk(i={"type": "integer"}, n={"type": "number"})
    _, quar, _ = viol(c, [{"i": True, "n": math.nan}, {"i": 3, "n": math.inf}])
    assert rules(quar[0]) == [("i", "type"), ("n", "type")] and rules(quar[1]) == [("n", "type")]


def test_allowed_values():
    c = mk(s={"type": "string", "allowed": ["a", "b"]})
    assert viol(c, [{"s": "a"}, {"s": "z"}])[2] == {"allowed": 1}


def test_range_bounds_are_inclusive():
    c = mk(n={"type": "number", "min": 1, "max": 10})
    valid, quar, _ = viol(c, [{"n": 1}, {"n": 10}, {"n": 0.99}, {"n": 10.01}])
    assert len(valid) == 2 and len(quar) == 2


def test_pattern_must_match_the_whole_value():
    c = mk(s={"type": "string", "pattern": "ORD-[0-9]{3}"})
    _, quar, _ = viol(c, [{"s": "ORD-123"}, {"s": "ORD-1234"}, {"s": "xORD-123"}])
    assert [q["row_index"] for q in quar] == [1, 2]


def test_unique_flags_later_occurrences_only():
    c = mk(k={"type": "string", "unique": True})
    _, quar, _ = viol(c, [{"k": "a"}, {"k": "b"}, {"k": "a"}, {"k": "a"}])
    assert [q["row_index"] for q in quar] == [2, 3]


def test_referential_integrity():
    c = mk(c={"type": "string", "references": {"dataset": "cust", "column": "id"}})
    _, quar, counts = viol(c, [{"c": "C1"}, {"c": "C9"}], refs={"cust": {"C1"}})
    assert counts == {"ref": 1} and quar[0]["row_index"] == 1


def test_quarantined_row_carries_every_violated_rule_and_rows_partition():
    c = mk(a={"type": "string", "pattern": "A[0-9]", "allowed": ["A1"]}, n={"type": "integer", "min": 5})
    rows = [{"a": "A1", "n": 7}, {"a": "zz", "n": 1}]
    valid, quar, _ = viol(c, rows)
    assert rules(quar[0]) == [("a", "allowed"), ("a", "pattern"), ("n", "range")]
    assert len(valid) + len(quar) == len(rows) and quar[0]["row"] is rows[1]


# ---- loaders ----
def test_csv_empty_string_is_null_and_marks_coerce(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,\n2,z\n", encoding="utf-8")
    rows, coerce = load_rows(p)
    assert coerce and rows == [{"a": "1", "b": None}, {"a": "2", "b": "z"}]


def test_ragged_csv_is_an_error_not_a_silent_skip(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n3\n", encoding="utf-8")
    with pytest.raises(InputError, match="line 3"):
        load_rows(p)


def test_jsonl_bad_line_reports_line_number(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n{oops\n', encoding="utf-8")
    with pytest.raises(InputError, match="line 2"):
        load_rows(p)


def test_non_object_row_and_unknown_extension_are_errors(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n[1, 2]\n', encoding="utf-8")
    with pytest.raises(InputError, match="row 1"):
        load_rows(p)
    with pytest.raises(InputError):
        load_rows(tmp_path / "x.parquet")
