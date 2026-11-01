# Create/update a direct Windows .lnk to the local portable executable.
[CmdletBinding()]
param(
    [string]$Exe = 'D:\Java-p\GeoPublisher-Workbench-Portable\Current\GeoPublisher-Workbench.exe',
    [string]$ShortcutName = 'GeoPublisher Workbench.lnk'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { throw "Executable is missing: $Exe" }
$Exe = (Resolve-Path -LiteralPath $Exe).Path
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
if (-not (Test-Path -LiteralPath $desktop -PathType Container)) { throw "Windows desktop unavailable: $desktop" }
$shortcutPath = Join-Path $desktop $ShortcutName
$shell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $shortcutPath) {
    $existing = $shell.CreateShortcut($shortcutPath)
    if ($existing.TargetPath -ne $Exe) {
        $backup = Join-Path (Split-Path -Parent (Split-Path -Parent $Exe)) 'shortcut-backups'
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        Copy-Item -LiteralPath $shortcutPath -Destination (Join-Path $backup "GeoPublisher-Workbench-$stamp.lnk")
    }
}
$link = $shell.CreateShortcut($shortcutPath)
$link.TargetPath = $Exe
$link.Arguments = ''
$link.WorkingDirectory = Split-Path -Parent $Exe
$link.Description = 'GeoPublisher Workbench - GeoServer 自动发布工作台'
$link.IconLocation = "$Exe,0"
$link.WindowStyle = 1
$link.Save()
$verified = $shell.CreateShortcut($shortcutPath)
if ($verified.TargetPath -ne $Exe -or $verified.Arguments -ne '') { throw 'Shortcut target did not persist correctly' }
"SHORTCUT=$shortcutPath"
"TARGET=$($verified.TargetPath)"
"ICON=$($verified.IconLocation)"
