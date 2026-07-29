@echo off
REM ============================================================
REM  fix-authorship.bat  -  ONE-TIME repair.
REM
REM  Past commits were made with the email saqib@users.noreply.github.com,
REM  which belongs to a DIFFERENT GitHub account (username "saqib").
REM  That is why the repo lists a contributor you do not recognise, and
REM  why your commits are not credited to you.
REM
REM  This rewrites every commit to your identity and force-pushes.
REM  Your code is unchanged. Tags and published releases are unaffected.
REM ============================================================
setlocal

set GIT_NAME=Saqib Ameen Subhan
set GIT_EMAIL=saqibameen86@users.noreply.github.com
set WRONG_EMAIL=saqib@users.noreply.github.com

echo.
echo This will rewrite commit authorship in this repository.
echo   from: %WRONG_EMAIL%
echo     to: %GIT_EMAIL%
echo.
set /p CONFIRM="Type yes to continue: "
if /i not "%CONFIRM%"=="yes" ( echo Cancelled. & exit /b 1 )

git config user.name "%GIT_NAME%"
git config user.email "%GIT_EMAIL%"

set FILTER_BRANCH_SQUELCH_WARNING=1
git filter-branch -f --env-filter ^
"if [ \"$GIT_AUTHOR_EMAIL\" = \"%WRONG_EMAIL%\" ]; then export GIT_AUTHOR_NAME=\"%GIT_NAME%\"; export GIT_AUTHOR_EMAIL=\"%GIT_EMAIL%\"; fi; if [ \"$GIT_COMMITTER_EMAIL\" = \"%WRONG_EMAIL%\" ]; then export GIT_COMMITTER_NAME=\"%GIT_NAME%\"; export GIT_COMMITTER_EMAIL=\"%GIT_EMAIL%\"; fi" ^
--tag-name-filter cat -- --branches --tags

if errorlevel 1 (
  echo.
  echo Rewrite failed. This is optional cleanup - your code is fine either way.
  echo Just run publish.bat; future commits will be credited correctly.
  exit /b 1
)

echo.
echo Pushing corrected history and tags...
git push --force --tags origin main

echo.
echo Done. Check https://github.com/saqibameen86/mdbkit/graphs/contributors
echo (GitHub can take a few minutes to refresh the contributor list.)
endlocal
