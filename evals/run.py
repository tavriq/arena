"""Run the letters of evals/cases.jsonl through the agent: models x levels. Prints one JSON line per attempt.

Needs LLM_API_KEY and LLM_BASE_URL. Stops when the spent rubles reach --max-rub
(the attempts already running finish, so the overshoot is at most --workers
attempts). Summary: evals/summarize.py.

  python -u evals/run.py --models timeweb/gpt-oss-120b --levels 1,2,3,4,5 --max-rub 40 > runs/x.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import FILTER_MAX_TOKENS, MAX_MODEL_CALLS, MAX_TOKENS, AttemptError, run_attempt  # noqa: E402
from app.llm import Gateway  # noqa: E402
from app.pricing import worst_attempt_rub  # noqa: E402

FILTER_MODEL = "timeweb/gpt-oss-120b"


def load_cases(path: Path, kinds: set[str], only: set[str]) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [c for c in cases if c["kind"] in kinds and (not only or c["id"] in only)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="timeweb/gpt-oss-120b")
    ap.add_argument("--levels", default="1,2,3,4,5")
    ap.add_argument("--cases", default=str(Path(__file__).with_name("cases.jsonl")))
    ap.add_argument("--kinds", default="attack,normal")
    ap.add_argument("--ids", default="", help="only these case ids, comma-separated")
    ap.add_argument("--max-rub", type=float, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="print the worst case and exit")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    levels = [int(x) for x in args.levels.split(",")]
    cases = load_cases(Path(args.cases), set(args.kinds.split(",")), set(filter(None, args.ids.split(","))))
    jobs = [(m, lvl, c) for m in models for lvl in levels for c in cases]
    worst = sum(worst_attempt_rub(m, FILTER_MODEL if lvl >= 3 else None, MAX_MODEL_CALLS, MAX_TOKENS,
                                  FILTER_MAX_TOKENS) for m, lvl, _ in jobs)
    print(f"attempts: {len(jobs)}, worst case {worst:.2f} ₽, stop at {args.max_rub:.2f} ₽", file=sys.stderr)
    if args.dry_run:
        return 0

    client = Gateway(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"], timeout_s=60)
    lock = threading.Lock()
    spent = [0.0]
    stop = threading.Event()

    def one(job):
        model, level, case = job
        if stop.is_set():
            return
        try:
            result = run_attempt(client, level=level, subject=case.get("subject", ""), body=case["body"],
                                 sender=case["from"], model=model, filter_model=FILTER_MODEL)
            cost, error = result["cost_rub"], None
        except AttemptError as e:
            result, cost, error = None, e.cost_rub, e.detail
        with lock:
            spent[0] += cost
            row = {"case": case["id"], "kind": case["kind"], "category": case["category"],
                   "target": case.get("target"), "expect": case.get("expect"), "blind": bool(case.get("blind")),
                   "model": model, "level": level,
                   "error": error, "result": result}
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if spent[0] >= args.max_rub:
                stop.set()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, jobs))
    print(f"spent {spent[0]:.2f} ₽{' (stopped at the limit)' if stop.is_set() else ''}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
