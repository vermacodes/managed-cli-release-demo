[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedSubject,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedThumbprint
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
$item = Get-Item -LiteralPath $resolved -Force
if ($item.PSIsContainer) {
    throw "Authenticode input must be a file: $resolved"
}

$signature = Get-AuthenticodeSignature -LiteralPath $resolved
if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Authenticode signature is not valid: $($signature.Status)"
}
if (-not $signature.SignerCertificate) {
    throw 'Authenticode signature has no signer certificate.'
}
if (-not [string]::Equals(
        $signature.SignerCertificate.Subject,
        $ExpectedSubject,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Authenticode publisher mismatch: $($signature.SignerCertificate.Subject)"
}
if ($ExpectedThumbprint) {
    $actualThumbprint = $signature.SignerCertificate.Thumbprint.Replace(' ', '')
    $normalizedExpected = $ExpectedThumbprint.Replace(' ', '')
    if (-not [string]::Equals(
            $actualThumbprint,
            $normalizedExpected,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Authenticode thumbprint mismatch: $actualThumbprint"
    }
}

[pscustomobject]@{
    Path = $resolved
    Publisher = $signature.SignerCertificate.Subject
    Status = $signature.Status.ToString()
    Thumbprint = $signature.SignerCertificate.Thumbprint
}
