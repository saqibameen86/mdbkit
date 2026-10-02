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

REM --- refuse to republish: PyPI never accepts a version twice, and an
REM --- old folder would force-push old history over GitHub.
set PYPI_STATUS=000
for /f %%s in ('curl -s -o nul -w "%%{http_code}" --max-time 15 https://pypi.org/pypi/mdbkit/!VERSION!/json 2^>nul') do set PYPI_STATUS=%%s
if "!PYPI_STATUS!"=="200" (
  echo ERROR: mdbkit !VERSION! is already on PyPI.
  echo        This folder holds an old release. Unpack the newest download.
  goto :end
)
if not "!PYPI_STATUS!"=="404" (
  echo WARNING: could not check PyPI for !VERSION! ^(HTTP !PYPI_STATUS!^); continuing.
  echo.
)

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
REM Some Pythons refuse installs into the base environment, so keep the
REM build tools in a private venv beside the project. Created once, reused.
set BUILDPY=python
python -c "import build, twine" >nul 2>&1
if errorlevel 1 (
  if not exist .venv\Scripts\python.exe (
    echo       creating .venv for build tools ^(one time only^)
    python -m venv .venv
  )
  echo       installing build and twine into .venv
  .venv\Scripts\python -m pip install --quiet --upgrade pip build twine
  set BUILDPY=.venv\Scripts\python
)
echo       using: !BUILDPY!
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
!BUILDPY! -m build
if errorlevel 1 (
  echo ERROR: build failed. Try:  python -m pip install --upgrade build
  goto :end
)
echo.

echo [5/5] Uploading !VERSION! to PyPI...
echo       paste your pypi- token at the prompt ^(it stays invisible^)
!BUILDPY! -m twine upload dist/mdbkit-!VERSION!*
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
