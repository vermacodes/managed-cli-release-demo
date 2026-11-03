# Managed CLI Release Demo

This public, synthetic project demonstrates a safe release pattern for a
PowerShell/Bash CLI distributed through GitHub Releases. It is intentionally
independent of any production product.

## What it demonstrates

```text
exact Git commit
    |
    v
explicit file allowlist
    |
    v
deterministic ZIP + release manifest + SHA256SUMS
    |
    v
archive safety and reproducibility tests
    |
    v
GitHub Actions artifact
    |
    v
optional GitHub Release + build provenance attestation
    |
    v
authenticated gh download
    |
    v
verification -> staging -> health check -> atomic activation
```

The bundle builder reads every included file from the selected Git commit with
`git show`. Untracked files, local configuration, logs, dependency folders, and
other workstation state cannot enter the archive accidentally.

The verifier rejects:

- missing or unexpected release assets;
- checksum, size, or per-file digest mismatches;
- absolute paths, `..`, backslashes, NULs, and drive-qualified paths;
- duplicate or case-colliding ZIP entries;
- symbolic links, encrypted entries, unsupported compression, and oversized
  payloads;
- manifest/release/source identity inconsistencies.

## Local validation

Requirements: Git, Python 3.12+, Node.js, and a current GitHub CLI with
artifact-attestation support.

```bash
python3 -m unittest discover -s tests -v

python3 scripts/build_release.py \
  --version v0.1.0 \
  --repository vermacodes/managed-cli-release-demo \
  --output dist

python3 scripts/verify_release.py dist
```

Run the app directly:

```bash
./app/managed-demo.sh
```

The source checkout has no generated release identity, so the direct command
reports a development build. The packaged app reads the generated
`release-info.json`.

## Download and install a published release

Both wrappers call the same release client. Public release discovery and
downloads use GitHub's anonymous HTTPS APIs. GitHub CLI is used only to verify
downloaded Sigstore bundles offline; no GitHub sign-in or token is required.

```bash
./scripts/get-release.sh \
  --repo vermacodes/managed-cli-release-demo \
  --tag v0.1.0 \
  --install-root "$HOME/.managed-cli-demo"
```

```powershell
.\scripts\Get-Release.ps1 `
  -Repository vermacodes/managed-cli-release-demo `
  -Tag v0.1.0 `
  -InstallRoot "$HOME\.managed-cli-demo"
```

The client copies downloaded assets into a private staging directory before
verification, installs under `versions/<release>`, verifies every installed
file, health-checks the staged app, and atomically updates `current.json`. The
previous version remains available:

```bash
python3 scripts/release_client.py rollback \
  --install-root "$HOME/.managed-cli-demo"
```

## Secure auto-update

The updater discovers only complete releases in the selected channel:

- `stable` accepts published non-prereleases;
- `preview` accepts published prereleases;
- mutable releases, drafts, malformed versions, and incomplete asset sets are ignored.

Versions use SemVer precedence rather than lexical ordering, and the updater
never downgrades an installed version automatically. Automatic channel updates
therefore require GitHub's repository-level immutable release policy.

Installed PowerShell and Bash launchers check for updates before starting the
app. The demo defaults to the `preview` channel; set
`MANAGED_CLI_UPDATE_CHANNEL=stable` to follow stable releases. When a newer
version exists, the launcher shows the exact version and release URL, then asks
`Install this update now? [y/N]`. Blank, `n`, or any response other than
`y`/`yes` keeps the current version. Non-interactive launches never update.

Manual update commands use the same confirmation. Passing `--yes` in Bash or
`-Yes` in PowerShell is explicit non-interactive approval.

```bash
python3 scripts/release_client.py check-update \
  --repo vermacodes/managed-cli-release-demo \
  --channel preview \
  --install-root "$HOME/.managed-cli-demo"

./scripts/update-release.sh \
  --repo vermacodes/managed-cli-release-demo \
  --channel preview \
  --install-root "$HOME/.managed-cli-demo"
# Add --yes only when intentionally approving an unattended update.
```

```powershell
.\scripts\Update-Release.ps1 `
  -Repository vermacodes/managed-cli-release-demo `
  -Channel preview `
  -InstallRoot "$HOME\.managed-cli-demo"
# Add -Yes only when intentionally approving an unattended update.
```

Automatic updates always verify all three assets' provenance attestations,
require the exact release workflow identity, `main` source ref, and source commit resolved from the
release tag, verify checksums and every archived file, stage privately,
health-check, and atomically activate. A failed update leaves the current
version untouched.

Verify a release without installing it:

```bash
python3 scripts/release_client.py verify-remote \
  --repo vermacodes/managed-cli-release-demo \
  --tag v0.1.0
```

## Signing validation

Published release assets are cryptographically signed with GitHub's keyless
OIDC/Sigstore provenance attestations. This is the actual trust mechanism used
by the demo release client.

The Windows CI job separately exercises Authenticode policy against a temporary
copy of a trusted Microsoft-signed Windows system binary. It proves that the validator:

- accepts a valid signature from the exact expected publisher and thumbprint;
- rejects a wrong publisher;
- rejects a wrong thumbprint;
- rejects an unsigned file;
- rejects bytes modified after signing.

The fixture neither signs release assets nor modifies certificate stores. A
production implementation must use its organization's approved code-signing
service and publisher certificate.

## Workflows

- **Validate** runs tests, proves reproducibility, and uploads a short-lived
  Actions artifact. A Windows job also runs the Authenticode policy tests. It
  never creates a GitHub Release.
- **Build or publish release** is manual. Its `publish` input defaults to
  `false`. A publishing run uses the `public-release` environment, attests the
  artifacts from the protected job, refuses to overwrite an existing release or
  mismatched tag, creates a draft, uploads every asset, and publishes only after
  the complete set is present. A retry may reuse only a tag that already points
  to the exact workflow commit.

The draft-first flow is compatible with GitHub's repository-level immutable
release policy. Enabling that policy locks future published release assets and
tags; it does not retroactively lock older releases.

For a real project, protect the `public-release` environment with required
reviewers and replace this demonstration's unsigned scripts with artifacts
signed by the organization's approved signing service.

## Trust boundaries

This demo proves packaging, transport integrity, provenance, signing-policy
validation, safe extraction, and activation behavior. It does **not** provide a
production signing identity, malware scanning, legal review, production
authorization, or endpoint management.
