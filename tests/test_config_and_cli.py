import re
import sys
from pathlib import Path

from audit_loop.cli import chain
from audit_loop.config import build_config, read_env_files, scrub_inherited_claude_env

REPO = Path(__file__).resolve().parents[1]


def test_env_files_only_yield_credentials_and_audit_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\nANTHROPIC_WORKSPACE_ID=w\nWALLET_PRIVATE_KEY=secret\nAUDIT_MODEL=m\n")
    assert read_env_files([env, tmp_path / "missing"]) == {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_WORKSPACE_ID": "w",
                                                          "AUDIT_MODEL": "m"}


def test_flags_beat_env_files(tmp_path, monkeypatch):
    for key in ("AUDIT_ROOTS", "AUDIT_EXCLUDE", "AUDIT_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"ANTHROPIC_API_KEY=k\nAUDIT_ROOTS={tmp_path}\nAUDIT_EXCLUDE=A, b\nAUDIT_MODEL=from-file\n")
    cfg = build_config(env_files=[str(env)], overrides={"AUDIT_MODEL": "from-flag"})
    assert cfg.roots == [tmp_path] and cfg.exclude == {"a", "b"} and cfg.model == "from-flag"
    assert cfg.credentials == {"ANTHROPIC_API_KEY": "k"} and cfg.effort == "xhigh"
    assert "fact-check" in cfg.prompt


def test_scrub_keeps_only_our_credentials():
    environ = {"ANTHROPIC_BASE_URL": "x", "CLAUDECODE": "1", "CLAUDE_CODE_SIMPLE": "1", "ANTHROPIC_API_KEY": "k",
               "CLAUDE_CONFIG_DIR": "d", "PATH": "p"}
    assert sorted(scrub_inherited_claude_env(environ)) == ["ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_CODE_SIMPLE"]
    assert environ == {"ANTHROPIC_API_KEY": "k", "CLAUDE_CONFIG_DIR": "d", "PATH": "p"}


def test_chain_repeats_until_exit_3(tmp_path):
    counter = tmp_path / "n"
    script = tmp_path / "step.py"
    script.write_text(
        "import pathlib, sys\n"
        f"p = pathlib.Path({counter.as_posix()!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "sys.exit(3 if n >= 3 else 0)\n"
    )
    command = f"{Path(sys.executable).as_posix()} {script.as_posix()}"
    assert chain([command], pause_s=0) == 3
    assert counter.read_text() == "3"


def test_committed_files_have_no_machine_paths():
    pattern = re.compile(r"[A-Za-z]:\\\\|/home/\w|/Users/\w|wsl\.localhost|/mnt/[a-z]/")
    files = [*REPO.joinpath("src").rglob("*.py"), *REPO.joinpath("src").rglob("*.md"),
             REPO / "README.md", REPO / ".env.example", REPO / "pyproject.toml"]
    hits = [f"{f.name}: {m.group()}" for f in files for m in pattern.finditer(f.read_text(encoding="utf-8"))]
    assert hits == []
