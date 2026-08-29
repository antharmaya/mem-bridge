"""Installer regression tests.

install.sh shipped a version that could only ever install into an empty path.
Anything already sitting at the install location -- a dangling symlink left by a
developer checkout, a half-finished clone, an unrelated directory -- fell
through to `git clone` onto an occupied path and died with:

    fatal: could not create work tree dir '...': File exists

and the update path could not move between tags at all, because a --depth 1
clone carries only the tag it was cloned at:

    error: pathspec 'v0.5.0' did not match any file(s) known to git

Both were reported from the field. These tests pin the states the installer has
to survive. They run fully offline against a local bare repo, so they are safe
in CI and do not depend on GitHub.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parent.parent / "install.sh"


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def origin(tmp_path_factory):
    """A local bare repo standing in for GitHub, tagged v0.4.0 and v0.5.0."""
    root = tmp_path_factory.mktemp("origin")
    work, bare = root / "work", root / "mem-bridge.git"

    work.mkdir()
    git("init", "-q", "-b", "main", str(work))
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "test", cwd=work)

    for tag in ("v0.4.0", "v0.5.0"):
        (work / "VERSION").write_text(tag)
        git("add", "-A", cwd=work)
        git("commit", "-qm", f"release {tag}", cwd=work)
        git("tag", "-a", tag, "-m", tag, cwd=work)

    git("clone", "-q", "--bare", str(work), str(bare))
    return bare


def install(origin, hermes_home, version="v0.5.0"):
    """Run the real installer, stopping after the clone/update logic."""
    env = {
        **os.environ,
        "HERMES_HOME": str(hermes_home),
        # file:// forces the real git transport; a plain local path makes git
        # ignore --depth, which would hide the shallow-clone bug being pinned.
        "REPO_URL": f"file://{origin}",
        "VERSION": version,
        "MEMORY_BRIDGE_SETUP_ONLY": "1",
    }
    return subprocess.run(
        ["bash", str(INSTALLER)], env=env, capture_output=True, text=True,
    )


def repo_dir(hermes_home):
    return hermes_home / "plugins" / "memory" / "antharmaya-bridge"


def installed_version(hermes_home):
    return git("describe", "--tags", "--exact-match", cwd=repo_dir(hermes_home))


def backups(hermes_home):
    return sorted(repo_dir(hermes_home).parent.glob("antharmaya-bridge.backup-*"))


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "hermes"
    (h / "plugins" / "memory").mkdir(parents=True)
    return h


def test_installs_into_empty_location(origin, home):
    result = install(origin, home)
    assert result.returncode == 0, result.stderr
    assert installed_version(home) == "v0.5.0"
    assert not backups(home)


def test_survives_dangling_symlink(origin, home):
    """The reported failure: a developer checkout that has since moved away."""
    repo_dir(home).symlink_to("/nonexistent/moved-away")
    assert not repo_dir(home).exists()  # dangling: -e is false, -L is true

    result = install(origin, home)

    assert result.returncode == 0, result.stderr
    assert "File exists" not in result.stderr
    assert installed_version(home) == "v0.5.0"
    assert not repo_dir(home).is_symlink()
    assert len(backups(home)) == 1, "the dead link should be kept, not deleted"


def test_does_not_write_through_a_live_symlink(origin, home, tmp_path):
    """A linked-in working tree must never be checked out from under the user."""
    dev = tmp_path / "dev-checkout"
    dev.mkdir()
    git("init", "-q", str(dev))
    (dev / "uncommitted.txt").write_text("work in progress")
    repo_dir(home).symlink_to(dev)

    result = install(origin, home)

    assert result.returncode == 0, result.stderr
    assert (dev / "uncommitted.txt").read_text() == "work in progress"
    assert installed_version(home) == "v0.5.0"


def test_moves_aside_an_unrelated_directory(origin, home):
    repo_dir(home).mkdir(parents=True)
    (repo_dir(home) / "junk.txt").write_text("not ours")

    result = install(origin, home)

    assert result.returncode == 0, result.stderr
    assert installed_version(home) == "v0.5.0"
    assert (backups(home)[0] / "junk.txt").read_text() == "not ours"


def test_moves_aside_a_foreign_git_repo(origin, home, tmp_path):
    other = tmp_path / "other.git"
    git("init", "-q", "--bare", str(other))
    git("clone", "-q", str(other), str(repo_dir(home)))

    result = install(origin, home)

    assert result.returncode == 0, result.stderr
    assert installed_version(home) == "v0.5.0"
    assert len(backups(home)) == 1


def test_upgrades_across_tags_on_a_shallow_clone(origin, home):
    """The second reported failure: `pathspec 'v0.5.0' did not match`."""
    assert install(origin, home, version="v0.4.0").returncode == 0
    assert installed_version(home) == "v0.4.0"
    assert git("rev-parse", "--is-shallow-repository", cwd=repo_dir(home)) == "true"

    result = install(origin, home, version="v0.5.0")

    assert result.returncode == 0, result.stderr
    assert "did not match" not in result.stderr
    assert installed_version(home) == "v0.5.0"
    assert not backups(home), "an upgrade should update in place, not re-clone"


def test_rerunning_is_idempotent(origin, home):
    install(origin, home)
    result = install(origin, home)

    assert result.returncode == 0, result.stderr
    assert "Already installed" in result.stdout
    assert "Downloading" not in result.stdout
    assert installed_version(home) == "v0.5.0"
    assert not backups(home)


def test_repairs_a_modified_checkout(origin, home):
    install(origin, home)
    (repo_dir(home) / "VERSION").write_text("corrupted")

    assert install(origin, home).returncode == 0
    assert (repo_dir(home) / "VERSION").read_text() == "v0.5.0"


def test_honours_hermes_home(origin, tmp_path):
    """REPO_DIR used to be hardcoded to ~/.hermes regardless of HERMES_HOME."""
    elsewhere = tmp_path / "custom-hermes"
    elsewhere.mkdir()

    assert install(origin, elsewhere).returncode == 0
    assert repo_dir(elsewhere).is_dir()
