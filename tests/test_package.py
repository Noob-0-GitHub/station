"""Release archives, repository bootstrap, and real offline installation."""

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

from station.cli import main

REPO = Path(__file__).resolve().parent.parent


def command(*args, cwd=None, env=None, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def git(repo, *args):
    return command("git", "-C", str(repo), *args).stdout.strip()


def load_package():
    spec = importlib.util.spec_from_file_location("package", REPO / "scripts" / "package.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def release_repo(tmp_path):
    """Commit a real snapshot of the current release inputs, not development HEAD."""
    mod = load_package()
    repo = tmp_path / "source"
    repo.mkdir()
    for name in mod.SHIP:
        src, dest = REPO / name, repo / name
        if src.is_dir():
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dest)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Station Test")
    git(repo, "config", "user.email", "test@localhost")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "release fixture")
    mod.REPO = repo
    return mod, repo


def extract_release(mod, directory):
    target = mod.build(directory)
    with tarfile.open(target) as tar:
        top = tar.getnames()[0].split("/")[0]
        for member in tar.getmembers():
            relative = Path(member.name)
            assert not relative.is_absolute() and ".." not in relative.parts
            dest = directory / relative
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                assert member.isfile()  # fixture archives contain no links
                stream = tar.extractfile(member)
                assert stream
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(stream.read())
                dest.chmod(member.mode)
    return directory / top


def test_release_contains_only_what_the_ai_needs(tmp_path, release_repo):
    mod, repo = release_repo
    # Uncommitted files, including credentials, must never enter the archive.
    (repo / "station" / "uncommitted.py").write_text("not shipped\n")
    (repo / "station" / "local.env").write_text("TEST_ONLY=not-a-secret\n")
    target = mod.build(tmp_path / "out")
    with tarfile.open(target) as tar:
        names = tar.getnames()
        top = names[0].split("/")[0]
        rel = {n[len(top) + 1:] for n in names if "/" in n}
        stream = tar.extractfile(f"{top}/RELEASE")
        assert stream
        release = stream.read().decode()
        mode = tar.getmember(f"{top}/install.sh").mode
    assert all(n == top or n.startswith(top + "/") for n in names)
    for needed in ("install.sh", "requirements.lock", "pyproject.toml", "RELEASE", "docs/README.md",
                   "station/loop.py", "station/release.py", "station/templates/terms.md",
                   "station/templates/skills/board/SKILL.md"):
        assert needed in rel
    for banned in ("DESIGN.md", "README.md", "tests/", "dev/", "research/", "probes/", "scripts/"):
        assert not any(n == banned or n.startswith(banned) for n in rel), banned
    assert "station/uncommitted.py" not in rel and "station/local.env" not in rel
    assert release.splitlines()[0] == top.removeprefix("station-")
    assert release.splitlines()[1] == "commit " + git(repo, "rev-parse", "HEAD")
    assert mode & stat.S_IXUSR


def test_release_rejects_committed_credentials(tmp_path, release_repo):
    mod, repo = release_repo
    (repo / "station" / "leak.env").write_text("TEST_ONLY=not-a-secret\n")
    git(repo, "add", "-f", "station/leak.env")
    git(repo, "commit", "-qm", "bad fixture")
    with pytest.raises(SystemExit, match="refusing to ship"):
        mod.build(tmp_path / "bad")


@pytest.mark.parametrize("kind", ["json", "symlink"])
def test_release_rejects_unexpected_files_and_links(tmp_path, release_repo, kind):
    mod, repo = release_repo
    path = repo / "docs" / ("unexpected.json" if kind == "json" else "linked.md")
    if kind == "json":
        path.write_text("{}\n")
    else:
        path.symlink_to("README.md")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "bad fixture")
    with pytest.raises(SystemExit, match="refusing to ship"):
        mod.build(tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_installer_rejects_unsafe_targets(tmp_path, release_repo):
    mod, _ = release_repo
    source = extract_release(mod, tmp_path / "release")
    for target, error in ((source, "not from"), (source / "nested", "inside"),
                          (source.parent, "outside the target")):
        result = command("bash", str(source / "install.sh"), str(target), check=False)
        assert result.returncode != 0 and error in result.stderr
    other = tmp_path / "unrelated"
    other.mkdir()
    (other / "important.txt").write_text("preserve\n")
    result = command("bash", str(source / "install.sh"), str(other), check=False)
    assert result.returncode != 0 and "not empty" in result.stderr
    assert (other / "important.txt").read_text() == "preserve\n"
    target = tmp_path / "new-target"
    env = {**os.environ, "STATION_ROOT": str(target / "runtime")}
    result = command("bash", str(source / "install.sh"), str(target), env=env, check=False)
    assert result.returncode != 0 and "outside the code repository" in result.stderr
    assert not target.exists()


def test_init_rejects_symlink_auth(tmp_path):
    root, dest = tmp_path / "runtime", tmp_path / "other"
    root.mkdir()
    dest.mkdir(mode=0o755)
    (root / "auth").symlink_to(dest, target_is_directory=True)
    before = stat.S_IMODE(dest.stat().st_mode)
    with pytest.raises(ValueError, match="symlink"):
        main(["--root", str(root), "init", "--home", str(tmp_path)])
    assert stat.S_IMODE(dest.stat().st_mode) == before


def test_init_production_config(tmp_path):
    root, home, docs = tmp_path / ".station", tmp_path, tmp_path / "station" / "docs"
    assert main(["--root", str(root), "init", "--home", str(home), "--docs-dir", str(docs)]) == 0
    cfg = yaml.safe_load((root / "station.yaml").read_text())
    assert cfg["home"] == str(home.resolve()) and cfg["docs_dir"] == str(docs.resolve())
    assert cfg["secrets"]["dir"] == str(root.resolve() / "auth")
    assert stat.S_IMODE(os.stat(root / "auth").st_mode) == 0o700
    assert (root / "state").is_dir() and (root / "inbox").is_dir()
    before = (root / "station.yaml").read_text()
    main(["--root", str(root), "init", "--home", "/elsewhere"])
    assert (root / "station.yaml").read_text() == before


def test_init_dev_config_unchanged(tmp_path):
    main(["--root", str(tmp_path), "init"])
    cfg = yaml.safe_load((tmp_path / "station.yaml").read_text())
    assert "home" not in cfg and "secrets" not in cfg and not (tmp_path / "auth").exists()


def test_init_bootstraps_release_repo(tmp_path, release_repo):
    mod, _ = release_repo
    code = extract_release(mod, tmp_path / "release")
    assert main(["--root", str(tmp_path / "runtime"), "init", "--code-dir", str(code)]) == 0
    before = git(code, "rev-parse", "HEAD")
    assert git(code, "branch", "--show-current") == "main"
    assert git(code, "rev-parse", "upstream") == before
    # Plain git commits by the AI work even without a global identity.
    (code / "note.txt").write_text("mine\n")
    git(code, "add", "note.txt")
    git(code, "commit", "-qm", "personal change")
    changed = git(code, "rev-parse", "HEAD")
    assert changed != before
    main(["--root", str(tmp_path / "runtime"), "init", "--code-dir", str(code)])
    assert git(code, "rev-parse", "HEAD") == changed


def test_installer_real_offline_upgrade_and_conflict(tmp_path, release_repo):
    wheelhouse = os.environ.get("STATION_TEST_WHEELHOUSE")
    if not wheelhouse:
        pytest.skip("set STATION_TEST_WHEELHOUSE to cached runtime/build wheels for real offline install")
    mod, repo = release_repo
    (repo / "docs/obsolete.md").write_text("removed in the next release\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial obsolete page")
    release1 = extract_release(mod, tmp_path / "release1")
    home, target, runtime = tmp_path / "home", tmp_path / "home" / "station", tmp_path / "home" / ".station"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "STATION_ROOT": str(runtime),
           "PYTHON": sys.executable, "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheelhouse,
           "PIP_CONFIG_FILE": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)

    def install(src, check=True):
        return command("bash", str(src / "install.sh"), str(target), cwd=tmp_path, env=env, check=check)

    install(release1)
    assert git(target, "branch", "--show-current") == "main"
    assert git(target, "status", "--porcelain") == ""
    probe = command(str(target / ".venv/bin/python"), "-c",
                    "import sys,station,sqlite_vec; print(sys.prefix); print(station.__file__); print(sqlite_vec.__file__)", cwd=home, env=env).stdout
    assert str(target / ".venv") in probe and str(target / "station/__init__.py") in probe
    assert "0.87.0" in probe
    # The launcher is installed next to the code, executable, and ignored by Git.
    assert os.access(target / "launcher.py", os.X_OK)
    assert (target / "launcher.py").read_bytes() == (target / "station" / "launcher.py").read_bytes()
    assert git(target, "check-ignore", "launcher.py") == "launcher.py"
    config = (runtime / "station.yaml").read_text()
    (runtime / "state" / "preserve.txt").write_text("keep history\n")
    (target / "personal.md").write_text("my own changes\n")
    refused = install(release1, check=False)
    assert refused.returncode != 0 and "uncommitted changes" in refused.stderr
    git(target, "add", "personal.md")
    git(target, "commit", "-qm", "personal work")
    original_head = git(target, "rev-parse", "HEAD")
    (repo / "docs" / "new.md").write_text("new upstream docs\n")
    (repo / "docs/obsolete.md").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "second release")
    release2 = extract_release(mod, tmp_path / "release2")
    install(release2)
    assert (target / "docs/new.md").read_text() == "new upstream docs\n"
    assert not (target / "docs/obsolete.md").exists()
    assert (target / "personal.md").read_text() == "my own changes\n"
    assert (runtime / "station.yaml").read_text() == config
    assert (runtime / "state/preserve.txt").read_text() == "keep history\n"
    assert git(target, "merge-base", "--is-ancestor", original_head, "HEAD") == ""
    after = git(target, "rev-parse", "HEAD")
    install(release2)
    assert git(target, "rev-parse", "HEAD") == after
    assert git(target, "status", "--porcelain") == ""
    # The same line changed locally and upstream: real Git conflict, then abort.
    page = target / "docs/new.md"
    page.write_text("my version\n")
    git(target, "add", "-A")
    git(target, "commit", "-qm", "local edit")
    before_conflict = git(target, "rev-parse", "HEAD")
    (repo / "docs/new.md").write_text("upstream version\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "third release")
    release3 = extract_release(mod, tmp_path / "release3")
    result = install(release3, check=False)
    assert result.returncode != 0 and "Merge conflicts" in result.stderr
    assert "<<<<<<< HEAD" in page.read_text()
    git(target, "merge", "--abort")
    assert git(target, "rev-parse", "HEAD") == before_conflict
    assert page.read_text() == "my version\n"
    # Resolve on the next attempt, then retry installer to finish dependency init.
    install(release3, check=False)
    page.write_text("resolved version\n")
    git(target, "add", "-A")
    git(target, "commit", "-qm", "resolve conflict")
    install(release3)
    assert page.read_text() == "resolved version\n"
    assert git(target, "status", "--porcelain") == ""
    # Exercise the documented rollback with both old and failed heads retained.
    failed_head = git(target, "rev-parse", "HEAD")
    git(target, "branch", "failed-upgrade-test", failed_head)
    git(target, "branch", "before-upgrade-test", original_head)
    git(target, "reset", "--keep", "before-upgrade-test")
    assert git(target, "rev-parse", "HEAD") == original_head
    assert git(target, "rev-parse", "failed-upgrade-test") == failed_head
    assert (target / "personal.md").read_text() == "my own changes\n"
    assert (runtime / "state/preserve.txt").read_text() == "keep history\n"
    assert (runtime / "station.yaml").read_text() == config
    for args in (("-r", "requirements.lock"),
                 ("--no-deps", "--config-settings", "editable_mode=compat", "-e", ".")):
        command(str(target / ".venv/bin/python"), "-m", "pip", "install", *args, cwd=target, env=env)
    assert git(target, "status", "--porcelain") == ""
