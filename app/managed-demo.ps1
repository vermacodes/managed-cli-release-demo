$ErrorActionPreference = 'Stop'

$releaseRoot = Split-Path -Parent $PSScriptRoot
$releaseInfo = Join-Path $releaseRoot 'release-info.json'
$releaseClient = Join-Path $releaseRoot 'scripts\release_client.py'

if (
    (Test-Path -LiteralPath $releaseInfo -PathType Leaf) -and
    (Test-Path -LiteralPath $releaseClient -PathType Leaf)
) {
    $python = Get-Command py, python3, python -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $python) {
        throw 'Python 3 is required for installed release updates.'
    }

    $versionsRoot = Split-Path -Parent $releaseRoot
    $installRoot = Split-Path -Parent $versionsRoot
    $channel = if ($env:MANAGED_CLI_UPDATE_CHANNEL) {
        $env:MANAGED_CLI_UPDATE_CHANNEL
    } else {
        'preview'
    }
    $clientArguments = @(
        $releaseClient,
        'startup',
        '--release-root', $releaseRoot,
        '--install-root', $installRoot,
        '--channel', $channel,
        '--'
    ) + $args
    if ($python.Name -eq 'py.exe') {
        $clientArguments = @('-3') + $clientArguments
    }
    & $python.Source @clientArguments
    exit $LASTEXITCODE
}

$node = Get-Command node -ErrorAction Stop
& $node.Source (Join-Path $PSScriptRoot 'managed-demo.js') @args
exit $LASTEXITCODE
