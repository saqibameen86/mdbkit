@echo off
REM ============================================================
REM  publish.bat - release mdbkit in one command.
REM  Safe to run repeatedly; it repairs git state every time.
REM ============================================================
setlocal EnableDelayedExpansion

set REPO=https://github.com/saqibameen86/mdbkit.git

REM --- your git identity ---------------------------------------
REM GIT_EMAIL decides who GitHub credits for each commit. This is
REM your GitHub noreply address from Settings - Emails. Do NOT use
REM your real Gmail here - it would be published in every commit.
set GIT_NAME=Saqib Ameen Subhan
set GIT_EMAIL=288828588+saqibameen86@users.noreply.github.com

echo.
echo === mdbkit publish ===
echo.

if not exist pyproject.toml (
  echo ERROR: pyproject.toml not found. Run this inside the mdbkit folder.
  goto :end
)

REM --- read version with findstr (no fragile quoting) ---------
set VERSION=
for /f "tokens=3 delims= " %%v in ('findstr /b /c:"version = " pyproject.toml') do set VERSION=%%v
set VERSION=!VERSION:"=!
if "!VERSION!"=="" (
  echo ERROR: could not read version from pyproject.toml
  goto :end
)
echo Version to publish: !VERSION!
echo Commit author:      %GIT_NAME% ^<%GIT_EMAIL%^>
echo.

echo [1/5] Preparing git...
if not exist .git ( git init -q )
git config user.name "%GIT_NAME%"
git config user.email "%GIT_EMAIL%"
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin %REPO%
echo       branch: main   origin set
echo.

echo [2/5] Committing...
git add -A
git commit -q -m "v!VERSION!"
if errorlevel 1 echo       nothing new to commit ^(already committed^)
echo.

echo [3/5] Pushing to GitHub...
echo       username: saqibameen86
echo       password: paste your Personal Access Token ^(not your password^)
git push --force origin main
if errorlevel 1 (
  echo.
  echo ERROR: push failed - most likely a wrong or expired token.
  echo Make a new token: GitHub - Settings - Developer settings -
  echo   Personal access tokens - Tokens ^(classic^), tick "repo", and retry.
  goto :end
)
echo.

echo [4/5] Building...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
python -m build
if errorlevel 1 (
  echo ERROR: build failed. Try:  python -m pip install --upgrade build
  goto :end
)
echo.

echo [5/5] Uploading !VERSION! to PyPI...
echo       paste your pypi- token at the prompt ^(it stays invisible^)
python -m twine upload dist/mdbkit-!VERSION!*
if errorlevel 1 (
  echo.
  echo ERROR: upload failed. If it says "File already exists", this version
  echo is already on PyPI - bump the version and try again.
  goto :end
)

echo.
echo ============================================================
echo  Done. v!VERSION! is on GitHub and PyPI.
echo.
echo  Last step in the browser:
echo    https://github.com/saqibameen86/mdbkit/releases/new
echo    Tag: v!VERSION!   Label: None ^(Latest release^)
echo.
echo  Verify:  pip install --upgrade mdbkit  ^&^&  mdbkit --version
echo ============================================================

:end
endlocal
