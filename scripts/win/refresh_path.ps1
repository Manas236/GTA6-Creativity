# Prints a fresh PATH for the calling launcher window (scripts\win\refresh_path.bat sets it).
# A console opened before winget/scoop installed something still has the old PATH, so this
# rebuilds it from the registry (Machine, then User), keeps any extra entries the window had,
# and, if ffmpeg still isn't on it, adds the folder of an ffmpeg found in a usual install place.
$ErrorActionPreference = 'SilentlyContinue'
$parts = New-Object System.Collections.Generic.List[string]
$seen = @{}
function Add-PathPart([string]$p, [switch]$Front) {
    if ([string]::IsNullOrWhiteSpace($p)) { return }
    $p = $p.Trim().Trim('"')
    $key = $p.TrimEnd('\').ToLowerInvariant()
    if ($seen.ContainsKey($key)) { return }
    $seen[$key] = $true
    if ($Front) { $parts.Insert(0, $p) } else { $parts.Add($p) }
}
foreach ($scope in 'Machine', 'User') {
    foreach ($p in ([Environment]::GetEnvironmentVariable('Path', $scope) -split ';')) { Add-PathPart $p }
}
foreach ($p in ($env:Path -split ';')) { Add-PathPart $p }

function Test-OnPath([string]$exe) {
    foreach ($d in $parts) { if (Test-Path -LiteralPath (Join-Path $d $exe)) { return $true } }
    return $false
}
if (-not (Test-OnPath 'ffmpeg.exe')) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:USERPROFILE 'scoop\shims'),
        (Join-Path $env:ProgramData 'chocolatey\bin'),
        'C:\ffmpeg\bin',
        (Join-Path $env:ProgramFiles 'ffmpeg\bin')
    )
    $wingetPkgs = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path -LiteralPath $wingetPkgs) {
        $candidates += Get-ChildItem -LiteralPath $wingetPkgs -Directory -Filter 'Gyan.FFmpeg*' |
            ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Recurse -Depth 3 -Filter 'ffmpeg.exe' } |
            ForEach-Object { $_.DirectoryName }
    }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath (Join-Path $c 'ffmpeg.exe'))) { Add-PathPart $c -Front; break }
    }
}
[Console]::Out.Write(($parts -join ';'))
