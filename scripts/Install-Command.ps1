[CmdletBinding()]
param(
    [string]$Repository = 'vermacodes/managed-cli-release-demo',

    [ValidateSet('stable', 'preview')]
    [string]$Channel = 'preview',

    [string]$Tag,

    [string]$InstallRoot,

    [string]$BinDirectory,

    [switch]$NoPathUpdate
)

$ErrorActionPreference = 'Stop'
$python = Get-Command py, python3, python -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $python) {
    throw 'Python 3 is required.'
}

$arguments = @(
    (Join-Path $PSScriptRoot 'install_command.py'),
    '--repo', $Repository,
    '--channel', $Channel
)
if ($Tag) {
    $arguments += @('--tag', $Tag)
}
if ($InstallRoot) {
    $arguments += @('--install-root', $InstallRoot)
}
if ($BinDirectory) {
    $arguments += @('--bin-dir', $BinDirectory)
}
if ($NoPathUpdate) {
    $arguments += '--no-path-update'
}
if ($python.Name -eq 'py.exe') {
    $arguments = @('-3', '-B') + $arguments
} else {
    $arguments = @('-B') + $arguments
}

& $python.Source @arguments
exit $LASTEXITCODE
