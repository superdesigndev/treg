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

$Dir = if ($env:TREG_INSTALL_DIR) { $env:TREG_INSTALL_DIR } else { Join-Path $env:USERPROFILE '.local\bin' }
$Bin = Join-Path $Dir 'treg.exe'

# Each step is reported to the registry's /cli/events, which forwards it without the address, as the
# shell installer does: TREG_TELEMETRY=0|false|off or DO_NOT_TRACK=1 turn it off.
$Telemetry = ($env:TREG_TELEMETRY -notin @('0', 'false', 'off')) -and ($env:DO_NOT_TRACK -ne '1')
$ConfigDir = if ($env:TREG_CONFIG) { Split-Path -Parent $env:TREG_CONFIG } else { Join-Path $env:USERPROFILE '.treg' }
function Get-InstallId {
  $File = Join-Path $ConfigDir 'analytics-id'
  try {
    if (-not (Test-Path $File) -or -not (Get-Content -Raw $File)) {
      New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
      Set-Content -NoNewline -Path $File -Value ([guid]::NewGuid().ToString())
    }
    $Id = (Get-Content -Raw $File).Trim()
    if ($Id -match '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') { return $Id }
  } catch {}
  return ''
}
$InstallId = if ($Telemetry) { Get-InstallId } else { '' }
$Prior = ''
if (Test-Path $Bin) { try { $Prior = ((& $Bin --version) -split ' ')[1] } catch {} }
$Version = ''
$OnPath = $false
$Reason = ''
function Send-Step([string]$Step) {
  if (-not $InstallId) { return }
  $Record = @{ event = 'cli_install_step'; distinct_id = $InstallId; uuid = [guid]::NewGuid().ToString()
    properties = @{ install_step = $Step; reason = $Reason; method = 'binary'; prior_version = "$Prior"
      version = "$Version"; on_path = $OnPath; os = 'Windows'; arch = "$($env:PROCESSOR_ARCHITECTURE)"
      ad_link = $false; with_token = [bool]$env:TREG_TOKEN; channel = $Channel } }
  try {
    Invoke-RestMethod -Method Post -Uri "$Base/cli/events" -ContentType 'application/json' -TimeoutSec 3 `
      -Body (ConvertTo-Json -Compress -Depth 4 @{ events = @($Record) }) | Out-Null
  } catch {}
}
Send-Step 'started'
try {
  $Reason = 'unsupported_arch'
  $Arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'amd64' }
    'ARM64' { 'arm64' }
    default { throw "no treg build for $($env:PROCESSOR_ARCHITECTURE)" }
  }

  $Reason = 'no_release'
  $Manifest = Invoke-RestMethod -Uri "$Base/cli/latest?channel=$Channel&os=windows&arch=$Arch"
  $Version = "$($Manifest.version)"
  $Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("treg-" + [guid]::NewGuid())
  New-Item -ItemType Directory -Path $Tmp | Out-Null
  try {
    Write-Host "Downloading treg $($Manifest.version) (windows/$Arch)..."
    $Zip = Join-Path $Tmp $Manifest.archive
    $Reason = 'download'
    Invoke-WebRequest -UseBasicParsing -Uri $Manifest.url -OutFile $Zip
    $Sums = (Invoke-WebRequest -UseBasicParsing -Uri $Manifest.checksums_url).Content
    if ($Sums -is [byte[]]) { $Sums = [System.Text.Encoding]::UTF8.GetString($Sums) }
    $Want = ($Sums -split "`n" | Where-Object { $_ -match "\s\*?$([regex]::Escape($Manifest.archive))\s*$" } |
      ForEach-Object { ($_ -split '\s+')[0] } | Select-Object -First 1)
    $Reason = 'checksum'
    $Got = (Get-FileHash -Algorithm SHA256 -Path $Zip).Hash.ToLower()
    if (-not $Want -or $Want.ToLower() -ne $Got) { throw "the download does not match the release's SHA256SUMS" }

    $Reason = 'archive'
    Expand-Archive -Path $Zip -DestinationPath (Join-Path $Tmp 'x') -Force
    $New = Join-Path $Tmp 'x\treg.exe'
    if (-not (Test-Path $New)) { throw 'the archive holds no treg.exe' }
    $Reason = 'install_dir'
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    # A running treg.exe cannot be overwritten but can be renamed; the next run removes the old file.
    if (Test-Path $Bin) { Move-Item -Force $Bin "$Bin.old" }
    Copy-Item -Force $New $Bin
  } finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
  }

  $Reason = ''
  $UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  $OnPath = ($UserPath -split ';') -contains $Dir
  if (-not $OnPath) {
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
} catch {
  Send-Step 'failed'
  throw
}
Send-Step 'installed'
Write-Host ''
Write-Host "Installed treg $($Manifest.version) to $Bin"
if (-not $env:TREG_TOKEN) { Write-Host 'Next: treg login' }
