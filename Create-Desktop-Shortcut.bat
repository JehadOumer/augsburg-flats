@echo off
setlocal
set TARGET=%~dp0Update-Listings.bat
set LINK=%USERPROFILE%\Desktop\Update Augsburg Flats.lnk
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%LINK%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 1; $s.Description = 'Scrape listings and push to GitHub Pages'; $s.Save()"
echo Created shortcut:
echo   %LINK%
pause
