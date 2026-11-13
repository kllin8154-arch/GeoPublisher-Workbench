@echo off
setlocal
rem Offline portable shortcut installer. Uses only built-in Windows PowerShell/COM.
rem Put this CMD next to GeoPublisher-Workbench.exe. No internet, pip, or admin needed.
set "GEOPUB_OFFLINE_EXE=%~dp0GeoPublisher-Workbench.exe"
if not exist "%GEOPUB_OFFLINE_EXE%" (
  echo GeoPublisher-Workbench.exe is missing. Keep the complete portable folder together.
  exit /b 2
)
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $exe=$env:GEOPUB_OFFLINE_EXE; if($exe.StartsWith('\\')){throw 'Copy the portable folder to a local disk first'}; $desktop=[Environment]::GetFolderPath('DesktopDirectory'); if(-not(Test-Path -LiteralPath $desktop -PathType Container)){throw 'The user desktop folder is unavailable'}; $link=Join-Path $desktop 'GeoPublisher Workbench.lnk'; $ws=New-Object -ComObject WScript.Shell; $item=$ws.CreateShortcut($link); $item.TargetPath=$exe; $item.WorkingDirectory=Split-Path -Parent $exe; $item.Arguments=''; $item.IconLocation=$exe+',0'; $item.Description='GeoPublisher Workbench - Offline GeoServer Publisher'; $item.Save(); if($ws.CreateShortcut($link).TargetPath -ne $exe){throw 'Shortcut verification failed'}; Write-Output ('SHORTCUT_OK='+$link)"
if errorlevel 1 (
  echo Desktop shortcut was not created. You can also right-click the EXE and make a shortcut.
  exit /b 1
)
echo.
echo Desktop shortcut is ready. Double-click GeoPublisher Workbench to open.
exit /b 0
