"""Write a runnable sample: contract, reference customers, history, one clean and one faulty batch."""
import csv
import json
import os

from .corpus import CUST_LIST, NOW, ORDERS_CONTRACT, make_batch
from .evaluate import make_history
from .contract import parse_contract


def write_sample(d):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "orders.contract.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(ORDERS_CONTRACT, f, indent=2)
    with open(os.path.join(d, "customers.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["customer_id"])
        w.writerows([c] for c in CUST_LIST)
    with open(os.path.join(d, "history.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(make_history(parse_contract(ORDERS_CONTRACT)), f)
    for name, kind, i in (("batch_clean.jsonl", "clean", 900), ("batch_orphans.jsonl", "orphan_fk", 900)):
        rows, _ = make_batch("sample", kind, i)
        with open(os.path.join(d, name), "w", encoding="utf-8", newline="\n") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
    with open(os.path.join(d, "NOW.txt"), "w", encoding="utf-8") as f:
        f.write(NOW.isoformat() + "\n")
