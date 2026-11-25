param(
    [string]$PrinterAddress = $env:K2_PRINTER_ADDRESS,
    [string]$DataDirectory,
    [string]$OrcaExecutable,
    [switch]$CompanionOnly
)

$ErrorActionPreference = 'Stop'
$PrinterAddress = ([string]$PrinterAddress).Trim().ToLowerInvariant()
if ($PrinterAddress -notmatch '^[a-z0-9][a-z0-9.-]*$' -or
    [System.Uri]::CheckHostName($PrinterAddress) -notin @('Dns', 'IPv4')) {
    throw 'Set -PrinterAddress or K2_PRINTER_ADDRESS to the K2 IPv4 address or hostname.'
}
$moonraker = "http://${PrinterAddress}:7125"
$workspace = Split-Path -Parent $PSScriptRoot
$orca = if ($OrcaExecutable) {
    (Get-Item -LiteralPath $OrcaExecutable).FullName
} else {
    Join-Path $env:LOCALAPPDATA 'K2Calibration/OrcaSlicer-2.5.1/orca-slicer.exe'
}
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$helper = Join-Path $workspace 'K2-Stock-KAMP-F021/stock-pa-adapter/orca_auto_pa.py'
$healthUrl = 'http://127.0.0.1:8766/health'

foreach ($file in @($orca, $python, $helper)) {
    if (!(Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Required file is missing. $file"
    }
}

$orcaData = if ($DataDirectory) {
    [System.IO.Path]::GetFullPath($DataDirectory)
} else {
    Join-Path $env:APPDATA 'OrcaSlicer'
}
$pluginSource = Join-Path $PSScriptRoot 'k2_calibration_plugin.py'
$pluginDirectory = Join-Path $orcaData 'orca_plugins/k2_calibration_plugin'
$pluginTarget = Join-Path $pluginDirectory 'k2_calibration_plugin.py'
if (!(Test-Path -LiteralPath $pluginTarget -PathType Leaf) -or
    [System.IO.File]::ReadAllText($pluginSource) -cne [System.IO.File]::ReadAllText($pluginTarget)) {
    New-Item -ItemType Directory -Path $pluginDirectory -Force | Out-Null
    Copy-Item -LiteralPath $pluginSource -Destination $pluginTarget -Force
}

function Get-Calibrator {
    try {
        Invoke-RestMethod $healthUrl -TimeoutSec 1
    } catch {
        $null
    }
}

$health = Get-Calibrator
if (!$health) {
    $logRoot = Join-Path $env:LOCALAPPDATA 'K2AutoPA'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $reportDirectory = Join-Path $logRoot 'reports'
    $arguments = @('-u', ('"' + $helper + '"'), '--host', $moonraker, '--output', ('"' + $reportDirectory + '"'))
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logRoot 'helper.log') `
        -RedirectStandardError (Join-Path $logRoot 'helper-errors.log') -PassThru
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        if ($process.HasExited) { break }
        $health = Get-Calibrator
        if ($health) { break }
        Start-Sleep -Milliseconds 250
    }
}
if ($health.service -ne 'k2-auto-pa') {
    throw 'The K2 Auto PA companion could not start on port 8766.'
}
if ($health.printer -ne $PrinterAddress) {
    throw 'The running companion points to a different printer. Close it before launching for this address.'
}
if (!$health.combined_calibration -or !$health.standalone_pa_outer_wall_only -or
    !$health.printed_adaptive_pa -or !$health.adaptive_pa_profile_preheat -or !$health.supported_nozzles_mm) {
    throw 'The running K2 companion predates the plugin. Restart it and run this launcher again.'
}

if ($CompanionOnly) {
    Write-Output 'The K2 calibration companion is ready on 127.0.0.1:8766.'
    return
}

$orcaDirectory = Split-Path -Parent $orca
if ($DataDirectory) {
    $directory = [System.IO.Path]::GetFullPath($DataDirectory)
    if ($directory.Contains('"')) { throw 'The Orca data directory contains a quote.' }
    Start-Process -FilePath $orca -WorkingDirectory $orcaDirectory -ArgumentList @('--datadir', ('"' + $directory + '"'))
} else {
    Start-Process -FilePath $orca -WorkingDirectory $orcaDirectory
}
