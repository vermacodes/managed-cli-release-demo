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

function Remove-TestCertificate {
    param([Parameter(Mandatory = $true)][string]$Thumbprint)

    $cleanupErrors = [System.Collections.Generic.List[string]]::new()
    foreach ($storeName in @('Root', 'My')) {
        $store = $null
        try {
            $store = [System.Security.Cryptography.X509Certificates.X509Store]::new(
                $storeName,
                [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
            )
            $store.Open(
                [System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite
            )
            $matches = $store.Certificates.Find(
                [System.Security.Cryptography.X509Certificates.X509FindType]::FindByThumbprint,
                $Thumbprint,
                $false
            )
            foreach ($match in $matches) {
                $store.Remove($match)
            }
        } catch {
            $cleanupErrors.Add("$storeName`: $($_.Exception.Message)")
        } finally {
            if ($store) {
                try {
                    $store.Close()
                } catch {
                    $cleanupErrors.Add(
                        "$storeName close: $($_.Exception.Message)"
                    )
                }
            }
        }
    }
    if ($cleanupErrors.Count -gt 0) {
        throw "Certificate cleanup failed: $($cleanupErrors -join '; ')"
    }
}

$temporaryRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) "managed-cli-authenticode-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

$subject = 'CN=Managed CLI Release Demo Test Publisher'
$certificate = $null
$rsa = $null
try {
    $rsa = [System.Security.Cryptography.RSA]::Create(2048)
    $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
        [System.Security.Cryptography.X509Certificates.X500DistinguishedName]::new(
            $subject
        ),
        $rsa,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
    )
    $codeSigningOids =
        [System.Security.Cryptography.OidCollection]::new()
    [void]$codeSigningOids.Add(
        [System.Security.Cryptography.Oid]::new(
            '1.3.6.1.5.5.7.3.3',
            'Code Signing'
        )
    )
    $request.CertificateExtensions.Add(
        [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new(
            $codeSigningOids,
            $false
        )
    )
    $request.CertificateExtensions.Add(
        [System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
            [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature,
            $true
        )
    )
    $certificate = $request.CreateSelfSigned(
        [DateTimeOffset]::UtcNow.AddMinutes(-5),
        [DateTimeOffset]::UtcNow.AddDays(1)
    )

    $rootStore = [System.Security.Cryptography.X509Certificates.X509Store]::new(
        'Root',
        [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
    )
    try {
        $rootStore.Open(
            [System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite
        )
        $rootStore.Add($certificate)
    } finally {
        $rootStore.Close()
    }
    $personalStore = [System.Security.Cryptography.X509Certificates.X509Store]::new(
        'My',
        [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
    )
    try {
        $personalStore.Open(
            [System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite
        )
        $personalStore.Add($certificate)
    } finally {
        $personalStore.Close()
    }

    $signedPath = Join-Path $temporaryRoot 'signed-demo.ps1'
    $unsignedPath = Join-Path $temporaryRoot 'unsigned-demo.ps1'
    Set-Content -LiteralPath $signedPath -Value "Write-Output 'signed demo'"
    Set-Content -LiteralPath $unsignedPath -Value "Write-Output 'unsigned demo'"

    $signingResult = Set-AuthenticodeSignature `
        -LiteralPath $signedPath `
        -Certificate $certificate
    if ($signingResult.Status -ne 'Valid') {
        throw "Test signing failed: $($signingResult.Status)"
    }

    & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
        -Path $signedPath `
        -ExpectedSubject $subject `
        -ExpectedThumbprint $certificate.Thumbprint | Out-Null
    Write-Host 'Accepted valid signature and expected publisher.'

    Assert-Rejected -Scenario 'wrong publisher' -Action {
        & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
            -Path $signedPath `
            -ExpectedSubject 'CN=Unexpected Publisher' `
            -ExpectedThumbprint $certificate.Thumbprint
    }
    Assert-Rejected -Scenario 'unsigned file' -Action {
        & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
            -Path $unsignedPath `
            -ExpectedSubject $subject `
            -ExpectedThumbprint $certificate.Thumbprint
    }

    Add-Content -LiteralPath $signedPath -Value '# post-signing tamper'
    Assert-Rejected -Scenario 'post-signing tamper' -Action {
        & (Join-Path $PSScriptRoot 'Assert-AuthenticodeSignature.ps1') `
            -Path $signedPath `
            -ExpectedSubject $subject `
            -ExpectedThumbprint $certificate.Thumbprint
    }

    Write-Host 'Authenticode validation tests passed.'
} finally {
    $cleanupErrors = [System.Collections.Generic.List[string]]::new()
    if ($certificate) {
        try {
            Remove-TestCertificate -Thumbprint $certificate.Thumbprint
        } catch {
            $cleanupErrors.Add($_.Exception.Message)
        }
        try {
            $certificate.Dispose()
        } catch {
            $cleanupErrors.Add("Certificate dispose: $($_.Exception.Message)")
        }
    }
    if ($rsa) {
        try {
            $rsa.Dispose()
        } catch {
            $cleanupErrors.Add("RSA dispose: $($_.Exception.Message)")
        }
    }
    try {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction Stop
    } catch {
        $cleanupErrors.Add("Temporary directory: $($_.Exception.Message)")
    }
    if ($cleanupErrors.Count -gt 0) {
        throw "Authenticode test cleanup failed: $($cleanupErrors -join '; ')"
    }
}
