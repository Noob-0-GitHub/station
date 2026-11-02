# Install, change, and upgrade Station

You are Station's user. The program and its documentation are yours to read and modify. The installer never starts Station, calls a model, creates an OS user, or installs a service.

The packaging workflow exists now. The operator still has to arrange production credentials, browser binaries, permissions and service management separately. A successful install does not mean those are configured.

## Requirements

- Linux, Bash, Git, `realpath`, and Python 3.11 or newer with `venv` and pip.
- Permission to write your home and the target directory. Run as your own unprivileged OS user, not root.
- Access to Python package downloads, or a local wheel directory containing the runtime dependencies and build tools (`setuptools>=68`, `wheel`, and their dependencies).

Runtime dependency versions are pinned in `requirements.lock`; Python build tools are not pinned. This is not a hermetic or fully reproducible build.

## Install a release

Use a release archive made by the operator. `VERSION` below is a placeholder for its version, not text to type literally. Extract outside the target directory:

```bash
mkdir -p "$HOME/downloads"
tar -xzf /path/to/station-VERSION.tar.gz -C "$HOME/downloads"
bash "$HOME/downloads/station-VERSION/install.sh"
```

The defaults are:

- Code, docs and a Python virtual environment: `~/station/`.
- Runtime config, state and inbox: `~/.station/`.
- Your home: `$HOME`, not `~/.station/home`.
- API credential directory: `~/.station/auth/` (mode 700).

To choose another code or runtime directory (keep the runtime outside the code repository):

```bash
STATION_ROOT="$HOME/.station" PYTHON=python3 \
  bash "$HOME/downloads/station-VERSION/install.sh" "$HOME/station"
```

The installer copies only the release files, creates Git branches `upstream` and `main`, creates `.venv`, installs the locked runtime dependencies and an editable Station package, and runs `station init`. Your work belongs on `main`; `upstream` records the operator's releases. A local Git identity (`Station <station@localhost>`) is configured so commits work without a global config. You can change it.

For an offline install, provide all required wheels through pip's normal environment settings:

```bash
PIP_NO_INDEX=1 PIP_FIND_LINKS=/path/to/wheels \
  bash "$HOME/downloads/station-VERSION/install.sh"
```

## Initialize without the installer

With the Station package already installed, this initializes the runtime and the Git repository of an extracted release:

```bash
station --root "$HOME/.station" init \
  --home "$HOME" --docs-dir "$HOME/station/docs" --code-dir "$HOME/station"
```

`--code-dir` must be an extracted release containing `RELEASE` and `pyproject.toml`. An existing repository is left alone. When running from a release, `init` also detects its code directory automatically. Without `--home`, development mode keeps the home under the runtime directory.

An existing `station.yaml` is never overwritten. In particular, passing new paths to `init` does not migrate an existing configuration; edit it deliberately if you move the installation. Registration files are created on registration, not by installation.

## Inspect the installation

These commands do not call a model:

```bash
cd "$HOME"
"$HOME/station/.venv/bin/python" -c \
  'import sys, station; print(sys.prefix); print(station.__file__)'
"$HOME/station/.venv/bin/station" --root "$HOME/.station" status
git -C "$HOME/station" status
```

Both Python paths should point into your installation, not the operator's development checkout. Run Station only after its provider credentials and other prerequisites have been configured:

```bash
"$HOME/station/.venv/bin/station" --root "$HOME/.station" run
```

That last command calls the model. It is not an installation check.

## Change Station

The source is `~/station/station/`; prompts and seed files are in `station/templates/`. The editable install uses a plain path entry so imports also work from your home directory, despite the enclosing directory also being called `station`.

Read the code, make a focused change, check it, and commit working versions. Commit your changes before an upgrade; the installer refuses a dirty working tree. Avoid storing credentials in the repository. Git ignores common credential paths, but cannot detect arbitrary secrets in files you choose to track.

Prompts in `station/templates/` are read each time they are used, so an edit applies to the next model call. Editing a template does not rewrite files already seeded in your home.

Code (`station/*.py`) and `~/.station/station.yaml` are loaded once per process. While you are asleep, Station checks them every few seconds. When they changed, it runs `station check` in a separate process against the new files (every module imports, the config and templates load, no model call). If that passes, Station restarts itself into the new code and resumes the same sleep; at your next wake an event says which files were loaded. If it fails, the old code keeps running and you get the error as an event; the same broken state is not retried until the files change again. Nothing restarts in the middle of a turn, so a half-finished edit is never loaded while you work. You can run the same check yourself:

```bash
"$HOME/station/.venv/bin/station" --root "$HOME/.station" check
```

`station check` only proves the code imports and the config loads. A change that imports but misbehaves at runtime is still loaded. Commit working versions so you can go back. Set `reload.enabled: false` in `station.yaml` to turn automatic restarts off.

## Checkpoints and automatic rollback

In production Station runs under `~/station/launcher.py`. It is a copy of `station/launcher.py`, installed by `station init`, and listed in `.gitignore`, so checkpoints and rollbacks never change it and a broken commit cannot break it. It is yours to read and edit like everything else; a reinstall replaces it and keeps your copy as `launcher.py.prev`.

Every time it starts `station run` (first start, after a hot reload, after a crash), the launcher takes a checkpoint of `~/station`: every tracked file, plus untracked files Git does not ignore, except untracked files over 1 MB. Your branches, HEAD and staging area are not touched; the checkpoint is a separate commit object.

- When that run completes its first turn, its checkpoint becomes the good one (`refs/station/good`). The newest 20, at most 30 days old, are kept under `refs/station/checkpoints/`.
- If the run instead fails with a Python error or a crash signal before completing a turn, and its code differs from the good checkpoint, the launcher restores the good checkpoint into the working tree and starts again. The version that crashed is kept on branch `station-failed/<time>`. You get a `rollback` event saying so, and the restart line says the code was rolled back.
- Not rolled back: a run that completed a turn and failed later (the code worked; the cause is somewhere else), a run stopped by the network, rate limits or the config, and a run killed with SIGKILL or SIGTERM (the operator, a timeout or the out-of-memory killer).

After a rollback HEAD stays where it was, so `git status` shows the restored files as changes. Look at the failed branch, then commit the rollback or fix forward:

```bash
cd ~/station
git log --oneline -3 station-failed/<time>     # what crashed
git diff station-failed/<time> -- station/     # what the rollback undid
git commit -am "Back to the last working version"
```

Only the code directory is rolled back. The runtime directory (`~/.station`), the rest of your home and ignored files are left alone.

**Keep your data out of `~/station`.** Notes, downloads, projects, databases and anything else you want to keep belong elsewhere in your home. The code directory is for Station's code and config. A file you add there is part of the next checkpoint, and a rollback deletes files added after the good checkpoint (they stay on the `station-failed/` branch, but you would have to notice and fetch them).

After any crash the launcher starts Station again after 10 seconds, doubling up to 30 minutes while it keeps crashing, and back to 10 seconds once a run has lasted 10 minutes.

Stop the process before changing its live state (`state/`) or upgrading its code; otherwise an in-progress call may see a mixture of versions.

## Upgrade

1. Stop Station through the operator's process manager. The installer does not do this for you.
2. Save or commit your code changes. Keep a rollback branch and a private backup of the runtime and any home files you care about. The runtime contains history and may contain credentials; do not upload it or put it in Git.
3. Extract the new archive outside `~/station` and run its `install.sh` with the same target and `STATION_ROOT`.
4. Check the result, then restart Station through the same process manager.

For example, before installing:

```bash
cd "$HOME/station"
git status
# Continue only when the working tree is clean.
BACKUP="before-upgrade-$(date +%Y%m%d-%H%M%S)"
git branch "$BACKUP"
printf 'Rollback branch: %s\n' "$BACKUP"
```

An upgrade stages the new release in a temporary Git index, records it on `upstream`, and merges it into `main`. It preserves your committed changes when Git can merge them. Files removed upstream are removed unless they conflict with your work. Existing runtime config, state, credential files and home files are not copied over. Re-running the installer from the same extracted release is safe and does not create another release commit.

### Merge conflicts

The installer stops before changing dependencies. Resolve the conflicted files in `~/station`, `git add` and `git commit`, then rerun the installer from the same extracted archive. Or run `git merge --abort` to return the working tree to its pre-merge state. The `upstream` branch still records the new release; aborting does not erase it.

If a dependency install or initialization fails after the merge, keep Station stopped, fix that error, and rerun the same installer. The Git merge has already happened; a failed installation is not an automatic rollback.

### Roll back

Keep Station stopped. First preserve the failed upgrade on a branch, and ensure your working tree is clean (resolve or abort any merge first). Replace `BACKUP` below with the rollback branch you saved:

```bash
cd "$HOME/station"
git status
# Continue only when clean; preserve HEAD before moving main.
git branch "failed-upgrade-$(date +%Y%m%d-%H%M%S)" HEAD
git reset --keep BACKUP
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --config-settings editable_mode=compat -e .
```

This restores the code and dependencies, not runtime state or home files. Restore those from your private backup only if needed, with Station stopped. Check before restarting. `upstream` can remain at the newer release; rerunning its installer will attempt the merge again. The preserved branch keeps the failed upgrade's commits recoverable.
