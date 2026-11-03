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
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    throw 'Node.js is required.'
}
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    throw 'GitHub CLI is required for offline signature verification.'
}

$installer = if ($PSScriptRoot) {
    Join-Path $PSScriptRoot 'install-command.js'
} else {
    ''
}
$runtime = if ($PSScriptRoot) {
    Join-Path $PSScriptRoot 'release-runtime.js'
} else {
    ''
}
$temporary = $null

try {
    if (
        -not $installer -or
        -not (Test-Path -LiteralPath $installer -PathType Leaf) -or
        -not (Test-Path -LiteralPath $runtime -PathType Leaf)
    ) {
        $version = 'v0.5.0'
        $temporary = Join-Path (
            [System.IO.Path]::GetTempPath()
        ) "managed-cli-installer-$([guid]::NewGuid().ToString('N'))"
        New-Item -ItemType Directory -Path $temporary | Out-Null
        $base = "https://raw.githubusercontent.com/vermacodes/managed-cli-release-demo/$version/scripts"
        foreach ($file in @('install-command.js', 'release-runtime.js')) {
            Invoke-WebRequest `
                -UseBasicParsing `
                -Uri "$base/$file" `
                -OutFile (Join-Path $temporary $file)
        }
        $installer = Join-Path $temporary 'install-command.js'
    }

    $effectiveBin = if ($BinDirectory) {
        [System.IO.Path]::GetFullPath($BinDirectory)
    } else {
        Join-Path $env:LOCALAPPDATA 'Programs\ManagedCliDemo\bin'
    }
    $arguments = @(
        $installer,
        '--repo', $Repository,
        '--channel', $Channel,
        '--no-path-update'
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

    & $node.Source @arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    if (-not $NoPathUpdate) {
        $current = [Environment]::GetEnvironmentVariable(
            'Path',
            [EnvironmentVariableTarget]::User
        )
        $parts = @(
            [string]$current -split ';' |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        )
        $present = @(
            $parts |
                Where-Object {
                    $candidate = [Environment]::ExpandEnvironmentVariables(
                        $_.Trim().Trim('"')
                    )
                    [string]::Equals(
                        $candidate,
                        $effectiveBin,
                        [System.StringComparison]::OrdinalIgnoreCase
                    )
                }
        ).Count -gt 0
        if (-not $present) {
            $updated = @($parts + $effectiveBin) -join ';'
            [Environment]::SetEnvironmentVariable(
                'Path',
                $updated,
                [EnvironmentVariableTarget]::User
            )
            Write-Host "Added $effectiveBin to the user PATH."
            Write-Host 'Open a new terminal, then run: managed-cli-demo'
        }
    }
} finally {
    if ($temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}
