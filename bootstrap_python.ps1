<#
Downloads a PORTABLE Python into the program folder - used by find_python.bat
when the system has no Python 3.10+ (with tkinter for the window).

Zrodlo: python-build-standalone (astral-sh) - te same buildy, ktorych uzywa uv.
Wariant "install_only" to zwykly katalog z python.exe, bez instalatora,
bez wpisow w rejestrze i bez ruszania PATH. Kasujemy folder = koniec.

Dlaczego GitHub API, a nie sklejanie nazwy pliku: w nazwie assetu siedzi PELNA
wersja Pythona (cpython-3.13.11+20260807-...), ktorej latest-release.json nie
podaje - jest tam tylko tag. Sklejanie na sztywno rozsypuje sie przy kazdym
kolejnym wydaniu.

Uzycie:
  powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap_python.ps1 -Dest .python
#>
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    # 3.14+ pomijamy domyslnie: nie kazda biblioteka ma dla niego wheele,
    # a instalator ma dzialac od razu, a nie kompilowac cokolwiek ze zrodel.
    [int[]]$PreferMinor = @(13, 12, 11, 10)
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # bez tego Invoke-WebRequest jest wolny

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {}

$repo = 'https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest'
$headers = @{ 'User-Agent' = 'rpgm-to-apk'; 'Accept' = 'application/vnd.github+json' }

Write-Host '  [1/3] looking for the latest release...'
$release = Invoke-RestMethod -Uri $repo -Headers $headers

# cpython-<wersja>+<tag>-x86_64-pc-windows-msvc-install_only.tar.gz
# (jest tez ...install_only_stripped.tar.gz - anchor na koncu go odrzuca)
$rx = '^cpython-3\.(?<minor>\d+)\.(?<patch>\d+)\+\d+-x86_64-pc-windows-msvc-install_only\.tar\.gz$'
$candidates = @()
foreach ($asset in $release.assets) {
    $m = [regex]::Match($asset.name, $rx)
    if (-not $m.Success) { continue }
    $candidates += [pscustomobject]@{
        Name  = $asset.name
        Url   = $asset.browser_download_url
        Minor = [int]$m.Groups['minor'].Value
        Patch = [int]$m.Groups['patch'].Value
    }
}
if ($candidates.Count -eq 0) {
    throw "w wydaniu $($release.tag_name) nie ma buildu dla Windows x86_64"
}

$pick = $null
foreach ($minor in $PreferMinor) {
    $pick = $candidates | Where-Object { $_.Minor -eq $minor } |
            Sort-Object Patch -Descending | Select-Object -First 1
    if ($pick) { break }
}
if (-not $pick) {
    $pick = $candidates | Sort-Object Minor, Patch -Descending | Select-Object -First 1
}

# -Dest bywa i bezwzgledny (tak wola zainstaluj.bat), i wzgledny; Join-Path na
# gotowej sciezce z litera dysku daje potworka typu "C:\a\C:\b" i GetFullPath
# rzuca "Format podanej sciezki nie jest obslugiwany"
if (-not [System.IO.Path]::IsPathRooted($Dest)) {
    $Dest = Join-Path $PWD.ProviderPath $Dest
}
$Dest = [System.IO.Path]::GetFullPath($Dest)
$tmp = Join-Path $env:TEMP $pick.Name

Write-Host "  [2/3] downloading $($pick.Name)..."
Invoke-WebRequest -Uri $pick.Url -OutFile $tmp -Headers @{ 'User-Agent' = 'rpgm-to-apk' }

Write-Host "  [3/3] unpacking to $Dest ..."
if (Test-Path -LiteralPath $Dest) { Remove-Item -LiteralPath $Dest -Recurse -Force }
New-Item -ItemType Directory -Path $Dest | Out-Null
# tar (bsdtar) jest w Windows od 10 1803; --strip-components zdejmuje "python/"
& tar.exe -xzf "$tmp" -C "$Dest" --strip-components=1
if ($LASTEXITCODE -ne 0) { throw "tar zwrocil kod $LASTEXITCODE" }
Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue

$exe = Join-Path $Dest 'python.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "brak $exe po rozpakowaniu" }
$ver = & $exe -c 'import sys; print(sys.version.split()[0])'
Write-Host "  done: Python $ver"
exit 0
