import os
import subprocess
from pathlib import Path

from audit_loop.discover import Repo, discover, github_slug, prioritize


def make_repo(path: Path, remote: str | None, files: bool = True, date: str = "2026-01-01T00:00:00") -> None:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, env=env)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    if remote:
        run("remote", "add", "origin", remote)
    if files:
        (path / "README.md").write_text("x")
        run("add", "README.md")
        run("commit", "-qm", "init")


def test_github_slug_forms():
    assert github_slug("https://github.com/Owner/Name.git") == "owner/name"
    assert github_slug("git@github.com:owner/name") == "owner/name"
    assert github_slug("https://github.com/owner/name/") == "owner/name"
    assert github_slug("https://gitlab.com/owner/name") is None
    assert github_slug(None) is None


def test_discover_filters_dedupes_and_orders(tmp_path):
    root = tmp_path / "root"
    make_repo(root / "old", "https://github.com/me/old.git", date="2026-01-01T00:00:00")
    make_repo(root / "new", "https://github.com/me/new.git", date="2026-03-01T00:00:00")
    make_repo(root / "empty", "https://github.com/me/empty.git", files=False)
    make_repo(root / "gitlab", "https://gitlab.com/me/x.git")
    make_repo(root / "noremote", None)
    make_repo(root / "Skipped", "https://github.com/me/skipped.git")
    make_repo(root / "clone-old", "https://github.com/me/dup.git", date="2026-01-02T00:00:00")
    make_repo(root / "clone-new", "git@github.com:me/dup.git", date="2026-02-01T00:00:00")
    (root / "plain-folder").mkdir()

    repos = discover([root, tmp_path / "missing-root"], {"skipped"}, check_push=False)

    assert [r.path.name for r in repos] == ["new", "clone-new", "old"]
    assert [r.slug for r in repos] == ["me/new", "me/dup", "me/old"]


def test_roots_keep_their_order(tmp_path):
    make_repo(tmp_path / "a" / "older", "https://github.com/me/a.git", date="2026-01-01T00:00:00")
    make_repo(tmp_path / "b" / "newer", "https://github.com/me/b.git", date="2026-05-01T00:00:00")
    repos = discover([tmp_path / "a", tmp_path / "b"], set(), check_push=False)
    assert [r.path.name for r in repos] == ["older", "newer"]


def test_prioritize_moves_named_repos_first_and_keeps_the_rest():
    repos = [Repo(Path(n), f"me/{n}", 0) for n in ("a", "b", "c", "d")]
    assert [r.path.name for r in prioritize(repos, ["C", "b"])] == ["c", "b", "a", "d"]
    assert prioritize(repos, []) == repos
