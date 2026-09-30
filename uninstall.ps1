[CmdletBinding()]
param(
    [switch]$PurgeSettings,
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\CodexStatusPet"
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = [IO.Path]::GetFullPath($InstallRoot)
$expectedRoot = [IO.Path]::GetFullPath("$env:LOCALAPPDATA\Programs\CodexStatusPet")
if ($resolvedRoot -ne $expectedRoot) { throw 'Unexpected installation root.' }

$productProcesses = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and (
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine.IndexOf($resolvedRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    )
})
$productProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$productProcesses | ForEach-Object { Wait-Process -Id $_.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }

$desktop = [Environment]::GetFolderPath('Desktop')
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
Remove-Item -LiteralPath (Join-Path $desktop 'Codex Windows Status Pet.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $startup 'Codex Windows Status Pet.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $programs 'Codex Windows Status Pet.lnk') -Force -ErrorAction SilentlyContinue
for ($attempt = 0; $attempt -lt 20 -and (Test-Path -LiteralPath $resolvedRoot); $attempt++) {
    Remove-Item -LiteralPath $resolvedRoot -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $resolvedRoot) { Start-Sleep -Milliseconds 100 }
}
if (Test-Path -LiteralPath $resolvedRoot) { throw 'Installed product directory could not be removed.' }

if ($PurgeSettings) {
    Remove-Item -LiteralPath (Join-Path $env:USERPROFILE '.codex\codex-windows-status-pet.json') -Force -ErrorAction SilentlyContinue
}
