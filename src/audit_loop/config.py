"""Settings for one invocation: CLI flags > AUDIT_* process env > env files > defaults.

Nothing machine-specific lives in code; every folder comes from flags or AUDIT_* keys, so
anyone can point the loop at their own projects (see .env.example).
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

from dotenv import dotenv_values  # https://github.com/theskumar/python-dotenv#other-use-cases

# Env vars that would make the bundled CLI pick another credential or endpoint than the API
# key: a host app (e.g. Claude Desktop) exports ANTHROPIC_BASE_URL and many CLAUDE_* session
# variables to its children. Precedence: https://code.claude.com/docs/en/authentication
_INHERITED_CLAUDE_ENV = re.compile(r"^(ANTHROPIC_|CLAUDE)")
# Credentials the agent gets (read from env files too) plus the user's own config dir.
# ANTHROPIC_WORKSPACE_ID only matters for workload identity federation, but is harmless with a key:
# https://code.claude.com/docs/en/env-vars
CREDENTIAL_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID")
_KEEP_ENV = {*CREDENTIAL_KEYS, "CLAUDE_CONFIG_DIR"}


@dataclass
class Config:
    roots: list[Path]
    exclude: set[str] = field(default_factory=set)
    priority: list[str] = field(default_factory=list)  # folder names audited first each round, in order
    ledger: Path = Path("runs/ledger.jsonl")
    log_dir: Path = Path("logs")
    prompt: str = ""
    model: str = "claude-opus-5-5"
    effort: str = "xhigh"
    max_total_usd: float = 100.0
    session_timeout_min: float = 240.0
    stop_at: datetime | None = None  # no session runs past this (e.g. when free credits expire)
    credentials: dict[str, str] = field(default_factory=dict)  # CREDENTIAL_KEYS that are set


def read_env_files(paths: list[Path]) -> dict[str, str]:
    """Return only CREDENTIAL_KEYS and AUDIT_* from the files; later files win.

    Env files often hold unrelated secrets (other providers, wallets); dropping them here
    keeps them out of the agent's environment and therefore out of anything it can print.
    """
    values: dict[str, str] = {}
    for path in paths:
        if path.is_file():
            for key, value in dotenv_values(path).items():
                if value and (key in CREDENTIAL_KEYS or key.startswith("AUDIT_")):
                    values[key] = value
    return values


def scrub_inherited_claude_env(environ: dict[str, str] | os._Environ = os.environ) -> list[str]:
    """Delete inherited ANTHROPIC_*/CLAUDE* vars so the SDK child uses only our API key."""
    removed = [k for k in list(environ) if _INHERITED_CLAUDE_ENV.match(k) and k not in _KEEP_ENV]
    for key in removed:
        del environ[key]
    return removed


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def parse_time(value: str | None) -> datetime | None:
    """ISO 8601 time; one without an offset is taken as this machine's local time."""
    return datetime.fromisoformat(value).astimezone() if value else None


def claude_config_dir() -> Path:
    """Where Claude Code keeps settings and transcripts: https://code.claude.com/docs/en/env-vars"""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def default_prompt() -> str:
    return resources.files("audit_loop").joinpath("prompts/fact_check.md").read_text(encoding="utf-8")


def build_config(
    roots: list[str] | None = None,
    exclude: list[str] | None = None,
    env_files: list[str] | None = None,
    overrides: dict[str, str | None] | None = None,
) -> Config:
    """Merge flags (``roots``/``exclude``/``overrides``) over AUDIT_* env over env files."""
    file_env = read_env_files([Path(p).expanduser() for p in (env_files if env_files is not None else [".env"])])

    def get(key: str, default: str | None = None) -> str | None:
        flag = (overrides or {}).get(key)
        return flag if flag is not None else os.environ.get(key, file_env.get(key, default))

    prompt_file = get("AUDIT_PROMPT_FILE")
    return Config(
        roots=[Path(r).expanduser() for r in (roots or _split(get("AUDIT_ROOTS")))],
        exclude={name.lower() for name in (exclude or _split(get("AUDIT_EXCLUDE")))},
        priority=[name.lower() for name in _split(get("AUDIT_PRIORITY"))],
        ledger=Path(get("AUDIT_LEDGER", "runs/ledger.jsonl")).expanduser(),
        log_dir=Path(get("AUDIT_LOG_DIR", "logs")).expanduser(),
        prompt=Path(prompt_file).expanduser().read_text(encoding="utf-8") if prompt_file else default_prompt(),
        model=get("AUDIT_MODEL", "claude-opus-5-5"),
        effort=get("AUDIT_EFFORT", "xhigh"),
        max_total_usd=float(get("AUDIT_MAX_TOTAL_USD", "100")),
        session_timeout_min=float(get("AUDIT_SESSION_TIMEOUT_MIN", "240")),
        stop_at=parse_time(get("AUDIT_STOP_AT")),
        credentials={k: v for k in CREDENTIAL_KEYS if (v := os.environ.get(k) or file_env.get(k))},
    )
