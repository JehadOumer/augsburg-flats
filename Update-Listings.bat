@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================
echo  Augsburg Flats — scrape + push to GitHub
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found on PATH.
  echo Install Python 3.11+ and tick "Add to PATH".
  goto :end
)

echo [1/4] Ensuring Python packages...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo ERROR: pip install failed.
  goto :end
)

echo [2/4] Scraping listings and exporting JSON...
python -m pipeline.export_listings
if errorlevel 1 (
  echo ERROR: scrape/export failed.
  goto :end
)

echo [3/4] Staging site data...
git add site/data/listings.json site/data/config.json
git diff --cached --quiet
if errorlevel 1 (
  for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH:mm"') do set STAMP=%%i
  git commit -m "listings: refresh %STAMP%"
  if errorlevel 1 (
    echo ERROR: git commit failed.
    goto :end
  )
  echo [4/4] Pushing to GitHub...
  git push
  if errorlevel 1 (
    echo ERROR: git push failed. Check remote auth / branch.
    goto :end
  )
  echo.
  echo DONE — GitHub Pages will rebuild in ~1–2 minutes.
) else (
  echo [4/4] No listing changes to commit.
  echo DONE — site data already up to date.
)

:end
echo.
pause
