"""Judge recorded attempts again with the current judge (app/judge.py), without calling the model.

A run keeps the code, every tool call in order and the agent's note, so a fixed judge can be applied to
an old run. Tool calls are replayed with app.tools.run_tool; the replayed status must match the recorded
one (the guard is the same), otherwise the row is left as it was and reported. A note hidden by the guard
keeps its recorded flag: its text is not in the run. Rows are merged as in summarize.py (a retry wins).

  python3 evals/rejudge.py runs/a.jsonl runs/a-retry.jsonl > runs/a-rejudged.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import verdict  # noqa: E402
from app.judge import Context, note_hacks  # noqa: E402
from app.tools import run_tool  # noqa: E402
from evals.summarize import merge  # noqa: E402

CASES = Path(__file__).with_name("cases.jsonl")


def rejudge(row: dict, case: dict) -> dict | None:
    """The new result fields, or None if the replay does not match the recorded tool statuses."""
    r = row["result"]
    ctx = Context(sender=case["from"], secret=r["secret"], letter=f"{case.get('subject', '')}\n{case['body']}")
    hacks: list = []
    stopped: list[str] = []
    by_filter = False
    for step in r["steps"]:
        if step["type"] == "filter" and step["injection"]:
            by_filter = True
        elif step["type"] == "tool" and step["status"] != "error":
            out = run_tool(step["name"], step["args"], row["level"], ctx)
            if out.status != step["status"]:
                return None
            if out.status == "done":
                hacks.extend(out.hacks)
            elif out.hacks:
                stopped.append(out.status)
        elif step["type"] == "note":
            if step["status"] == "done":
                hacks.extend(note_hacks(step["text"], ctx))
            elif step["hack"]:
                stopped.append("blocked")
    return verdict(hacks, stopped, by_filter)


def main(paths: list[str]) -> int:
    cases = {c["id"]: c for c in map(json.loads, CASES.read_text(encoding="utf-8").splitlines()) if c}
    rows = merge([json.loads(line) for p in paths for line in Path(p).read_text(encoding="utf-8").splitlines()
                  if line.strip()])
    changed = mismatched = 0
    for row in rows:
        if not row["result"]:
            print(json.dumps(row, ensure_ascii=False))
            continue
        new = rejudge(row, cases[row["case"]])
        if new is None:
            mismatched += 1
            print(f"replay mismatch, left as is: level {row['level']} {row['case']}", file=sys.stderr)
        else:
            old = (row["result"]["verdict"], sorted(h["title"] for h in row["result"]["hacks"]))
            if old != (new["verdict"], sorted(h["title"] for h in new["hacks"])):
                changed += 1
                print(f"level {row['level']} {row['case']}: {old[0]} {old[1]} -> {new['verdict']} "
                      f"{sorted(h['title'] for h in new['hacks'])}", file=sys.stderr)
            row["result"].update(new)
        print(json.dumps(row, ensure_ascii=False))
    print(f"rows {len(rows)}, changed {changed}, replay mismatch {mismatched}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
