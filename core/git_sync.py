"""Commit the day's captured history (data/) to git and push.

Lets a long-running deployment (e.g. a Raspberry Pi) durably archive each day's
forecasts/results/bias/outcomes by committing them to the repo after market
close. Pulls --rebase before pushing so multiple machines don't collide.

Best-effort: every failure is returned as a message, never raised, so a git
hiccup can't take down the bot.
"""

from __future__ import annotations

import logging
import os
import subprocess

from core.paths import ROOT as REPO_DIR

logger = logging.getLogger(__name__)


def _git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_DIR, capture_output=True, text=True, timeout=timeout,
    )


def get_version() -> str:
    """branch@shortsha of the running checkout, for status displays."""
    try:
        commit = _git("rev-parse", "--short", "HEAD").stdout.strip()
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        return f"{branch}@{commit}" if commit else "unknown"
    except Exception:
        return "unknown"


def commit_data_history(message: str, push: bool = True) -> tuple[bool, str]:
    """Stage data/, commit if there's anything new, then (optionally) pull --rebase + push.

    Returns (changed, message). `changed` is False when there was nothing to commit.
    """
    try:
        if not os.path.isdir(os.path.join(REPO_DIR, "data")):
            return False, "no data/ directory yet"

        add = _git("add", "data")
        if add.returncode != 0:
            return False, f"git add failed: {add.stderr.strip()}"

        # `git diff --cached --quiet` exits 0 when nothing is staged, 1 when there is.
        if _git("diff", "--cached", "--quiet", "--", "data").returncode == 0:
            return False, "no new history to commit"

        commit = _git("commit", "-m", message)
        if commit.returncode != 0:
            return False, f"git commit failed: {(commit.stderr or commit.stdout).strip()}"

        if not push:
            return True, "committed (push skipped)"

        pull = _git("pull", "--rebase", "--autostash")
        if pull.returncode != 0:
            return True, f"committed locally, but pull --rebase failed: {pull.stderr.strip()}"

        push_res = _git("push")
        if push_res.returncode != 0:
            return True, f"committed locally, but push failed: {push_res.stderr.strip()}"

        return True, "committed and pushed"
    except Exception as exc:  # subprocess timeout, git missing, etc.
        logger.exception("git_sync error: %s", exc)
        return False, f"git error: {exc}"
