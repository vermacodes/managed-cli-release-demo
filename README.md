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

Requirements: Git, Python 3.12+, Node.js, and (for release downloads) GitHub CLI.

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

Both wrappers call the same release client. GitHub CLI handles authentication;
the scripts never read or persist a GitHub token.

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

For private repositories, sign in first with `gh auth login`. Add
`--verify-attestation` to verify the downloaded ZIP's GitHub artifact
attestation in addition to the manifest and checksums.

The client installs under `versions/<release>`, health-checks the staged app,
and atomically updates `current.json`. The previous version remains available:

```bash
python3 scripts/release_client.py rollback \
  --install-root "$HOME/.managed-cli-demo"
```

## Workflows

- **Validate** runs tests, proves reproducibility, and uploads a short-lived
  Actions artifact. It never creates a GitHub Release.
- **Build or publish release** is manual. Its `publish` input defaults to
  `false`. A publishing run uses the `public-release` environment, attests the
  artifacts, refuses to overwrite an existing tag/release, and then creates
  either a prerelease or stable release.

For a real project, protect the `public-release` environment with required
reviewers and replace this demonstration's unsigned scripts with artifacts
signed by the organization's approved signing service.

## Trust boundaries

This demo proves packaging, transport integrity, provenance, safe extraction,
and activation behavior. It does **not** provide code-signing infrastructure,
malware scanning, legal review, production authorization, or endpoint
management.
