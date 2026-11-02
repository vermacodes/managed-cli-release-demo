[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Repository,

    [Parameter(Mandatory = $true)]
    [string]$Tag,

    [Parameter(Mandatory = $true)]
    [string]$InstallRoot,

    [switch]$VerifyAttestation
)

$ErrorActionPreference = 'Stop'
$python = Get-Command py, python3, python -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $python) {
    throw 'Python 3 is required.'
}

$arguments = @(
    (Join-Path $PSScriptRoot 'release_client.py'),
    'download',
    '--repo', $Repository,
    '--tag', $Tag,
    '--install-root', $InstallRoot
)
if ($VerifyAttestation) {
    $arguments += '--verify-attestation'
}

if ($python.Name -eq 'py.exe') {
    $arguments = @('-3') + $arguments
}
& $python.Source @arguments
exit $LASTEXITCODE
