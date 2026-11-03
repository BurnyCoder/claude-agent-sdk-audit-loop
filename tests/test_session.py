import asyncio
from pathlib import Path

from claude_agent_sdk import PermissionResultAllow, ResultMessage, ToolPermissionContext
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from audit_loop.config import Config
from audit_loop.session import _text, agent_env, allow_everything, classify, estimate_cost, first_heading, options


def result(is_error=False, subtype="success"):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id="s")


def test_classify():
    assert classify(set(), "", result(), None) == "ok"
    assert classify({"billing_error"}, "", result(True), 400) == "billing_exhausted"
    assert classify(set(), "Your credit balance is too low", None, 400) == "billing_exhausted"
    assert classify(set(), "", result(True, "error_max_budget_usd"), None) == "budget_cap"
    assert classify({"authentication_failed"}, "", result(True), 401) == "auth_failed"
    assert classify({"rate_limit"}, "", result(True), 429) == "transient"
    assert classify(set(), "", result(True), 529) == "transient"
    assert classify(set(), "boom", None, None) == "error"


def test_allow_everything_approves_ask_rules():
    decision = asyncio.run(allow_everything("Bash", {"command": "sudo ls"}, ToolPermissionContext()))
    assert isinstance(decision, PermissionResultAllow)
    assert decision.updated_input == {"command": "sudo ls"}


def test_ask_user_question_is_answered_without_a_human():
    questions = {"questions": [{"question": "Which?", "header": "H", "options": [], "multiSelect": False}]}
    decision = asyncio.run(allow_everything("AskUserQuestion", questions, ToolPermissionContext()))
    assert decision.updated_input["questions"] == questions["questions"]
    assert "unattended" in decision.updated_input["response"]


def test_cli_flags_are_unrestricted_opus_xhigh():
    cfg = Config(roots=[], credentials={"ANTHROPIC_API_KEY": "k"})
    opts = options(Path("."), cfg, 12.5)
    transport = SubprocessCLITransport(prompt="x", options=opts)
    transport._cli_path = "claude"  # normally resolved in connect(); we only inspect the argv
    cmd = transport._build_command()
    joined = " ".join(cmd)
    for flag in ("--model claude-opus-5-5", "--effort xhigh", "--permission-mode bypassPermissions",
                 "--thinking adaptive", "--max-budget-usd 12.5"):
        assert flag in joined
    assert "--tools" not in cmd and "--disallowedTools" not in cmd and "--max-turns" not in cmd
    assert "--system-prompt" not in cmd  # the claude_code preset, not the SDK's minimal prompt
    assert opts.env["ANTHROPIC_API_KEY"] == "k"


def test_large_messages_and_images():
    assert options(Path("."), Config(roots=[]), 1.0).max_buffer_size >= 100 * 1024 * 1024
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 5000}}
    assert _text([image, {"type": "text", "text": "ok"}]) == "<image image/png, 5000 base64 chars>\nok"


def test_workspace_id_becomes_header():
    env = agent_env({"ANTHROPIC_API_KEY": "k", "ANTHROPIC_WORKSPACE_ID": "wrkspc_1"})
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "anthropic-workspace-id: wrkspc_1"
    assert "ANTHROPIC_CUSTOM_HEADERS" not in agent_env({"ANTHROPIC_API_KEY": "k"})


def test_estimate_cost_uses_opus_prices():
    usage = {"m1": {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 1_000_000}}
    assert abs(estimate_cost(usage) - (4.0 + 2.0 + 0.2)) < 1e-9


def test_first_heading(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("intro\n\n## First rule\ntext\n# Later\n", encoding="utf-8")
    assert first_heading(md) == "## First rule"
    assert first_heading(tmp_path / "missing.md") is None
