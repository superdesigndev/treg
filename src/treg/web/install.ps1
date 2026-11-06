# treg installer for Windows: irm {BASE}/install.ps1 | iex
#
# Downloads treg.exe for this machine from the release the registry names, checks it against the
# release's SHA256SUMS, puts it in %USERPROFILE%\.local\bin (the folder the Git Bash installer
# uses; TREG_INSTALL_DIR to change), adds that folder to the user PATH and points treg at this
# registry. TREG_TOKEN signs in non-interactively; TREG_CHANNEL picks stable or beta.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Base = '{BASE}'
$Channel = if ($env:TREG_CHANNEL) { $env:TREG_CHANNEL } else { '{CHANNEL}' }
if ($Channel -notin @('stable', 'beta')) { throw 'TREG_CHANNEL is stable or beta' }

$Arch = switch ($env:PROCESSOR_ARCHITECTURE) {
  'AMD64' { 'amd64' }
  'ARM64' { 'arm64' }
  default { throw "no treg build for $($env:PROCESSOR_ARCHITECTURE)" }
}

$Dir = if ($env:TREG_INSTALL_DIR) { $env:TREG_INSTALL_DIR } else { Join-Path $env:USERPROFILE '.local\bin' }
$Bin = Join-Path $Dir 'treg.exe'

$Manifest = Invoke-RestMethod -Uri "$Base/cli/latest?channel=$Channel&os=windows&arch=$Arch"
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("treg-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Tmp | Out-Null
try {
  Write-Host "Downloading treg $($Manifest.version) (windows/$Arch)..."
  $Zip = Join-Path $Tmp $Manifest.archive
  Invoke-WebRequest -UseBasicParsing -Uri $Manifest.url -OutFile $Zip
  $Sums = (Invoke-WebRequest -UseBasicParsing -Uri $Manifest.checksums_url).Content
  if ($Sums -is [byte[]]) { $Sums = [System.Text.Encoding]::UTF8.GetString($Sums) }
  $Want = ($Sums -split "`n" | Where-Object { $_ -match "\s\*?$([regex]::Escape($Manifest.archive))\s*$" } |
    ForEach-Object { ($_ -split '\s+')[0] } | Select-Object -First 1)
  $Got = (Get-FileHash -Algorithm SHA256 -Path $Zip).Hash.ToLower()
  if (-not $Want -or $Want.ToLower() -ne $Got) { throw "the download does not match the release's SHA256SUMS" }

  Expand-Archive -Path $Zip -DestinationPath (Join-Path $Tmp 'x') -Force
  $New = Join-Path $Tmp 'x\treg.exe'
  if (-not (Test-Path $New)) { throw 'the archive holds no treg.exe' }
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  # A running treg.exe cannot be overwritten but can be renamed; the next run removes the old file.
  if (Test-Path $Bin) { Move-Item -Force $Bin "$Bin.old" }
  Copy-Item -Force $New $Bin
} finally {
  Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}

$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (($UserPath -split ';') -contains $Dir)) {
  [Environment]::SetEnvironmentVariable('Path', (@($Dir) + ($UserPath -split ';' | Where-Object { $_ })) -join ';', 'User')
  Write-Host "Added $Dir to your user PATH (new terminals pick it up)."
}
$env:Path = "$Dir;$env:Path"

& $Bin config --base-url $Base --channel $Channel | Out-Null
& $Bin skill bootstrap
if ($env:TREG_TOKEN) {
  $Token = $env:TREG_TOKEN
  $env:TREG_TOKEN = ''
  & $Bin login --token $Token
  if ($LASTEXITCODE -eq 0) { & $Bin mcp install }
  $env:TREG_TOKEN = $Token
}
Write-Host ''
Write-Host "Installed treg $($Manifest.version) to $Bin"
if (-not $env:TREG_TOKEN) { Write-Host 'Next: treg login' }
