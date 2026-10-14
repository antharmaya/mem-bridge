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
    # Where Hermes (0.10+) looks for user-installed memory providers.
    return hermes_home / "plugins" / "antharmaya-bridge"


def legacy_dir(hermes_home):
    # Where installers up to v0.5.0 put it; Hermes never scans this path.
    return hermes_home / "plugins" / "memory" / "antharmaya-bridge"


def installed_version(hermes_home):
    return git("describe", "--tags", "--exact-match", cwd=repo_dir(hermes_home))


def backups(hermes_home):
    return sorted(repo_dir(hermes_home).parent.glob("antharmaya-bridge.backup-*"))


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "hermes"
    (h / "plugins").mkdir(parents=True)
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


def test_moves_a_legacy_install_to_where_hermes_looks(origin, home):
    """v0.5.0 installed into plugins/memory/<name>/, which Hermes never reads."""
    legacy_dir(home).parent.mkdir(parents=True)
    git("clone", "-q", "--depth", "1", "--branch", "v0.4.0",
        f"file://{origin}", str(legacy_dir(home)))

    result = install(origin, home)

    assert result.returncode == 0, result.stderr
    assert installed_version(home) == "v0.5.0"
    assert not legacy_dir(home).exists()
    assert not legacy_dir(home).parent.exists(), "the emptied plugins/memory dir is tidied"
    assert not backups(home)


def test_leaves_a_legacy_symlink_alone(origin, home, tmp_path):
    """A developer link at the old path is not ours to move."""
    dev = tmp_path / "dev-checkout"
    dev.mkdir()
    legacy_dir(home).parent.mkdir(parents=True)
    legacy_dir(home).symlink_to(dev)

    result = install(origin, home)

    assert result.returncode == 0, result.stderr
    assert legacy_dir(home).is_symlink()
    assert installed_version(home) == "v0.5.0"


# ─── Full install against the real package ─────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def real_origin(tmp_path_factory):
    """A bare repo holding this working tree, tagged, so the whole installer runs."""
    root = tmp_path_factory.mktemp("real-origin")
    work, bare = root / "work", root / "mem-bridge.git"
    files = git("ls-files", "-co", "--exclude-standard", cwd=REPO_ROOT).splitlines()
    for rel in files:
        src_file = REPO_ROOT / rel
        if not src_file.is_file():
            continue  # deleted in the working tree but still in the index
        dest = work / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)
    git("init", "-q", "-b", "main", str(work))
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "test", cwd=work)
    git("add", "-A", cwd=work)
    git("commit", "-qm", "snapshot", cwd=work)
    git("tag", "-a", "v9.9.9", "-m", "v9.9.9", cwd=work)
    git("clone", "-q", "--bare", str(work), str(bare))
    return bare


def full_install(origin, fake_home, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "MEMORY_BRIDGE_BIN_DIR", "PYTHONPATH")}
    env.update({
        "HOME": str(fake_home),
        "REPO_URL": f"file://{origin}",
        "VERSION": "v9.9.9",
        # Deliberately without ~/.local/bin, to exercise the PATH hint.
        "PATH": os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                if ".local/bin" not in p),
        **(extra_env or {}),
    })
    return subprocess.run(["bash", str(INSTALLER)], env=env,
                          capture_output=True, text=True, timeout=300)


def test_full_install_puts_a_working_cli_on_path(real_origin, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = full_install(real_origin, fake_home)

    assert result.returncode == 0, result.stdout + result.stderr
    plugin = fake_home / ".hermes" / "plugins" / "antharmaya-bridge"
    assert (plugin / "__init__.py").is_file()
    wrapper = fake_home / ".local" / "bin" / "memory-bridge"
    assert os.access(wrapper, os.X_OK)
    assert "is not on your PATH" in result.stdout

    from memory_bridge import __version__
    ran = subprocess.run([str(wrapper), "--version"], capture_output=True, text=True,
                         env={"HOME": str(fake_home), "PATH": os.environ["PATH"]})
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == f"memory-bridge {__version__}"

    stats = subprocess.run([str(wrapper), "stats"], capture_output=True, text=True,
                           env={"HOME": str(fake_home), "PATH": os.environ["PATH"]})
    assert stats.returncode == 0, stats.stderr
    assert (fake_home / ".hermes" / "antharmaya-memory" / "index.db").is_file()


def test_full_install_never_replaces_a_foreign_memory_bridge(real_origin, tmp_path):
    fake_home = tmp_path / "home"
    bin_dir = fake_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    foreign = bin_dir / "memory-bridge"
    foreign.write_text("#!/bin/sh\necho pip-installed\n")

    result = full_install(real_origin, fake_home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert foreign.read_text() == "#!/bin/sh\necho pip-installed\n"
    assert "not written by this installer" in result.stdout
