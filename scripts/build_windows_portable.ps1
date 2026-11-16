# Build a D:-drive Windows portable developer preview with the locked Pixi GIS runtime.
# Do not use for public distribution until third-party license texts and clean-room tests pass.
[CmdletBinding()]
param(
    [string]$OutputRoot = 'D:\Java-p\GeoPublisher-Workbench-Portable',
    [switch]$Resume
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvPython = Join-Path $repo '.venv\Scripts\python.exe'
$pixi = Join-Path $repo '.pixi\envs\default'
$icon = Join-Path $repo 'assets\geopublisher.ico'
$arcmapResources = Join-Path $repo 'src\geopublisher\resources'
$offlineShortcut = Join-Path $repo 'scripts\Install-Offline-Shortcut.cmd'
if (-not (Test-Path -LiteralPath $offlineShortcut -PathType Leaf)) { throw 'Missing offline shortcut helper' }
if (-not (Test-Path (Join-Path $arcmapResources 'arcmap_mxd_to_shp.py')) -or -not (Test-Path (Join-Path $arcmapResources 'arcmap_create_synthetic_mxd.py'))) { throw 'Missing optional offline ArcMap helpers.' }
$entry = Join-Path $repo 'scripts\desktop_entry.py'
$final = Join-Path $OutputRoot 'Current'
$buildRoot = Join-Path $OutputRoot '_build'
$distRoot = Join-Path $buildRoot 'portable-dist'
$workRoot = Join-Path $buildRoot 'pyinstaller-work'
$specRoot = Join-Path $buildRoot 'spec'
$application = Join-Path $distRoot 'GeoPublisher-Workbench'
if (-not (Test-Path $venvPython) -or -not (Test-Path (Join-Path $pixi 'Library\bin\gdalinfo.exe'))) {
    throw 'Developer venv or locked Windows GDAL runtime is missing.'
}
if ($OutputRoot -match '[^\x20-\x7e]') { throw 'Use an ASCII-only install path for this pinned Windows PROJ runtime; the data folder may contain Chinese characters.' }
if (Test-Path $final) { throw "Refusing to overwrite an installed release: $final" }
if ((Test-Path $application) -and (-not $Resume)) { throw "Refusing to reuse an old staging release: $application" }
if ($Resume -and -not (Test-Path (Join-Path $application 'runtime\Library\share\proj\proj.db'))) { throw 'Cannot resume an incomplete staged runtime' }
if (-not $Resume) {
    Push-Location $repo
    try {
        $dirty = @(git status --porcelain)
        if ($LASTEXITCODE -ne 0 -or $dirty.Count -ne 0) { throw 'Build only from a clean, committed working tree.' }
    } finally { Pop-Location }
New-Item -ItemType Directory -Force -Path $OutputRoot,$buildRoot,$distRoot,$workRoot,$specRoot | Out-Null
if (-not (Test-Path $icon)) { & $venvPython (Join-Path $repo 'scripts\make_windows_icon.py'); if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed' } }
$env:PYINSTALLER_CONFIG_DIR = Join-Path $buildRoot 'pyinstaller-cache'
Push-Location $repo
try {
    & $venvPython -m PyInstaller --noconfirm --clean --windowed --onedir --name GeoPublisher-Workbench --paths (Join-Path $repo 'src') --add-data "$arcmapResources;geopublisher/resources" --icon $icon --distpath $distRoot --workpath $workRoot --specpath $specRoot $entry
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
} finally { Pop-Location }
if (-not (Test-Path (Join-Path $application 'GeoPublisher-Workbench.exe'))) { throw 'Missing packaged Windows exe' }
$runtime = Join-Path $application 'runtime'
function CopyRuntime([string]$source,[string]$target,[string]$filter='*') {
    if (-not (Test-Path $source)) { throw "Missing locked runtime source: $source" }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    & robocopy $source $target $filter /E /R:1 /W:1 /NFL /NDL /NP /NJH /NJS | Out-Null
    $rc = $LASTEXITCODE
    if ($rc -gt 7) { throw "Runtime copy failed (robocopy $rc): $source" }
}
CopyRuntime (Join-Path $pixi 'Library\bin') (Join-Path $runtime 'Library\bin')
CopyRuntime (Join-Path $pixi 'Library\share') (Join-Path $runtime 'Library\share')
if (Test-Path (Join-Path $pixi 'Library\lib\gdalplugins')) {
    CopyRuntime (Join-Path $pixi 'Library\lib\gdalplugins') (Join-Path $runtime 'Library\lib\gdalplugins')
}
CopyRuntime (Join-Path $pixi 'conda-meta') (Join-Path $runtime 'conda-meta') '*.json'
}
$runtime = Join-Path $application 'runtime'
$bin = Join-Path $runtime 'Library\bin'
$share = Join-Path $runtime 'Library\share'
$exe = Join-Path $application 'GeoPublisher-Workbench.exe'
if (-not (Test-Path (Join-Path $share 'proj\proj.db')) -or -not (Test-Path (Join-Path $share 'gdal'))) { throw 'Incomplete GDAL/PROJ data directory' }
Copy-Item -LiteralPath $offlineShortcut -Destination (Join-Path $application 'Install-Offline-Shortcut.cmd') -ErrorAction Stop
foreach ($guide in @('QUICKSTART-ZH.md', 'FIELD-SHEET-SPLIT-P0B.md', 'SHEET-REVIEW-P0B.md', 'SHEET-BOUNDARY-P0C.md')) {
    Copy-Item -LiteralPath (Join-Path $repo ('docs\' + $guide)) -Destination (Join-Path $application $guide) -ErrorAction Stop
}
$oldPath = $env:PATH
$oldGdal = $env:GDAL_DATA
$oldProj = $env:PROJ_DATA
$oldNet = $env:PROJ_NETWORK
try {
    $env:PATH = $bin
    $env:GDAL_DATA = Join-Path $share 'gdal'
    $env:PROJ_DATA = Join-Path $share 'proj'
    $env:PROJ_NETWORK = 'OFF'
    $version = & (Join-Path $bin 'gdalinfo.exe') --version
    if ($LASTEXITCODE -ne 0 -or -not ($version -match '3.13.3')) { throw "Relocated GDAL version check failed: $version" }
    $formats = & (Join-Path $bin 'gdalinfo.exe') --formats
    if ($LASTEXITCODE -ne 0 -or -not ($formats -match 'GTiff') -or -not ($formats -match 'COG') -or -not ($formats -match 'VRT')) { throw 'Raster drivers missing from relocated runtime' }
    $vectors = & (Join-Path $bin 'ogrinfo.exe') --formats
    if ($LASTEXITCODE -ne 0 -or -not ($vectors -match 'ESRI Shapefile')) { throw 'Shapefile driver missing' }
    & (Join-Path $bin 'gdalsrsinfo.exe') EPSG:4326 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'PROJ CRS lookup failed' }
} finally {
    $env:PATH = $oldPath
    $env:GDAL_DATA = $oldGdal
    $env:PROJ_DATA = $oldProj
    $env:PROJ_NETWORK = $oldNet
}
$packages = @(Get-ChildItem (Join-Path $pixi 'conda-meta') -Filter '*.json' | ForEach-Object {
    try {
        $m = Get-Content -Raw -LiteralPath $_.FullName -Encoding UTF8 | ConvertFrom-Json
        if ($m.name -and $m.version) {
            [pscustomobject]@{Name=$m.name;Version=$m.version;License=$m.license;Channel=$m.channel}
        }
    } catch { throw "Failed to parse package metadata: $($_.Name)" }
})
$packages | Sort-Object Name | Export-Csv -LiteralPath (Join-Path $application 'THIRD-PARTY-METADATA.csv') -NoTypeInformation -Encoding UTF8
@'
GeoPublisher Workbench 0.1.0 - WINDOWS DEVELOPER PREVIEW
This package contains the pinned GDAL 3.13.3 / PROJ runtime and PySide6 desktop.
Third-party package metadata is included. Full license files, NOTICE review,
signing and clean Windows offline acceptance remain required before distribution.
No production GeoServer or business databases have been used to build or test.
To start: double-click GeoPublisher-Workbench.exe.
For an offline Windows desktop, double-click Install-Offline-Shortcut.cmd
once after copying the COMPLETE Current folder to the target local disk.
No network, admin access, Python, pip or PowerShell script policy changes needed.
GIS tools are preselected only when a complete bundle is present.
Do not move or delete runtime/ or _internal/ separately from the executable.
'@ | Set-Content -LiteralPath (Join-Path $application 'PREVIEW-READ-ME.txt') -Encoding UTF8
Push-Location $repo
try { $commit = (& git rev-parse HEAD).Trim() } finally { Pop-Location }
$manifest = [ordered]@{
    product = 'GeoPublisher Workbench'
    status = 'internal-developer-preview-not-production'
    commit = $commit
    built_at = (Get-Date).ToString('o')
    gdal_version = $version.Trim()
    proj_network = 'OFF'
    windows_exe = 'GeoPublisher-Workbench.exe'
    bundled_runtime = 'runtime/Library/bin'
    checksums = [ordered]@{
        exe_sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
        gdal_sha256 = (Get-FileHash -LiteralPath (Join-Path $bin 'gdalinfo.exe') -Algorithm SHA256).Hash
        projdb_sha256 = (Get-FileHash -LiteralPath (Join-Path $share 'proj\proj.db') -Algorithm SHA256).Hash
        offline_shortcut_sha256 = (Get-FileHash -LiteralPath (Join-Path $application 'Install-Offline-Shortcut.cmd') -Algorithm SHA256).Hash
        offline_guide_sha256 = (Get-FileHash -LiteralPath (Join-Path $application 'FIELD-SHEET-SPLIT-P0B.md') -Algorithm SHA256).Hash
        offline_boundary_guide_sha256 = (Get-FileHash -LiteralPath (Join-Path $application 'SHEET-BOUNDARY-P0C.md') -Algorithm SHA256).Hash
    }
    third_party_count = $packages.Count
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $application 'build-manifest.json') -Encoding UTF8
# Antivirus/indexers can briefly lock PyInstaller's directory after COLLECT.
# Prefer the fast atomic directory move; on an access-denied move, copy the
# same verified stage to a NEW destination without disabling security software.
try {
    Move-Item -LiteralPath $application -Destination $final -ErrorAction Stop
} catch {
    if ((Test-Path -LiteralPath $final) -or -not (Test-Path -LiteralPath $application)) {
        throw 'Portable move left a partial/unknown destination; manual review required.'
    }
    New-Item -ItemType Directory -Path $final -ErrorAction Stop | Out-Null
    & robocopy.exe $application $final /E /R:1 /W:1 /NP /NFL /NDL /NJH /NJS | Out-Null
    $copyExit = $LASTEXITCODE
    if ($copyExit -ge 8) { throw "Portable finalization copy failed: robocopy $copyExit" }
}
$finalManifest = Get-Content -LiteralPath (Join-Path $final 'build-manifest.json') -Raw | ConvertFrom-Json
if ($finalManifest.commit -ne $commit) { throw 'Final build manifest does not match source commit' }
foreach ($check in @(
    @('GeoPublisher-Workbench.exe', $finalManifest.checksums.exe_sha256),
    @('runtime\Library\bin\gdalinfo.exe', $finalManifest.checksums.gdal_sha256),
    @('runtime\Library\share\proj\proj.db', $finalManifest.checksums.projdb_sha256),
    @('Install-Offline-Shortcut.cmd', $finalManifest.checksums.offline_shortcut_sha256),
    @('FIELD-SHEET-SPLIT-P0B.md', $finalManifest.checksums.offline_guide_sha256),
    @('SHEET-BOUNDARY-P0C.md', $finalManifest.checksums.offline_boundary_guide_sha256)
)) {
    if ((Get-FileHash -LiteralPath (Join-Path $final $check[0]) -Algorithm SHA256).Hash -ne $check[1]) {
        throw "Portable finalization checksum mismatch: $($check[0])"
    }
}
"PORTABLE_BUILT=$final"
"EXE=$final\GeoPublisher-Workbench.exe"
"GDAL=$version"
"THIRD_PARTY_PACKAGES=$($packages.Count)"
"MANIFEST=$final\build-manifest.json"
