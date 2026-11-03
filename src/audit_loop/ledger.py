"""Append-only JSONL record of every session: the shared budget, round resume, and the report.

Several invocations (e.g. one on Windows and one inside WSL) can point at the same file, so
the global cap covers all of them. Each line is one finished session.
"""

import json
import time
from pathlib import Path

# Statuses after which a repo counts as audited for the round. Credit, auth and transient
# failures are left undone so the next run (or the next credits) retries them.
DONE_STATUSES = {"ok", "error", "timeout", "budget_cap"}
MAX_ATTEMPTS = 3  # after this many sessions in one round a repo counts as done, whatever happened


def append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}) + "\n")


def read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def total_cost(records: list[dict]) -> float:
    return sum(r.get("cost_usd") or 0.0 for r in records)


def current_round(records: list[dict], scope: str, projects: list[str]) -> tuple[int, set[str]]:
    """Return (round number, projects already done in it) for this scope's projects.

    The latest round stays current until every project has a done record in it; then a new
    round starts with nothing done, so every repo is audited again, independently.
    """
    mine = [r for r in records if r.get("scope") == scope]
    if not mine:
        return 1, set()
    latest = max(r["round"] for r in mine)
    in_round = [r for r in mine if r["round"] == latest]
    attempts = {p: sum(r["project"] == p for r in in_round) for p in projects}
    done = {r["project"] for r in in_round if r["status"] in DONE_STATUSES}
    done |= {p for p, n in attempts.items() if n >= MAX_ATTEMPTS}
    if set(projects) <= done:
        return latest + 1, set()
    return latest, done


def last_attempt(records: list[dict], scope: str, project: str, round_no: int) -> dict | None:
    """The most recent record for this project in this round, if any (used to resume a crash)."""
    mine = [r for r in records if r.get("scope") == scope and r["project"] == project and r["round"] == round_no]
    return mine[-1] if mine else None


def report(records: list[dict]) -> str:
    """Markdown table of all sessions, with folder names and GitHub slugs only (no local paths)."""
    rows = [
        "| Time (UTC) | Round | Repo | Status | Cost (USD) | Minutes | Turns | Unpushed after |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for r in records:
        rows.append(
            f"| {r['ts']} | {r['round']} | [{r['name']}](https://github.com/{r['slug']}) | {r['status']} "
            f"| {r.get('cost_usd') or 0:.2f} | {r.get('minutes', 0):.0f} | {r.get('turns', 0)} "
            f"| {r.get('unpushed_after', '')} |"
        )
    ok = sum(r["status"] == "ok" for r in records)
    head = (
        f"# Audit runs\n\nGenerated from the ledger by `audit-loop report`. {len(records)} sessions, "
        f"{ok} finished normally, estimated total ${total_cost(records):.2f} "
        "(SDK client-side estimate, not a billing statement).\n\n"
    )
    return head + "\n".join(rows) + "\n"
