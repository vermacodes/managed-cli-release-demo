#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-Rejected {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action,

        [Parameter(Mandatory = $true)]
        [string]$Scenario
    )

    $rejected = $false
    try {
        & $Action | Out-Null
    } catch {
        $rejected = $true
        Write-Host "Rejected $Scenario`: $($_.Exception.Message)"
    }
    if (-not $rejected) {
        throw "Expected rejection did not occur: $Scenario"
    }
}

$temporaryRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) "managed-cli-authenticode-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

try {
    $systemBinary = Join-Path $env:SystemRoot (
        'System32\WindowsPowerShell\v1.0\powershell.exe'
    )
    if (-not (Test-Path -LiteralPath $systemBinary -PathType Leaf)) {
        throw "Trusted Windows signing fixture is missing: $systemBinary"
    }

    $validPath = Join-Path $temporaryRoot 'valid-signed.exe'
    $tamperedPath = Join-Path $temporaryRoot 'tampered-signed.exe'
    $unsignedPath = Join-Path $temporaryRoot 'unsigned-demo.ps1'
    Copy-Item -LiteralPath $systemBinary -Destination $validPath
    Copy-Item -LiteralPath $systemBinary -Destination $tamperedPath
    Set-Content -LiteralPath $unsignedPath -Value "Write-Output 'unsigned demo'"

    $knownGood = Get-AuthenticodeSignature -LiteralPath $validPath
    if (
        $knownGood.Status -ne
        [System.Management.Automation.SignatureStatus]::Valid -or
        -not $knownGood.SignerCertificate
    ) {
        throw "Windows signing fixture is not trusted: $($knownGood.Status)"
    }
    $subject = $knownGood.SignerCertificate.Subject
    $thumbprint = $knownGood.SignerCertificate.Thumbprint

    & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
        -Path $validPath `
        -ExpectedSubject $subject `
        -ExpectedThumbprint $thumbprint | Out-Null
    Write-Host 'Accepted valid signature, publisher, and thumbprint.'

    Assert-Rejected -Scenario 'wrong publisher' -Action {
        & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
            -Path $validPath `
            -ExpectedSubject 'CN=Unexpected Publisher' `
            -ExpectedThumbprint $thumbprint
    }
    Assert-Rejected -Scenario 'wrong thumbprint' -Action {
        & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
            -Path $validPath `
            -ExpectedSubject $subject `
            -ExpectedThumbprint ('0' * $thumbprint.Length)
    }
    Assert-Rejected -Scenario 'unsigned file' -Action {
        & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
            -Path $unsignedPath `
            -ExpectedSubject $subject `
            -ExpectedThumbprint $thumbprint
    }

    $tamperedBytes = [System.IO.File]::ReadAllBytes($tamperedPath)
    $tamperOffset = [Math]::Min(4096, $tamperedBytes.Length - 1)
    $tamperedBytes[$tamperOffset] = $tamperedBytes[$tamperOffset] -bxor 1
    [System.IO.File]::WriteAllBytes($tamperedPath, $tamperedBytes)
    Assert-Rejected -Scenario 'post-signing tamper' -Action {
        & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
            -Path $tamperedPath `
            -ExpectedSubject $subject `
            -ExpectedThumbprint $thumbprint
    }

    Write-Host 'Authenticode validation tests passed.'
} finally {
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction Stop
}
