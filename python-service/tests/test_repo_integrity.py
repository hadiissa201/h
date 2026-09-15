"""The working tree and the committed repository must agree.

Every other test in this suite runs against the working tree, so a source file
that exists on disk but was never committed passes everything here and still
breaks a fresh clone. That is not hypothetical: an unanchored ``data/`` rule in
.gitignore matched ``python-service/app/data/`` at depth and silently excluded the
entire market-data package from three commits. The suite was green; cloning the
repo and starting the service raised
``ModuleNotFoundError: No module named 'app.data'``.

These tests close that gap by asking git, not the filesystem, what the repository
actually contains.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _git(*args: str, ok_codes: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in ok_codes:
        pytest.skip(f"git unavailable or not a repository: {result.stderr.strip()}")
    return result.stdout


def _is_git_repo() -> bool:
    return (REPO_ROOT / ".git").exists()


pytestmark = pytest.mark.skipif(
    not _is_git_repo(), reason="not running inside a git checkout"
)


def _source_files() -> list[Path]:
    return [
        path
        for path in APP_DIR.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_every_application_source_file_is_tracked_by_git():
    """A file that git does not know about does not exist for anyone else."""
    tracked = {
        (REPO_ROOT / line).resolve()
        for line in _git("ls-files").splitlines()
        if line
    }

    untracked = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in _source_files()
        if path.resolve() not in tracked
    )

    assert not untracked, (
        "these application source files exist on disk but are NOT committed, so a "
        "fresh clone will fail to import them:\n  " + "\n  ".join(untracked)
    )


def test_no_application_source_file_is_gitignored():
    """Catches the cause, not just the symptom.

    ``git add`` on an ignored file fails quietly in normal workflows, so an
    over-broad ignore rule reappears as an untracked file the moment someone adds
    a new module under the matched directory.
    """
    candidates = [str(path.relative_to(REPO_ROOT)) for path in _source_files()]
    if not candidates:
        pytest.fail("no application source files found; the path is wrong")

    # check-ignore exits 1 when nothing matches -- that is the passing case here,
    # not a failure, so it must not be mistaken for "git is unavailable".
    ignored = _git("check-ignore", "--no-index", *candidates, ok_codes=(0, 1)).splitlines()

    assert not ignored, (
        "these application source files match a .gitignore rule and would be "
        "silently excluded from commits:\n  " + "\n  ".join(sorted(ignored))
    )


def test_gitignore_directory_rules_for_build_output_are_anchored():
    """``data/`` matches at any depth; ``/data/`` matches only the repo root.

    The unanchored form is what swallowed ``app/data``. Secret-bearing patterns
    (``secrets/``, ``*.pem``, ``*.key``) are deliberately left broad: the cost of
    committing a credential is far worse than the cost of an awkward directory
    name.
    """
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    rules = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]

    must_be_anchored = {"data", "logs", "reports", "build", "dist", "tmp"}
    unanchored = [
        rule
        for rule in rules
        if rule.rstrip("/").lstrip("!").split("/")[0] in must_be_anchored
        and not rule.lstrip("!").startswith("/")
    ]

    assert not unanchored, (
        "these .gitignore rules match at any depth and can swallow a source "
        "package; anchor them with a leading slash:\n  " + "\n  ".join(unanchored)
    )


def test_the_market_data_package_specifically_is_committed():
    """A named guard for the package that actually went missing."""
    tracked = set(_git("ls-files").splitlines())

    required = {
        "python-service/app/data/__init__.py",
        "python-service/app/data/service.py",
        "python-service/app/data/validation.py",
        "python-service/app/data/providers/__init__.py",
        "python-service/app/data/providers/base.py",
        "python-service/app/data/providers/ccxt_provider.py",
        "python-service/app/data/providers/synthetic.py",
    }

    missing = sorted(required - tracked)
    assert not missing, "market-data package files not committed:\n  " + "\n  ".join(missing)
