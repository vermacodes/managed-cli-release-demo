# Managed CLI Release Demo

This public, synthetic project demonstrates a secure release, installation, and
update pattern for a PowerShell/Bash CLI distributed through GitHub Releases. It
does not contain production product code.

## Install the command

Prerequisites:

- Node.js 18 or newer
- a current [GitHub CLI](https://cli.github.com/) with
  `gh attestation verify`
- PowerShell on Windows, or Bash and `curl` on Git Bash/WSL/Linux

Python, administrator access, and root access are not required.

### Windows PowerShell

```powershell
$installer = Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/vermacodes/managed-cli-release-demo/v0.5.0/scripts/Install-Command.ps1'
& ([scriptblock]::Create($installer))
```

### Git Bash, WSL, or Linux

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/vermacodes/managed-cli-release-demo/v0.5.0/scripts/install-command.sh | bash
```

The bootstrap URL is pinned to the immutable installer version rather than a
moving branch. Open a new terminal after the first installation, then run the
command from any directory:

```text
managed-cli-demo
```

Run the installer once in each environment where the command is needed. A
Windows installation is shared by PowerShell, Command Prompt, and Git Bash.
Each WSL distribution has its own Linux filesystem and PATH, so it needs its own
installation.

The default channel is `preview`. To select `stable`, append
`-Channel stable` to the PowerShell invocation or use
`bash -s -- --channel stable` after the Bash pipe.

macOS is intentionally outside this demo's tested support scope.

## How the installer works

The native PowerShell/Bash bootstrap downloads the two small Node.js installer
modules from the same version-pinned source tag. The shared installer then:

1. selects the newest complete, immutable release in the requested channel;
2. anonymously downloads the ZIP, manifest, checksums, and offline provenance
   bundle;
3. checks GitHub's asset digests and verifies the three content assets with
   `gh attestation verify --bundle`, bound to the exact repository, release
   workflow, source commit, and `main` branch;
4. validates the manifest, checksums, ZIP structure, paths, modes, sizes, and
   every packaged file;
5. extracts to a private staging directory, runs a health check, and activates
   the version atomically;
6. creates a stable `managed-cli-demo` launcher and adds its directory to the
   per-user PATH idempotently.

Install locations:

| Environment | Versioned runtime | Command directory |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Programs\ManagedCliDemo` | `%LOCALAPPDATA%\Programs\ManagedCliDemo\bin` |
| WSL/Linux | `${XDG_DATA_HOME:-~/.local/share}/managed-cli-demo` | `~/.local/bin` |

Installed versions remain side by side under `versions/<release>`, while
`current.json` is the atomic activation pointer. Re-running the installer is
safe and never automatically downgrades an existing installation. To verify
and reactivate the previous installed version:

```text
managed-cli-demo --rollback
```

To install a specific preview release from a source checkout:

```bash
./scripts/install-command.sh --channel preview --tag v0.5.0
```

PowerShell supports the equivalent `-Channel preview -Tag v0.5.0` parameters.
Custom locations are available through `--install-root` and `--bin-dir`, or
`-InstallRoot` and `-BinDirectory`.

## Secure startup updates

The installed command checks its selected channel at startup:

- `stable` accepts immutable published non-prereleases;
- `preview` accepts immutable published prereleases;
- drafts, mutable releases, malformed versions, and incomplete asset sets are
  ignored.

Versions use SemVer precedence rather than lexical ordering. If a newer version
exists, the command displays the exact version and release URL, then asks:

```text
Install this update now? [y/N]
```

Blank, `n`, or any answer other than `y`/`yes` keeps the current version.
Non-interactive launches never update. A failed download, verification, staging,
or health check leaves the active version untouched.

## Release pipeline

```text
exact Git commit
    |
    v
explicit file allowlist
    |
    v
deterministic ZIP + manifest + SHA256SUMS
    |
    v
Linux and Windows validation
    |
    v
protected publishing job + GitHub provenance
    |
    v
draft release upload
    |
    v
immutable publication
```

The bundle builder reads included files from the exact selected Git commit with
`git show`. Untracked files, local configuration, logs, dependency folders, and
other workstation state cannot enter the archive accidentally.

The manual release workflow defaults to build-only mode. Publishing requires
the `public-release` environment, refuses to replace an existing release or a
mismatched tag, creates a draft first, uploads the complete four-asset contract,
and publishes only after upload succeeds.

The repository's immutable-release policy locks future published release assets
and tags. It does not retroactively lock releases created before the policy was
enabled.

## Developer validation

Python is used only for repository build and test tooling; it is not packaged
or used by installation, launch, or update.

```bash
node --test tests/node_runtime.test.js
python3 -m unittest discover -s tests -v

python3 scripts/build_release.py \
  --version v0.5.0 \
  --repository vermacodes/managed-cli-release-demo \
  --output dist

python3 scripts/verify_release.py dist
```

Run the source app directly:

```bash
node app/managed-demo.js
```

## Signing boundary

GitHub's keyless OIDC/Sigstore build provenance is the release trust mechanism
implemented by this demo. Windows CI separately validates Authenticode policy
against a temporary copy of a trusted Microsoft-signed system binary, including
wrong-publisher, wrong-thumbprint, unsigned, and tampered-file failures.

That fixture does not sign release assets or modify certificate stores. ESRP
and production publisher signing are intentionally deferred; a production
implementation must use its organization's approved signing service.

## Trust boundaries

This demo proves deterministic packaging, transport integrity, provenance,
archive safety, staged installation, atomic activation, and prompted updates.
It does not provide production authorization, malware scanning, legal review,
endpoint management, or a production code-signing identity.
