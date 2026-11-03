"""Find the repositories to audit: immediate subfolders of each root that are pushable GitHub repos."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# owner/name out of https://github.com/o/n(.git), git@github.com:o/n(.git), ssh://git@github.com/o/n
_GITHUB_SLUG = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)


@dataclass(frozen=True)
class Repo:
    path: Path
    slug: str  # "owner/name", lowercased: identifies clones of the same remote
    head_time: int  # committer time of HEAD, used for newest-first ordering


def git(path: Path, *args: str) -> str | None:
    """Run git in ``path``; return stripped stdout, or None if git fails."""
    try:
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def github_slug(remote_url: str | None) -> str | None:
    match = _GITHUB_SLUG.search(remote_url or "")
    return f"{match[1]}/{match[2]}".lower() if match else None


def can_push(slug: str) -> bool:
    """Ask the GitHub API (via gh) whether the current user may push; assume yes without gh.

    ``permissions.push`` is part of the repository object for authenticated callers:
    https://docs.github.com/en/rest/repos/repos#get-a-repository
    """
    try:
        out = subprocess.run(["gh", "api", f"repos/{slug}"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return True
    if out.returncode != 0:
        return True  # gh not logged in, offline, ...: let the agent find out instead of skipping
    return bool(json.loads(out.stdout).get("permissions", {}).get("push", False))


def inspect(path: Path) -> Repo | None:
    """Return a Repo if ``path`` is a non-empty Git repo whose origin is on GitHub."""
    if not (path / ".git").exists():
        return None
    slug = github_slug(git(path, "remote", "get-url", "origin"))
    if slug is None or not git(path, "ls-files"):
        return None
    return Repo(path=path, slug=slug, head_time=int(git(path, "log", "-1", "--format=%ct") or 0))


def discover(roots: list[Path], exclude: set[str], check_push: bool = True) -> list[Repo]:
    """Eligible repos in root order, newest HEAD first within each root, one clone per remote."""
    found: list[Repo] = []
    for root in roots:
        subdirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        repos = [r for p in subdirs if p.name.lower() not in exclude and (r := inspect(p))]
        found += sorted(repos, key=lambda r: r.head_time, reverse=True)
    newest_clone: dict[str, Repo] = {}
    for repo in found:
        if repo.slug not in newest_clone or repo.head_time > newest_clone[repo.slug].head_time:
            newest_clone[repo.slug] = repo
    kept = [r for r in found if newest_clone[r.slug] is r]
    return [r for r in kept if can_push(r.slug)] if check_push else kept
