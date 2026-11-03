[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Repository,

    [Parameter(Mandatory = $true)]
    [ValidateSet('stable', 'preview')]
    [string]$Channel,

    [Parameter(Mandatory = $true)]
    [string]$InstallRoot
)

$ErrorActionPreference = 'Stop'
$python = Get-Command py, python3, python -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $python) {
    throw 'Python 3 is required.'
}

$arguments = @(
    (Join-Path $PSScriptRoot 'release_client.py'),
    'update',
    '--repo', $Repository,
    '--channel', $Channel,
    '--install-root', $InstallRoot
)
if ($python.Name -eq 'py.exe') {
    $arguments = @('-3') + $arguments
}
& $python.Source @arguments
exit $LASTEXITCODE
