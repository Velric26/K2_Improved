param(
    [string]$PrinterAddress = $(if ($env:K2_PRINTER_ADDRESS) { $env:K2_PRINTER_ADDRESS } else { '192.0.2.10' }),
    [string]$TaskName = 'K2 Auto PA Companion',
    [switch]$Uninstall
)

# Stores the K2 address for the current user and registers a logon task that
# starts the Auto PA companion (127.0.0.1:8766) hidden, so Orca's Auto
# Calibrations dialog works after every reboot without running the launcher.

$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    [Environment]::SetEnvironmentVariable('K2_PRINTER_ADDRESS', $null, 'User')
    Write-Output 'Removed the autostart task and K2_PRINTER_ADDRESS.'
    return
}

$PrinterAddress = $PrinterAddress.Trim().ToLowerInvariant()
if ($PrinterAddress -notmatch '^[a-z0-9][a-z0-9.-]*$' -or
    [System.Uri]::CheckHostName($PrinterAddress) -notin @('Dns', 'IPv4')) {
    throw 'Pass -PrinterAddress with the K2 IPv4 address or hostname.'
}

$launcher = Join-Path $PSScriptRoot 'Start-K2-AutoPA-Orca.ps1'
if (!(Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "Missing $launcher" }

[Environment]::SetEnvironmentVariable('K2_PRINTER_ADDRESS', $PrinterAddress, 'User')

$hidden = Join-Path $PSScriptRoot 'Run-Hidden.vbs'
if (!(Test-Path -LiteralPath $hidden -PathType Leaf)) { throw "Missing $hidden" }
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
# powershell.exe is a console program, so starting it directly flashes a window
# on every run. wscript.exe has no console and starts PowerShell hidden.
$arguments = '//B //Nologo "{0}" "{1}" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "{2}" -PrinterAddress {3} -CompanionOnly' -f $hidden, $powershell, $launcher, $PrinterAddress
$action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\wscript.exe') -Argument $arguments
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
# Re-run every 15 minutes. The launcher exits at once when the companion is healthy,
# so this only restarts a companion that crashed or was closed.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)).Repetition
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Starts the K2 Auto PA companion for OrcaSlicer at logon.' -Force | Out-Null

Write-Output "K2_PRINTER_ADDRESS=$PrinterAddress (user environment)."
Write-Output "Scheduled task '$TaskName' starts the companion at logon and restarts it within 15 minutes if it stops."
