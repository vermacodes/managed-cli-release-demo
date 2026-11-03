$ErrorActionPreference = 'Stop'

$node = Get-Command node -ErrorAction Stop
& $node.Source (Join-Path $PSScriptRoot 'managed-demo.js') @args
exit $LASTEXITCODE
