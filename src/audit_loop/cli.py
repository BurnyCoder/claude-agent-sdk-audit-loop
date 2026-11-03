"""`audit-loop` entry point: list | check | run | report | chain.

Data flow of `run`: discover repos under the roots -> for each repo not yet done this round,
run one Agent SDK session with the prompt (budget = cap minus ledger total) -> if commits are
left unpushed, resume that session once and ask it to push -> append a ledger line -> repeat
rounds until the credits or the cap run out (exit 3).
"""

import argparse
import asyncio
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from audit_loop import ledger
from audit_loop.config import Config, build_config, claude_config_dir, scrub_inherited_claude_env
from audit_loop.discover import Repo, discover
from audit_loop.logs import log, session_log, setup_console
from audit_loop.session import check, push_prompt, run_session, transcript_cost, unpushed_commits

EXIT_EXHAUSTED = 3  # credits gone or the shared cap reached: stop everything
EXIT_AUTH = 4  # the key is rejected: retrying cannot help
STOP_CODES = (EXIT_EXHAUSTED, EXIT_AUTH)


CONTINUE_PROMPT = ("Your previous session in this repository was cut off by a harness error. Continue the "
                   "task from where you stopped; the original instructions still apply.")


def audit_repo(repo: Repo, cfg: Config, round_no: int, scope: str, budget_usd: float,
               resume: str | None = None) -> dict:
    """Audit one repo (plus at most one push follow-up) and return its ledger record.

    With ``resume`` (the session ID of a crashed attempt), the agent continues that session
    instead of starting over, so the work already paid for is kept.
    """
    start = time.time()
    with session_log(cfg.log_dir, repo.path.name) as log_path:
        log.info("=== AUDIT %s (%s), round %d, budget left $%.2f%s ===", repo.path, repo.slug, round_no,
                 budget_usd, f", resuming {resume}" if resume else "")
        prompt = CONTINUE_PROMPT if resume else cfg.prompt
        recorded = sum(r.get("cost_usd") or 0 for r in ledger.read(cfg.ledger) if resume and r.get("session_id") == resume)
        outcome = asyncio.run(run_session(repo.path, prompt, cfg, budget_usd, resume=resume, spent_before=recorded))
        outcome.session_id = outcome.session_id or resume
        unpushed = unpushed_commits(repo.path)
        if unpushed and outcome.status == "ok" and outcome.session_id:
            log.info("=== %d unpushed commit(s); resuming the session to push them ===", len(unpushed))
            follow = asyncio.run(run_session(repo.path, push_prompt(unpushed), cfg, budget_usd - outcome.cost_usd,
                                             resume=outcome.session_id, spent_before=recorded + outcome.cost_usd))
            outcome.cost_usd += follow.cost_usd
            outcome.turns += follow.turns
            if follow.status in ("billing_exhausted", "budget_cap", "auth_failed"):
                outcome.status = follow.status
            unpushed = unpushed_commits(repo.path)
        # The transcript counts every API call, subagents and crashed runs included; take it when it
        # is higher, minus what earlier attempts of the same (resumed) session already recorded.
        seen = transcript_cost(outcome.session_id, claude_config_dir())
        if seen is not None:
            recorded = sum(r.get("cost_usd") or 0 for r in ledger.read(cfg.ledger)
                           if r.get("session_id") == outcome.session_id)
            outcome.cost_usd = max(outcome.cost_usd, seen - recorded)
        if outcome.status == "budget_cap":
            outcome.detail = f"stopped at the shared cap of ${cfg.max_total_usd:.2f}. " + outcome.detail
        log.info("=== DONE %s: %s, $%.2f, %d turns, %d unpushed ===",
                 repo.path.name, outcome.status, outcome.cost_usd, outcome.turns, len(unpushed))
    return {
        "scope": scope, "host": socket.gethostname(), "project": str(repo.path), "name": repo.path.name,
        "slug": repo.slug, "round": round_no, "session_id": outcome.session_id, "status": outcome.status,
        "cost_usd": round(outcome.cost_usd, 4), "minutes": round((time.time() - start) / 60, 1),
        "turns": outcome.turns, "unpushed_after": len(unpushed), "detail": outcome.detail,
        "log": log_path.name,
    }


def run_loop(cfg: Config, rounds: int) -> int:
    """Audit repos one at a time, round after round, until a stop condition (see module doc)."""
    scope = f"{socket.gethostname()}|{sys.platform}|" + ",".join(str(r.resolve()) for r in cfg.roots)
    repos = discover(cfg.roots, cfg.exclude)
    log.info("%d repos under %s; cap $%.2f, spent so far $%.2f", len(repos), [str(r) for r in cfg.roots],
             cfg.max_total_usd, ledger.total_cost(ledger.read(cfg.ledger)))
    if not repos:
        return 0
    passes, failures, auth_failures = 0, 0, 0
    while rounds == 0 or passes < rounds:
        round_no, done = ledger.current_round(ledger.read(cfg.ledger), scope, [str(r.path) for r in repos])
        queue = [r for r in repos if str(r.path) not in done]
        while queue:
            repo = queue[0]
            records = ledger.read(cfg.ledger)
            remaining = cfg.max_total_usd - ledger.total_cost(records)
            if remaining < 0.05:
                log.info("Spend cap of $%.2f reached; stopping", cfg.max_total_usd)
                return EXIT_EXHAUSTED
            minutes_left = (cfg.stop_at - datetime.now().astimezone()).total_seconds() / 60 if cfg.stop_at else None
            if minutes_left is not None and minutes_left < 1:
                log.info("Stop time %s reached; stopping", cfg.stop_at.isoformat())
                return EXIT_EXHAUSTED
            previous = ledger.last_attempt(records, scope, str(repo.path), round_no)
            resume = previous["session_id"] if previous and previous["status"] == "transient" else None
            session_cfg = cfg if minutes_left is None else replace(  # end the session by the stop time
                cfg, session_timeout_min=min(cfg.session_timeout_min, minutes_left))
            record = audit_repo(repo, session_cfg, round_no, scope, remaining, resume)
            ledger.append(cfg.ledger, record)
            status = record["status"]
            attempts = sum(r.get("scope") == scope and r["project"] == str(repo.path) and r["round"] == round_no
                           for r in ledger.read(cfg.ledger))
            if status != "transient" or attempts >= ledger.MAX_ATTEMPTS:
                queue.pop(0)  # otherwise retry (resume) the same repo after the backoff below
            if status in ("billing_exhausted", "budget_cap"):
                log.info("Credits exhausted or cap reached (%s); stopping", status)
                return EXIT_EXHAUSTED
            if status == "auth_failed":
                auth_failures += 1
                if auth_failures >= 3:
                    log.info("Authentication failed 3 times; stopping")
                    return EXIT_AUTH
            failures = 0 if status == "ok" else failures + 1
            if failures:  # transient, error, timeout, auth: back off 1, 2, 4, ... up to 10 minutes
                delay = min(60 * 2 ** (failures - 1), 600)
                log.info("Session ended with %s; waiting %d s", status, delay)
                time.sleep(delay)
        passes += 1
    return 0


def chain(commands: list[str], pause_s: float = 60) -> int:
    """Run the commands in order, forever, until one exits 3 or 4 (works across environments).

    Commands are split POSIX-style (shlex), so quote with '...' and avoid backslash paths.
    """
    while True:
        for command in commands:
            log.info("chain: %s", command)
            code = subprocess.run(shlex.split(command)).returncode
            if code in STOP_CODES:
                log.info("chain: exit %d from %r; stopping", code, command)
                return code
            if code != 0:
                log.info("chain: exit %d from %r; pausing %.0f s", code, command, pause_s)
                time.sleep(pause_s)


def write_report(cfg: Config, out: Path, push: bool) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ledger.report(ledger.read(cfg.ledger)), encoding="utf-8")
    log.info("Wrote %s", out)
    if push:  # commit just the report in the repo the command runs in
        subprocess.run(["git", "add", str(out)], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", "docs: update audit run report", "--", str(out)], check=True)
            subprocess.run(["git", "push"], check=False)
    return 0


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", action="append", help="folder whose subfolders are projects (repeatable; AUDIT_ROOTS)")
    common.add_argument("--exclude", action="append", help="folder names to skip, comma-separated (AUDIT_EXCLUDE)")
    common.add_argument("--env-file", action="append", help="env file(s) with ANTHROPIC_API_KEY / AUDIT_* (default .env)")
    common.add_argument("--ledger", help="JSONL ledger shared by all runs (AUDIT_LEDGER)")
    common.add_argument("--log-dir", help="transcript folder (AUDIT_LOG_DIR)")
    common.add_argument("--prompt-file", help="prompt instead of the bundled fact-check prompt (AUDIT_PROMPT_FILE)")
    common.add_argument("--model", help="model id (AUDIT_MODEL, default claude-opus-5-5)")
    common.add_argument("--effort", help="low|medium|high|xhigh|max (AUDIT_EFFORT, default xhigh)")
    common.add_argument("--max-total-usd", help="stop when the ledger total reaches this (AUDIT_MAX_TOTAL_USD, default 100)")
    common.add_argument("--session-timeout-min", help="wall-clock guard per session (AUDIT_SESSION_TIMEOUT_MIN, default 240)")
    common.add_argument("--stop-at", help="ISO time after which no session runs, e.g. when credits expire (AUDIT_STOP_AT)")
    common.add_argument("--keep-env", action="store_true",
                        help="keep inherited ANTHROPIC_*/CLAUDE* variables (e.g. your own gateway ANTHROPIC_BASE_URL)")
    p = argparse.ArgumentParser(prog="audit-loop", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", parents=[common], help="print the repos that would be audited")
    sub.add_parser("check", parents=[common], help="few-cent preflight: key, credits, model, CLAUDE.md")
    run = sub.add_parser("run", parents=[common], help="audit repos until the credits or the cap run out")
    run.add_argument("--rounds", type=int, default=0, help="passes over all repos (0 = until stopped)")
    rep = sub.add_parser("report", parents=[common], help="write the ledger as a Markdown table")
    rep.add_argument("--out", default="reports/runs.md")
    rep.add_argument("--push", action="store_true", help="commit and push the report in the current repo")
    ch = sub.add_parser("chain", help="repeat commands in order until one exits 3 or 4")
    ch.add_argument("commands", nargs="+")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    setup_console()
    if args.command == "chain":
        return chain(args.commands)
    flags = {"AUDIT_LEDGER": args.ledger, "AUDIT_LOG_DIR": args.log_dir, "AUDIT_PROMPT_FILE": args.prompt_file,
             "AUDIT_MODEL": args.model, "AUDIT_EFFORT": args.effort, "AUDIT_MAX_TOTAL_USD": args.max_total_usd,
             "AUDIT_SESSION_TIMEOUT_MIN": args.session_timeout_min, "AUDIT_STOP_AT": args.stop_at}
    excludes = [name for value in args.exclude or [] for name in value.split(",") if name.strip()]
    cfg = build_config(args.root, excludes or None, args.env_file, flags)
    if not args.keep_env:
        removed = scrub_inherited_claude_env()
        if removed:
            log.info("Removed %d inherited ANTHROPIC_*/CLAUDE* variables: %s", len(removed), ", ".join(removed))
    if args.command == "list":
        repos = discover(cfg.roots, cfg.exclude)
        for repo in repos:
            log.info("%s  %s  %s", time.strftime("%Y-%m-%d", time.gmtime(repo.head_time)), repo.slug, repo.path)
        log.info("%d repos", len(repos))
        return 0
    if args.command == "report":
        return write_report(cfg, Path(args.out), args.push)
    if "ANTHROPIC_API_KEY" not in cfg.credentials:
        log.info("No ANTHROPIC_API_KEY in the environment or the env files %s", args.env_file or [".env"])
        return EXIT_AUTH
    if args.command == "check":
        return 0 if asyncio.run(check(cfg, claude_config_dir() / "CLAUDE.md")) else 1
    try:
        return run_loop(cfg, args.rounds)
    finally:
        records = ledger.read(cfg.ledger)
        log.info("Ledger: %d sessions, estimated total $%.2f", len(records), ledger.total_cost(records))


if __name__ == "__main__":
    sys.exit(main())
