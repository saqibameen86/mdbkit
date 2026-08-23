#!/usr/bin/env bash
# publish.sh — release mdbkit in one command (Linux / macOS).
# Safe to run repeatedly; it repairs git state every time.
set -euo pipefail

REPO="https://github.com/saqibameen86/mdbkit.git"
# GIT_EMAIL decides who GitHub credits. Check https://github.com/settings/emails
GIT_NAME="Saqib Ameen Subhan"
GIT_EMAIL="288828588+saqibameen86@users.noreply.github.com"

echo
echo "=== mdbkit publish ==="
echo

[ -f pyproject.toml ] || { echo "ERROR: run this from inside the mdbkit folder"; exit 1; }

# This script needs bash. `sh publish.sh` on macOS runs it in POSIX mode.
[ -n "${BASH_VERSION:-}" ] || { echo "ERROR: run with  bash publish.sh  (or ./publish.sh)"; exit 1; }

VERSION=$(python3 -c "import re,io;print(re.search(r'^version = \"(.*?)\"', io.open('pyproject.toml',encoding='utf-8').read(), re.M).group(1))")
echo "Version to publish: $VERSION"
echo

echo "[1/5] Preparing git..."
[ -d .git ] || { echo "      initialising repository"; git init -q; }
git config user.name "$GIT_NAME"
git config user.email "$GIT_EMAIL"
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin "$REPO"
echo "      branch: main"
echo "      origin: $REPO"
echo

echo "[2/5] Committing..."
git add -A
git commit -q -m "v$VERSION" 2>/dev/null || echo "      nothing new to commit"
echo

echo "[3/5] Pushing to GitHub..."
git push --force origin main
echo

echo "[4/5] Building..."

# Homebrew and system Pythons are "externally managed" (PEP 668), so build
# and twine cannot be installed into them. Keep a small private venv beside
# the project instead — created once, reused forever, never committed.
BUILDPY="python3"
if ! python3 -c "import build, twine" >/dev/null 2>&1; then
  if [ ! -x .venv/bin/python ]; then
    echo "      creating .venv for build tools (one time only)"
    python3 -m venv .venv
  fi
  echo "      installing build and twine into .venv"
  .venv/bin/python -m pip install --quiet --upgrade pip build twine
  BUILDPY=".venv/bin/python"
fi
echo "      using: $BUILDPY"

rm -rf dist build
"$BUILDPY" -m build
echo

echo "[5/5] Uploading $VERSION to PyPI..."
"$BUILDPY" -m twine upload dist/mdbkit-"$VERSION"*

cat <<EOF

============================================================
 Done. v$VERSION is on GitHub and PyPI.

 Last step, in the browser:
   https://github.com/saqibameen86/mdbkit/releases/new
   Tag: v$VERSION   Label: None (Latest release)

 Verify:
   pip install --upgrade mdbkit && mdbkit --version
============================================================
EOF
