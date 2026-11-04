"""One Agent SDK session in one repository: options, live transcript logging, outcome.

API reference for every name used here: https://code.claude.com/docs/en/agent-sdk/python
"""

import asyncio
import contextlib
import json
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKError,
    PermissionResultAllow,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from audit_loop.config import Config, claude_config_dir
from audit_loop.discover import git
from audit_loop.logs import log

# We set can_use_tool on purpose next to bypassPermissions (see options()); silence the advisory.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

# Opus 5.5 list prices in USD per million tokens; cache writes cost 1.25x input (5-minute TTL)
# or 2x (1-hour TTL), and web search is $10 per 1,000 searches:
# https://platform.claude.com/docs/en/about-claude/pricing
_PRICE_PER_MTOK = {
    "input_tokens": 4.0,
    "output_tokens": 20.0,
    "cache_read_input_tokens": 0.20,
    "ephemeral_5m_input_tokens": 5.0,
    "ephemeral_1h_input_tokens": 8.0,
}
_WEB_SEARCH_USD = 0.01


@dataclass
class Outcome:
    status: str = "error"  # ok | billing_exhausted | budget_cap | auth_failed | transient | timeout | error
    cost_usd: float = 0.0
    session_id: str | None = None
    turns: int = 0
    detail: str = ""


NO_HUMAN = ("No human is available: this is an unattended run. Use your best judgment, follow "
            "CLAUDE.md, and keep going without asking.")


async def allow_everything(tool_name, tool_input, context) -> PermissionResultAllow:
    """Approve every permission prompt: the run is unattended and meant to be unrestricted.

    bypassPermissions already approves almost everything; this catches the explicit ``ask``
    rules a user's settings may carry (sudo, rm -r, ...) and AskUserQuestion, which reaches
    the callback in every mode and is answered with a free-form ``response`` saying nobody
    is there. Deny rules still win in every mode.
    https://code.claude.com/docs/en/agent-sdk/user-input#response-format
    """
    if tool_name == "AskUserQuestion":
        return PermissionResultAllow(updated_input={**tool_input, "response": NO_HUMAN})
    return PermissionResultAllow(updated_input=tool_input)


def agent_env(credentials: dict[str, str]) -> dict[str, str]:
    """Credentials for the CLI child; a workspace ID also becomes the API's workspace header.

    A key that is not scoped to a workspace gets 400 "must include the anthropic-workspace-id
    header". The CLI sends extra headers from ANTHROPIC_CUSTOM_HEADERS ("Name: Value"):
    https://code.claude.com/docs/en/env-vars
    """
    # CLAUDE_CODE_SUBPROCESS_ENV_SCRUB keeps the key for the CLI's own calls but strips credentials
    # from the Bash/hook/MCP processes it starts (GitHub tokens stay), so an audited repo's own
    # scripts cannot quietly spend this key: https://code.claude.com/docs/en/env-vars
    env = {**credentials, "CLAUDE_AGENT_SDK_CLIENT_APP": "audit-loop/0.1", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}
    if workspace := credentials.get("ANTHROPIC_WORKSPACE_ID"):
        env["ANTHROPIC_CUSTOM_HEADERS"] = f"anthropic-workspace-id: {workspace}"
    return env


def options(repo: Path, cfg: Config, budget_usd: float, resume: str | None = None, **extra) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(repo),
        model=cfg.model,
        effort=cfg.effort,
        thinking={"type": "adaptive", "display": "summarized"},  # summaries land in the transcript
        # Full Claude Code prompt; without it the SDK uses a minimal one.
        # https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=None,  # user + project + local, so ~/.claude/CLAUDE.md is loaded
        permission_mode="bypassPermissions",
        can_use_tool=allow_everything,
        max_budget_usd=max(budget_usd, 0.01),  # only the shared cap; no per-repo limit
        resume=resume,
        env=agent_env(cfg.credentials),
        # The SDK reads each CLI message into a 1 MB buffer by default; one Read of a PNG figure
        # (base64 image) exceeded it and killed a session with CLIJSONDecodeError.
        max_buffer_size=256 * 1024 * 1024,
        stderr=lambda line: log.info("[cli] %s", line.rstrip()),
        **extra,
    )


def classify(api_errors: set[str], error_text: str, result: ResultMessage | None,
             api_status: int | None) -> str:
    """Map what the session reported to a status the loop acts on."""
    text = error_text.lower()
    if "billing_error" in api_errors or "credit balance" in text:
        return "billing_exhausted"
    if result is not None and not result.is_error:
        return "ok"
    if result is not None and result.subtype == "error_max_budget_usd":
        return "budget_cap"
    if "authentication_failed" in api_errors or api_status in (401, 403):
        return "auth_failed"
    if api_errors & {"rate_limit", "server_error"} or (api_status or 0) == 429 or (api_status or 0) >= 500:
        return "transient"
    return "error"


def usage_cost(usage: dict) -> float:
    """List-price USD of one API response's ``usage`` block."""
    cache_writes = usage.get("cache_creation") or {"ephemeral_5m_input_tokens": usage.get("cache_creation_input_tokens", 0)}
    tokens = {**usage, **cache_writes}
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests", 0)
    return sum(tokens.get(k, 0) * p for k, p in _PRICE_PER_MTOK.items()) / 1e6 + searches * _WEB_SEARCH_USD


def estimate_cost(usage_by_message: dict[str, dict]) -> float:
    return sum(usage_cost(u) for u in usage_by_message.values())


def transcript_cost(session_id: str | None, config_dir: Path) -> float | None:
    """List-price cost of everything a session's CLI transcript records, subagents included.

    The CLI writes every API response with its usage to
    ``<config dir>/projects/<project>/<session>.jsonl`` and subagents to
    ``<session>/subagents/*.jsonl`` (https://code.claude.com/docs/en/sessions). Counting each
    message id once reproduced the SDK's own figure to within $0.0002 on test sessions, and
    unlike the streamed messages it also covers sessions that crash. Returns None if absent.
    """
    if not session_id:
        return None
    projects = config_dir / "projects"
    files = [*projects.glob(f"*/{session_id}.jsonl"), *projects.glob(f"*/{session_id}/subagents/*.jsonl")]
    usage: dict[str, dict] = {}
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if entry.get("type") == "assistant" and isinstance(message, dict) and message.get("usage") and message.get("id"):
                usage[message["id"]] = message["usage"]
    return estimate_cost(usage) if files else None


def _block_text(block) -> str:
    if not isinstance(block, dict):
        return str(block)
    if block.get("type") == "image":  # base64 pixels say nothing in a transcript
        source = block.get("source", {})
        return f"<image {source.get('media_type', '?')}, {len(source.get('data', ''))} base64 chars>"
    return block.get("text", json.dumps(block, ensure_ascii=False))


def _text(content) -> str:
    if isinstance(content, str) or content is None:
        return content or ""
    return "\n".join(_block_text(c) for c in content)


def log_message(message) -> None:
    """Write one SDK message to the log in full (assistant text, thinking, tool calls, results)."""
    if isinstance(message, SystemMessage):
        if message.subtype == "init":
            d = message.data
            log.info("INIT model=%s permissionMode=%s apiKeySource=%s claude_code=%s cwd=%s tools=%s",
                     d.get("model"), d.get("permissionMode"), d.get("apiKeySource"),
                     d.get("claude_code_version"), d.get("cwd"), ",".join(d.get("tools", [])))
        elif message.subtype != "thinking_tokens":  # per-chunk token counters carry no content
            log.info("SYSTEM %s %s", message.subtype, json.dumps(message.data, ensure_ascii=False, default=str))
    elif isinstance(message, (AssistantMessage, UserMessage)):
        who = "SUBAGENT" if message.parent_tool_use_id else ("ASSISTANT" if isinstance(message, AssistantMessage) else "USER")
        if isinstance(message, AssistantMessage) and message.error:
            log.info("%s API ERROR: %s", who, message.error)
        for block in message.content if isinstance(message.content, list) else [TextBlock(message.content)]:
            if isinstance(block, TextBlock):
                log.info("%s: %s", who, block.text)
            elif isinstance(block, ThinkingBlock):
                log.info("%s THINKING: %s", who, block.thinking)
            elif isinstance(block, ToolUseBlock):
                log.info("%s TOOL_USE %s %s", who, block.name, json.dumps(block.input, ensure_ascii=False))
            elif isinstance(block, ToolResultBlock):
                log.info("%s TOOL_RESULT%s: %s", who, " (error)" if block.is_error else "", _text(block.content))
            else:
                log.info("%s %s: %s", who, type(block).__name__, json.dumps(vars(block), ensure_ascii=False, default=str))
    elif isinstance(message, ResultMessage):
        log.info("RESULT subtype=%s is_error=%s turns=%s cost=$%.4f stop=%s terminal=%s errors=%s\n%s",
                 message.subtype, message.is_error, message.num_turns, message.total_cost_usd or 0,
                 message.stop_reason, message.terminal_reason, message.errors, message.result or "")
    elif isinstance(message, RateLimitEvent):
        log.info("RATE_LIMIT %s", message.rate_limit_info.raw)
    else:
        log.info("%s", type(message).__name__)


async def run_session(repo: Path, prompt: str, cfg: Config, budget_usd: float,
                      resume: str | None = None, spent_before: float = 0.0, **extra) -> Outcome:
    """Run one query() to completion, logging everything, and classify how it ended.

    Every 20 API calls the session's real spend so far is read from its CLI transcript
    (minus ``spent_before``, what earlier runs of a resumed session already cost); at
    ``budget_usd`` the session is stopped, so the cap holds even if the CLI's own estimate lags.
    """
    out = Outcome(session_id=resume)
    capped = False
    result: ResultMessage | None = None
    api_errors: set[str] = set()
    error_text: list[str] = []
    api_status: int | None = None
    usage_by_message: dict[str, dict] = {}
    crashed = False
    turns = 0
    try:
        async with asyncio.timeout(cfg.session_timeout_min * 60):
            stream = query(prompt=prompt, options=options(repo, cfg, budget_usd, resume, **extra))
            async with contextlib.aclosing(stream):
                async for message in stream:
                    log_message(message)
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        out.session_id = message.data.get("session_id", out.session_id)
                    elif isinstance(message, AssistantMessage):
                        if message.error:
                            api_errors.add(message.error)
                            error_text += [b.text for b in message.content if isinstance(b, TextBlock)]
                        if message.usage and message.message_id:
                            new = message.message_id not in usage_by_message
                            usage_by_message[message.message_id] = message.usage
                            if new and len(usage_by_message) % 20 == 0:
                                seen = transcript_cost(out.session_id, claude_config_dir())
                                spent = (seen - spent_before) if seen is not None else estimate_cost(usage_by_message)
                                log.info("COST this session so far $%.2f of $%.2f budget (%d API calls seen)",
                                         spent, budget_usd, len(usage_by_message))
                                if spent >= budget_usd:
                                    log.info("Budget reached; stopping the session")
                                    capped = True
                                    break
                    elif isinstance(message, ResultMessage):  # one per turn, incl. background-task turns
                        result = message
                        turns += message.num_turns
    except TimeoutError:
        out.status = "timeout"
        log.info("Session hit the %.0f-minute wall-clock guard", cfg.session_timeout_min)
    except ResultError as e:  # the CLI exits non-zero after an error result
        api_status = e.api_error_status
        error_text += [str(e), *(e.errors or [])]
    except ClaudeSDKError as e:  # the harness broke, not the audit: retry (resume) it later
        crashed = True
        error_text.append(f"{type(e).__name__}: {e}")
        log.info("SDK error: %s", error_text[-1])
    if result is not None:
        out.cost_usd = result.total_cost_usd or 0.0
        out.turns = turns
        out.session_id = result.session_id or out.session_id
        api_status = api_status or result.api_error_status
        if result.is_error:
            error_text += [result.result or "", *(result.errors or [])]
    else:
        out.cost_usd = estimate_cost(usage_by_message)
    status = classify(api_errors, "\n".join(error_text), result, api_status)
    if crashed and status == "error":
        status = "transient"
    if capped and status != "billing_exhausted":
        status = "budget_cap"
    if out.status != "timeout" or status == "billing_exhausted":
        out.status = status
    out.detail = "\n".join(t for t in error_text if t)[:500]
    return out


def unpushed_commits(repo: Path) -> list[str]:
    """Commits on local branches that no remote-tracking branch contains (after a fetch)."""
    git(repo, "fetch", "--quiet", "--all")
    listing = git(repo, "log", "--branches", "--not", "--remotes", "--format=%h %s")
    return listing.splitlines() if listing else []


def push_prompt(commits: list[str]) -> str:
    shown = "\n".join(commits[:50]) + ("\n..." if len(commits) > 50 else "")
    return f"These commits are not on GitHub yet:\n{shown}\nPush them (branch, PR and merge, per CLAUDE.md)."


def first_heading(claude_md: Path) -> str | None:
    for line in claude_md.read_text(encoding="utf-8").splitlines() if claude_md.is_file() else []:
        if line.lstrip().startswith("#"):
            return line.strip()
    return None


async def check(cfg: Config, claude_md: Path) -> bool:
    """Few-cent preflight: key and credits work, and the user CLAUDE.md reaches the model."""
    expected = first_heading(claude_md)
    prompt = ("Without using any tools, quote verbatim the first markdown heading line of the "
              "user-level CLAUDE.md instructions in your context. Reply with that line only.")
    seen: list[str] = []
    with tempfile.TemporaryDirectory() as empty:
        stream = query(prompt=prompt, options=options(Path(empty), cfg, 1.0, tools=[], max_turns=1))
        outcome = Outcome()
        try:
            async with contextlib.aclosing(stream):
                async for message in stream:
                    log_message(message)
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        seen.append(f"model={message.data.get('model')} key={message.data.get('apiKeySource')}")
                    elif isinstance(message, AssistantMessage):
                        seen += [b.text for b in message.content if isinstance(b, TextBlock)]
                        if message.error:
                            outcome.detail = message.error
                    elif isinstance(message, ResultMessage):
                        outcome.cost_usd = message.total_cost_usd or 0.0
        except ClaudeSDKError as e:
            outcome.detail = str(e)
    answer = "\n".join(seen)
    ok = (expected is not None and expected.lstrip("# ").lower() in answer.lower()
          and f"model={cfg.model}" in answer and "key=ANTHROPIC_API_KEY" in answer)
    log.info("CHECK %s: expected heading %r; got %r; cost $%.4f %s", "PASSED" if ok else "FAILED",
             expected, answer, outcome.cost_usd, outcome.detail)
    return ok
