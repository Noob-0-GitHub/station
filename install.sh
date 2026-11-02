#!/usr/bin/env bash
# Install or upgrade Station from an extracted release directory.
#
#   ./install.sh [TARGET]        TARGET defaults to ~/station
#
# Environment: STATION_ROOT (runtime directory, default ~/.station),
#              PYTHON (default python3).
#
# Fresh install: copies the release to TARGET, makes it a git repository
# (release commits on branch `upstream`, your work on `main`), creates the
# virtual environment and runs `station init`.
# Upgrade: commits this release on `upstream` and merges it into `main`.
# TARGET must have no uncommitted changes. On a merge conflict, resolve it,
# commit, and run this same command again.
set -euo pipefail

die() { echo "install.sh: $*" >&2; exit 1; }

SRC=$(cd "$(dirname "$0")" && pwd)
TARGET=$(realpath -m "${1:-$HOME/station}")
RUNTIME=$(realpath -m "${STATION_ROOT:-$HOME/.station}")
PY=${PYTHON:-python3}
[ -f "$SRC/RELEASE" ] || die "no RELEASE file next to install.sh; run it from an extracted release"
VERSION=$(head -n1 "$SRC/RELEASE")
[ "$SRC" != "$TARGET" ] || die "run install.sh from the extracted release, not from $TARGET"
case "$TARGET/" in "$SRC/"*) die "target must not be inside the extracted release" ;; esac
case "$SRC/" in "$TARGET/"*) die "extract the release outside the target repository" ;; esac
case "$RUNTIME/" in "$TARGET/"*) die "runtime directory must be outside the code repository" ;; esac
command -v git >/dev/null || die "git is required"
command -v "$PY" >/dev/null || die "$PY is required"
GIT=(git -C "$TARGET" -c user.name=Station -c user.email=station@localhost)

if [ ! -d "$TARGET/.git" ]; then
    if [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET")" ]; then
        die "$TARGET exists, is not empty and is not a Station repository"
    fi
    echo "Installing Station $VERSION into $TARGET"
    mkdir -p "$TARGET"
    cp -a "$SRC/." "$TARGET/"
    "$PY" "$TARGET/station/release.py" "$TARGET"
else
    "${GIT[@]}" rev-parse -q --verify refs/heads/upstream >/dev/null || die "$TARGET has no upstream branch"
    [ "$("${GIT[@]}" rev-parse --abbrev-ref HEAD)" = main ] || die "$TARGET is not on branch main"
    [ -z "$("${GIT[@]}" status --porcelain)" ] || die "$TARGET has uncommitted changes; commit or stash them first"
    parent=$("${GIT[@]}" rev-parse upstream)
    index=$(mktemp)
    trap 'rm -f "$index"' EXIT
    rm -f "$index"
    # Stage the release tree in a scratch index, without touching main's work tree.
    GIT_INDEX_FILE="$index" "${GIT[@]}" --work-tree="$SRC" add -A
    tree=$(GIT_INDEX_FILE="$index" "${GIT[@]}" write-tree)
    if [ "$tree" = "$("${GIT[@]}" rev-parse "upstream^{tree}")" ]; then
        echo "Release $VERSION is already on upstream"
    else
        echo "Upgrading $TARGET to Station $VERSION"
        commit=$(echo "Station $VERSION" | "${GIT[@]}" commit-tree "$tree" -p "$parent")
        "${GIT[@]}" update-ref refs/heads/upstream "$commit" "$parent"
    fi
    if ! "${GIT[@]}" merge -q --no-edit upstream; then
        echo >&2
        echo "Merge conflicts. Resolve them in $TARGET, commit, then run this command again." >&2
        exit 1
    fi
fi

cd "$TARGET"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.lock
# A plain path (.pth) also works from HOME, where ~/station would otherwise
# shadow the real package as a namespace before setuptools' editable finder.
.venv/bin/python -m pip install -q --disable-pip-version-check --no-deps --config-settings editable_mode=compat -e .
.venv/bin/station --root "$RUNTIME" init --home "$HOME" --docs-dir "$TARGET/docs" --code-dir "$TARGET"

echo
echo "Station $VERSION is installed in $TARGET (branch main)."
echo "Runtime directory: $RUNTIME. Restart Station for the new code to take effect."
