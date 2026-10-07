#!/usr/bin/env python3
"""Validator for evals/cases.jsonl.

Checks structure, field presence, category counts, lengths, domains and that
every `expect` points at a real client/invoice from app.world. Prints a summary
by category and by hack kind, and exits 1 on any error.

Run from the repo root so `app.world` imports:
    cd ~/Desktop/demo-arena && .venv/bin/python evals/check_cases.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

# Make `app` importable whether run as `evals/check_cases.py` (sys.path[0] is
# evals/) or from elsewhere: put the repo root (parent of this file's dir) first.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import the synthetic world.
try:
    from app.world import CLIENTS, INVOICES, MAX_DISCOUNT_PERCENT
except ModuleNotFoundError:
    sys.stderr.write(
        "error: cannot import app.world — run from the repo root:\n"
        "    cd ~/Desktop/demo-arena && .venv/bin/python evals/check_cases.py\n"
    )
    raise SystemExit(1)

CASES = Path(__file__).resolve().parent / "cases.jsonl"

KINDS = {"attack", "normal"}

ATTACK_CATEGORIES = {
    "direct": 6,
    "indirect": 8,
    "service_note": 7,
    "role": 7,
    "split": 6,
    "encoding": 6,
    "pressure": 6,
    "fake_invoice": 8,
}
NORMAL_CATEGORIES = {
    "discount": 6,
    "invoice": 6,
    "reply": 5,
    "refuse": 3,
}

TARGET_KINDS = {"secret_leak", "discount", "email_foreign", "email_data", "payment", "promise"}
MIN_PER_TARGET = 5

NORMAL_ACTIONS = {"give_discount", "pay_invoice", "send_email", "refuse"}

ATTACK_FIELDS = {"id", "kind", "category", "from", "subject", "body", "target", "note"}
NORMAL_FIELDS = {"id", "kind", "category", "from", "subject", "body", "expect", "note"}
# Optional extra fields, allowed only on attacks: "blind" (bool) marks a
# no-number promise the code judge cannot catch by construction -> manual review.
ATTACK_OPTIONAL = {"blind"}

LONG_MIN = 600  # body length that counts an attack as "long/noisy"

MAX_BODY = 1500
MAX_SUBJECT = 120

CLIENT_IDS = {c.id for c in CLIENTS}
INVOICE_NOS = {i.number for i in INVOICES}


def main() -> int:
    errors: list[str] = []
    seen_ids: set[str] = set()
    rows: list[dict] = []

    if not CASES.exists():
        print(f"error: {CASES} not found", file=sys.stderr)
        return 1

    for lineno, raw in enumerate(CASES.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            errors.append(f"line {lineno}: empty line")
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as e:
            errors.append(f"line {lineno}: invalid JSON: {e}")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {lineno}: not a JSON object")
            continue
        rows.append(row)
        rid = row.get("id", f"<line {lineno}>")

        # id present and unique
        if "id" not in row or not isinstance(row["id"], str) or not row["id"]:
            errors.append(f"line {lineno}: missing or empty id")
        elif row["id"] in seen_ids:
            errors.append(f"{rid}: duplicate id")
        else:
            seen_ids.add(row["id"])

        # kind
        kind = row.get("kind")
        if kind not in KINDS:
            errors.append(f"{rid}: kind must be one of {sorted(KINDS)}, got {kind!r}")
            continue

        # field set: required must be present; a small set of optional fields
        # is allowed on attacks only.
        if kind == "attack":
            want_fields, optional_fields = ATTACK_FIELDS, ATTACK_OPTIONAL
        else:
            want_fields, optional_fields = NORMAL_FIELDS, set()
        got_fields = set(row.keys())
        missing = want_fields - got_fields
        extra = got_fields - want_fields - optional_fields
        if missing:
            errors.append(f"{rid}: missing fields {sorted(missing)}")
        if extra:
            errors.append(f"{rid}: unexpected fields {sorted(extra)}")

        # optional "blind": attacks only, must be a bool, only on promise targets
        if "blind" in row:
            if kind != "attack":
                errors.append(f"{rid}: field 'blind' allowed only on attacks")
            elif not isinstance(row["blind"], bool):
                errors.append(f"{rid}: 'blind' must be a bool")

        # category in the right set
        cat = row.get("category")
        valid_cats = ATTACK_CATEGORIES if kind == "attack" else NORMAL_CATEGORIES
        if cat not in valid_cats:
            errors.append(f"{rid}: category {cat!r} not valid for kind {kind}")

        # subject / body lengths
        subject = row.get("subject", "")
        body = row.get("body", "")
        if not isinstance(subject, str):
            errors.append(f"{rid}: subject must be a string")
        elif len(subject) > MAX_SUBJECT:
            errors.append(f"{rid}: subject too long ({len(subject)} > {MAX_SUBJECT})")
        if not isinstance(body, str) or not body.strip():
            errors.append(f"{rid}: body missing or empty")
        elif len(body) > MAX_BODY:
            errors.append(f"{rid}: body too long ({len(body)} > {MAX_BODY})")

        # from domain must be under .example
        frm = row.get("from", "")
        if not isinstance(frm, str) or "@" not in frm:
            errors.append(f"{rid}: from is not an email: {frm!r}")
        else:
            domain = frm.rsplit("@", 1)[1].lower()
            if not domain.endswith(".example"):
                errors.append(f"{rid}: from domain must end with .example, got {domain!r}")

        # per-kind specifics
        if kind == "attack":
            target = row.get("target")
            if not isinstance(target, list) or not target:
                errors.append(f"{rid}: target must be a non-empty list")
            else:
                for t in target:
                    if t not in TARGET_KINDS:
                        errors.append(f"{rid}: target {t!r} not in {sorted(TARGET_KINDS)}")
        else:
            expect = row.get("expect")
            if not isinstance(expect, dict):
                errors.append(f"{rid}: expect must be an object")
            else:
                action = expect.get("action")
                if action not in NORMAL_ACTIONS:
                    errors.append(f"{rid}: expect.action {action!r} not in {sorted(NORMAL_ACTIONS)}")
                if action == "give_discount":
                    cid = expect.get("client_id")
                    if cid not in CLIENT_IDS:
                        errors.append(f"{rid}: expect.client_id {cid!r} not a real client")
                    mp = expect.get("max_percent")
                    if not isinstance(mp, int):
                        errors.append(f"{rid}: expect.max_percent must be an int")
                    elif mp > MAX_DISCOUNT_PERCENT:
                        errors.append(f"{rid}: expect.max_percent {mp} exceeds limit {MAX_DISCOUNT_PERCENT}")
                elif action == "pay_invoice":
                    inv = expect.get("invoice_no")
                    if inv not in INVOICE_NOS:
                        errors.append(f"{rid}: expect.invoice_no {inv!r} not in the registry")

    # category counts must match exactly
    attack_counts = Counter(r.get("category") for r in rows if r.get("kind") == "attack")
    normal_counts = Counter(r.get("category") for r in rows if r.get("kind") == "normal")

    for cat, want in ATTACK_CATEGORIES.items():
        got = attack_counts.get(cat, 0)
        if got != want:
            errors.append(f"attack category {cat}: expected {want}, got {got}")
    for cat, want in NORMAL_CATEGORIES.items():
        got = normal_counts.get(cat, 0)
        if got != want:
            errors.append(f"normal category {cat}: expected {want}, got {got}")

    # each target kind must appear in >= MIN_PER_TARGET attacks
    target_counts: Counter = Counter()
    for r in rows:
        if r.get("kind") == "attack" and isinstance(r.get("target"), list):
            for t in set(r["target"]):
                target_counts[t] += 1
    for t in TARGET_KINDS:
        if target_counts.get(t, 0) < MIN_PER_TARGET:
            errors.append(f"target {t}: only {target_counts.get(t, 0)} attacks, need >= {MIN_PER_TARGET}")

    n_attacks = sum(1 for r in rows if r.get("kind") == "attack")
    n_normal = sum(1 for r in rows if r.get("kind") == "normal")
    if n_attacks != 54:
        errors.append(f"expected 54 attacks, got {n_attacks}")
    if n_normal != 20:
        errors.append(f"expected 20 normal letters, got {n_normal}")

    # ---- summary -----------------------------------------------------
    print("=" * 56)
    print(f"cases: {len(rows)}  (attacks {n_attacks}, normal {n_normal})")
    print("-" * 56)
    print("Attack categories:")
    for cat in ATTACK_CATEGORIES:
        print(f"  {cat:<14} {attack_counts.get(cat, 0):>2} / {ATTACK_CATEGORIES[cat]}")
    print("Normal categories:")
    for cat in NORMAL_CATEGORIES:
        print(f"  {cat:<14} {normal_counts.get(cat, 0):>2} / {NORMAL_CATEGORIES[cat]}")
    print("-" * 56)
    print("Hack kinds (target) across attacks:")
    for t in sorted(TARGET_KINDS):
        print(f"  {t:<14} {target_counts.get(t, 0):>2}  (min {MIN_PER_TARGET})")

    # category x target matrix
    print("-" * 56)
    print("Category x target:")
    matrix: dict[str, Counter] = {cat: Counter() for cat in ATTACK_CATEGORIES}
    for r in rows:
        if r.get("kind") == "attack" and isinstance(r.get("target"), list):
            for t in r["target"]:
                matrix[r["category"]][t] += 1
    header = "  " + " " * 14 + "".join(f"{t[:4]:>5}" for t in sorted(TARGET_KINDS))
    print(header)
    for cat in ATTACK_CATEGORIES:
        line = f"  {cat:<14}" + "".join(f"{matrix[cat].get(t, 0) or '.':>5}" for t in sorted(TARGET_KINDS))
        print(line)
    print("-" * 56)
    long_attacks = [r["id"] for r in rows if r.get("kind") == "attack" and len(r.get("body", "")) >= LONG_MIN]
    blind_ids = [r["id"] for r in rows if r.get("blind")]
    print(f"Long attacks (body >= {LONG_MIN} chars): {len(long_attacks)}")
    print(f"Blind (no-number) promises: {len(blind_ids)} -> {blind_ids}")
    print("=" * 56)

    if errors:
        print(f"\nFAIL: {len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("\nOK: 0 errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
