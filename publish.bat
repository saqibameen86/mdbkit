@echo off
REM ============================================================
REM  publish.bat - release mdbkit in one command.
REM
REM  Usage:  publish.bat
REM
REM  Safe to run repeatedly. It fixes up git state every time, so
REM  it does not matter what shape the folder was left in.
REM ============================================================
setlocal EnableDelayedExpansion

set REPO=https://github.com/saqibameen86/mdbkit.git

echo.
echo === mdbkit publish ===
echo.

REM --- sanity: are we in the right folder? --------------------
if not exist pyproject.toml (
  echo ERROR: pyproject.toml not found.
  echo Run this from inside the mdbkit folder.
  exit /b 1
)

REM --- read the version --------------------------------------
for /f "delims=" %%v in ('python -c "import re,io;print(re.search(r'^version = \"(.*?)\"', io.open('pyproject.toml',encoding='utf-8').read(), re.M).group(1))"') do set VERSION=%%v
if "%VERSION%"=="" (
  echo ERROR: could not read version from pyproject.toml
  exit /b 1
)
echo Version to publish: %VERSION%
echo.

REM --- step 1: make git sane ---------------------------------
echo [1/5] Preparing git...
if not exist .git (
  echo       no repository here yet - initialising
  git init -q
)

REM branch must be main
git branch -M main

REM origin must exist and point at the right place
git remote remove origin >nul 2>&1
git remote add origin %REPO%

echo       branch: main
echo       origin: %REPO%
echo.

REM --- step 2: commit ----------------------------------------
echo [2/5] Committing...
git add -A
git commit -q -m "v%VERSION%" 2>nul
if errorlevel 1 echo       nothing new to commit ^(already committed^)
echo.

REM --- step 3: push ------------------------------------------
echo [3/5] Pushing to GitHub...
echo       ^(username: saqibameen86 - password: your Personal Access Token^)
git push --force origin main
if errorlevel 1 (
  echo.
  echo ERROR: push failed. Most likely the token was wrong or expired.
  echo Create a new one: GitHub - Settings - Developer settings -
  echo   Personal access tokens - Tokens ^(classic^) - Generate new token
  echo   Tick the "repo" scope, copy it, and run publish.bat again.
  exit /b 1
)
echo.

REM --- step 4: build -----------------------------------------
echo [4/5] Building the package...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
python -m build 2>nul
if errorlevel 1 (
  echo ERROR: build failed. Try:  python -m pip install --upgrade build
  exit /b 1
)
echo.

REM --- step 5: upload ----------------------------------------
echo [5/5] Uploading %VERSION% to PyPI...
echo       ^(paste your pypi- token at the prompt; it stays invisible^)
python -m twine upload dist/mdbkit-%VERSION%*
if errorlevel 1 (
  echo.
  echo ERROR: upload failed.
  echo If it says "File already exists", this version is already on PyPI -
  echo bump the version in pyproject.toml and mdbkit/__init__.py first.
  exit /b 1
)

echo.
echo ============================================================
echo  Done. v%VERSION% is on GitHub and PyPI.
echo.
echo  Last step, in the browser:
echo    https://github.com/saqibameen86/mdbkit/releases/new
echo    Tag: v%VERSION%   Label: None (Latest release)
echo.
echo  Verify the install:
echo    pip install --upgrade mdbkit
echo    mdbkit --version
echo ============================================================
endlocal
