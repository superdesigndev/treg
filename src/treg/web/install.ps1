# treg installer for Windows: irm {BASE}/install.ps1 | iex
#
# Downloads treg.exe for this machine from the release the registry names (from the release's
# mirror when that fails), checks it against the release's SHA256SUMS, puts it in
# %USERPROFILE%\.local\bin (the folder the Git Bash installer
# uses; TREG_INSTALL_DIR to change), adds that folder to the user PATH and points treg at this
# registry. TREG_TOKEN signs in non-interactively; TREG_CHANNEL picks stable or beta.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# .NET's own types for the download, hash and unzip: a Windows PowerShell started from PowerShell 7
# inherits a module path where Get-FileHash and Expand-Archive do not load.
Add-Type -AssemblyName System.Net.Http, System.IO.Compression.FileSystem

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
$Source = ''
$Detail = ''
$MirrorDetail = ''
$Seconds = $null
function Send-Step([string]$Step) {
  if (-not $InstallId) { return }
  $Record = @{ event = 'cli_install_step'; distinct_id = $InstallId; uuid = [guid]::NewGuid().ToString()
    properties = @{ install_step = $Step; reason = $Reason; method = 'binary'; prior_version = "$Prior"
      version = "$Version"; on_path = $OnPath; os = 'Windows'; arch = "$($env:PROCESSOR_ARCHITECTURE)"
      ad_link = $false; with_token = [bool]$env:TREG_TOKEN; channel = $Channel
      source = $Source; detail = $Detail; mirror_detail = $MirrorDetail; seconds = $Seconds } }
  try {
    Invoke-RestMethod -Method Post -Uri "$Base/cli/events" -ContentType 'application/json' -TimeoutSec 3 `
      -Body (ConvertTo-Json -Compress -Depth 4 @{ events = @($Record) }) | Out-Null
  } catch {}
}
function ConvertTo-Snake([string]$Name) { ($Name -creplace '(?<=[a-z0-9])([A-Z])', '_$1').ToLower() }
# Why a request failed, in a few words, from the innermost cause that names one: the socket's error
# (ConnectionRefused -> connection_refused), a TLS failure, Windows PowerShell's WebException status
# or PowerShell 7's HttpRequestError.
function Get-FailureKind([System.Exception]$E) {
  for ($X = $E; $X; $X = $X.InnerException) {
    if ($X -is [System.Net.Sockets.SocketException]) { return (ConvertTo-Snake "$($X.SocketErrorCode)") }
    if ($X -is [System.Security.Authentication.AuthenticationException]) { return 'tls' }
  }
  for ($X = $E; $X; $X = $X.InnerException) {
    if ($X -is [System.Net.WebException] -and "$($X.Status)" -ne 'UnknownError') { return (ConvertTo-Snake "$($X.Status)") }
    if ($X.PSObject.Properties['HttpRequestError'] -and "$($X.HttpRequestError)" -ne 'Unknown') {
      return (ConvertTo-Snake "$($X.HttpRequestError)")
    }
    if ($X -is [System.OperationCanceledException] -or $X -is [System.TimeoutException]) { return 'timeout' }
  }
  return (ConvertTo-Snake ($E.GetType().Name -replace 'Exception$', ''))
}
# Saves $Url to $Path: nothing when it is saved, else why not. A 30-second window that moves under
# 10 KB gives the transfer up, so a stalled one reaches the mirror; Invoke-WebRequest's timeout does
# not cover a body that keeps trickling.
function Save-Url([string]$Url, [string]$Path) {
  $Client = New-Object System.Net.Http.HttpClient
  $Client.Timeout = [System.Threading.Timeout]::InfiniteTimeSpan
  $Out = $null
  try {
    $Get = $Client.GetAsync($Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead)
    if (-not $Get.Wait(60000)) { return 'timeout' }
    $Response = $Get.Result
    if (-not $Response.IsSuccessStatusCode) { return "http_$([int]$Response.StatusCode)" }
    $In = $Response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    $Out = [System.IO.File]::Create($Path)
    $Buffer = New-Object byte[] 65536
    $Window = [System.Diagnostics.Stopwatch]::StartNew()
    $Moved = 0
    while ($true) {
      $Read = $In.ReadAsync($Buffer, 0, $Buffer.Length)
      if (-not $Read.Wait(30000)) { return 'stalled' }
      if ($Read.Result -eq 0) { return }
      $Out.Write($Buffer, 0, $Read.Result)
      $Moved += $Read.Result
      if ($Window.Elapsed.TotalSeconds -ge 30) {
        if ($Moved -lt 10240) { return 'stalled' }
        $Moved = 0
        $Window.Restart()
      }
    }
  } catch {
    return (Get-FailureKind $_.Exception)
  } finally {
    if ($Out) { $Out.Close() }
    $Client.Dispose()
  }
}
function Get-Sha256([string]$Path) {
  $Sha = [System.Security.Cryptography.SHA256]::Create()
  $Stream = [System.IO.File]::OpenRead($Path)
  try { return (($Sha.ComputeHash($Stream) | ForEach-Object { $_.ToString('x2') }) -join '') }
  finally { $Stream.Close(); $Sha.Dispose() }
}
# What a file that failed its checksum holds, by its first bytes: an archive, or a page some proxy
# or filter answered with.
function Get-FileKind([string]$Path) {
  try {
    $Stream = [System.IO.File]::OpenRead($Path)
    try { $Head = New-Object byte[] 4; $Read = $Stream.Read($Head, 0, 4) } finally { $Stream.Close() }
  } catch { return 'missing' }
  if ($Read -eq 0) { return 'empty' }
  if ($Head[0] -eq 0x50 -and $Head[1] -eq 0x4B) { return 'zip' }
  if ($Head[0] -eq 0x1F -and $Head[1] -eq 0x8B) { return 'gzip' }
  if ($Head[0] -eq 0x3C) { return 'html' }
  return 'other'
}
# Saves the archive at $Url to $Zip and checks it against $Want, the sum the registry named, or
# without one the release's SHA256SUMS at $SumsUrl: nothing when it matches, else the step that
# failed and why.
function Get-Release([string]$Url, [string]$SumsUrl, [string]$Zip, [string]$Archive, [string]$Want) {
  $Kind = Save-Url $Url $Zip
  if ($Kind) { return @('download', $Kind) }
  if (-not $Want) {
    $SumsFile = "$Zip.sums"
    $Kind = Save-Url $SumsUrl $SumsFile
    if ($Kind) { return @('download', $Kind) }
    $Want = ([System.IO.File]::ReadAllText($SumsFile) -split "`n" |
      Where-Object { $_ -match "\s\*?$([regex]::Escape($Archive))\s*$" } |
      ForEach-Object { ($_ -split '\s+')[0] } | Select-Object -First 1)
    if (-not $Want) { return @('checksum', 'sums_missing') }
  }
  if ($Want.ToLower() -ne (Get-Sha256 $Zip)) { return @('checksum', "got_$(Get-FileKind $Zip)") }
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
    $Watch = [System.Diagnostics.Stopwatch]::StartNew()
    $Failed = Get-Release $Manifest.url $Manifest.checksums_url $Zip $Manifest.archive "$($Manifest.sha256)"
    if (-not $Failed) {
      $Source = 'primary'
    } else {
      $Reason, $Detail = $Failed
      $Seconds = [int]$Watch.Elapsed.TotalSeconds
      # The mirror only with the registry's sum: nothing it serves is trusted on its own.
      if ($Manifest.mirror_url -and $Manifest.sha256) {
        Write-Host "That failed ($Detail); trying the mirror..."
        $MirrorFailed = Get-Release $Manifest.mirror_url '' $Zip $Manifest.archive $Manifest.sha256
        if ($MirrorFailed) { $MirrorDetail = $MirrorFailed[1] } else { $Source = 'mirror' }
      }
      if (-not $Source) {
        if ($Reason -eq 'checksum') { throw "the download does not match the release's SHA256SUMS" }
        throw "could not download $($Manifest.url)$(if ($Manifest.mirror_url) { ' or its mirror' })"
      }
    }

    $Reason = 'archive'
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, (Join-Path $Tmp 'x'))
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

  $Reason = 'run'
  & $Bin config --base-url $Base --channel $Channel | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "the installed $Bin does not run" }
  $Reason = ''
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
