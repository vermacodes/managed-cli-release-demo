#!/usr/bin/env node
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const runtime = require('./release-runtime.js');

const COMMAND = 'managed-cli-demo';
const DEFAULT_REPOSITORY = 'vermacodes/managed-cli-release-demo';
const PATH_START = '# >>> managed-cli-demo PATH >>>';
const PATH_END = '# <<< managed-cli-demo PATH <<<';

function fail(message) {
  throw new runtime.ReleaseError(message);
}

function parseArguments(values) {
  const result = {
    repository: DEFAULT_REPOSITORY,
    channel: 'preview',
    tag: null,
    installRoot: null,
    binDir: null,
    updatePath: true
  };
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    if (value === '--no-path-update') {
      result.updatePath = false;
      continue;
    }
    const target = {
      '--repo': 'repository',
      '--channel': 'channel',
      '--tag': 'tag',
      '--install-root': 'installRoot',
      '--bin-dir': 'binDir'
    }[value];
    if (!target || index + 1 >= values.length) fail(`Unknown argument: ${value}`);
    result[target] = values[index + 1];
    index += 1;
  }
  if (!['stable', 'preview'].includes(result.channel)) fail('Invalid channel.');
  runtime.validateRepository(result.repository);
  if (result.tag) runtime.parseVersion(result.tag);
  return result;
}

function defaultLayout() {
  if (process.platform === 'darwin') {
    fail('macOS is not supported by this demo installer.');
  }
  if (process.platform === 'win32') {
    const base =
      process.env.LOCALAPPDATA ||
      path.join(os.homedir(), 'AppData', 'Local');
    const root = path.join(base, 'Programs', 'ManagedCliDemo');
    return { platform: 'windows', installRoot: root, binDir: path.join(root, 'bin') };
  }
  const base =
    process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
  return {
    platform: 'linux',
    installRoot: path.join(base, 'managed-cli-demo'),
    binDir: path.join(os.homedir(), '.local', 'bin')
  };
}

function atomicWrite(filePath, value, mode = 0o644) {
  const temporary = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  try {
    fs.writeFileSync(temporary, value, { flag: 'wx', mode });
    fs.renameSync(temporary, filePath);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

function posixShim(installRoot) {
  const quoted = `'${installRoot.replaceAll("'", "'\\''")}'`;
  return `#!/usr/bin/env bash
set -euo pipefail

install_root=${quoted}
version="$(node -e '
const fs = require("fs");
const value = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
if (!/^v[0-9]+\\.[0-9]+\\.[0-9]+(?:-[0-9A-Za-z.-]+)?$/.test(value.current || "")) {
  throw new Error("The managed-cli-demo installation pointer is invalid.");
}
process.stdout.write(value.current);
' "$install_root/current.json")"
target="$install_root/versions/$version/app/managed-demo.sh"
if [[ ! -x "$target" ]]; then
  printf 'The managed-cli-demo installation is incomplete: %s\\n' "$target" >&2
  exit 1
fi
exec "$target" "$@"
`;
}

function powershellShim(installRoot) {
  const escaped = installRoot.replaceAll("'", "''");
  return `$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$installRoot = '${escaped}'
$pointer = Get-Content -LiteralPath (Join-Path $installRoot 'current.json') -Raw |
    ConvertFrom-Json
$version = [string]$pointer.current
if ($version -notmatch '^v[0-9]+\\.[0-9]+\\.[0-9]+(?:-[0-9A-Za-z.-]+)?$') {
    throw 'The managed-cli-demo installation pointer is invalid.'
}
$versionsRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $installRoot 'versions')
).TrimEnd('\\', '/') + [System.IO.Path]::DirectorySeparatorChar
$target = [System.IO.Path]::GetFullPath(
    (Join-Path $versionsRoot "$version\\app\\managed-demo.ps1")
)
if (-not $target.StartsWith(
        $versionsRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
    throw 'The managed-cli-demo pointer escaped its version root.'
}
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "The managed-cli-demo installation is incomplete: $target"
}
& $target @args
exit $LASTEXITCODE
`;
}

function cmdShim() {
  return `@echo off\r
powershell.exe -NoLogo -NoProfile -File "%~dp0managed-cli-demo.ps1" %*\r
exit /b %errorlevel%\r
`;
}

function gitBashShim() {
  return `#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "\${BASH_SOURCE[0]}")" && pwd)"
ps1="$script_dir/managed-cli-demo.ps1"
if command -v cygpath >/dev/null 2>&1; then
  ps1="$(cygpath -w "$ps1")"
fi
exec powershell.exe -NoLogo -NoProfile -File "$ps1" "$@"
`;
}

function createShims(layout) {
  fs.mkdirSync(layout.binDir, { recursive: true });
  if (layout.platform === 'windows') {
    const values = [
      [path.join(layout.binDir, `${COMMAND}.ps1`), powershellShim(layout.installRoot), 0o644],
      [path.join(layout.binDir, `${COMMAND}.cmd`), cmdShim(), 0o644],
      [path.join(layout.binDir, COMMAND), gitBashShim(), 0o755]
    ];
    for (const [filePath, value, mode] of values) atomicWrite(filePath, value, mode);
    return values.map(([filePath]) => filePath);
  }
  const shim = path.join(layout.binDir, COMMAND);
  atomicWrite(shim, posixShim(layout.installRoot), 0o755);
  return [shim];
}

function updatePosixPath(
  binDir,
  homeDirectory = os.homedir(),
  shell = process.env.SHELL || ''
) {
  const current = (process.env.PATH || '').split(':').filter(Boolean);
  if (current.includes(binDir)) return { changed: false, location: null };
  const profile = path.join(
    homeDirectory,
    shell.endsWith('/zsh') ? '.zprofile' : '.profile'
  );
  const existing = fs.existsSync(profile) ? fs.readFileSync(profile, 'utf8') : '';
  const hasStart = existing.includes(PATH_START);
  const hasEnd = existing.includes(PATH_END);
  if (hasStart !== hasEnd) fail(`PATH block is incomplete: ${profile}`);
  if (hasStart) return { changed: false, location: profile };
  const quoted = `'${binDir.replaceAll("'", "'\\''")}'`;
  const block = `${PATH_START}
case ":$PATH:" in
  *:${quoted}:*) ;;
  *) export PATH=${quoted}:$PATH ;;
esac
${PATH_END}
`;
  const separator = !existing || existing.endsWith('\n') ? '' : '\n';
  const mode = fs.existsSync(profile) ? fs.statSync(profile).mode & 0o777 : 0o644;
  atomicWrite(profile, `${existing}${separator}${block}`, mode);
  return { changed: true, location: profile };
}

async function main() {
  const args = parseArguments(process.argv.slice(2));
  const defaults = defaultLayout();
  const layout = {
    platform: defaults.platform,
    installRoot: path.resolve(args.installRoot || defaults.installRoot),
    binDir: path.resolve(args.binDir || defaults.binDir)
  };
  fs.mkdirSync(layout.installRoot, { recursive: true });

  let release;
  if (args.tag) {
    release = await runtime.getRelease(args.repository, args.tag);
    const expectedPreview = args.channel === 'preview';
    if (
      release.immutable !== true ||
      release.draft !== false ||
      release.prerelease !== expectedPreview
    ) {
      fail('The requested release is not immutable or is in the wrong channel.');
    }
  } else {
    release = runtime.selectRelease(
      await runtime.listReleases(args.repository),
      args.channel
    );
    if (!release) fail(`No immutable ${args.channel} release is available.`);
    const pointer = path.join(layout.installRoot, 'current.json');
    if (fs.existsSync(pointer)) {
      const current = runtime.readPointer(layout.installRoot).current;
      if (runtime.compareVersions(release.tag_name, current) <= 0) {
        release = await runtime.getRelease(args.repository, current);
      }
    }
  }

  const installed = await runtime.installRelease(
    args.repository,
    release,
    layout.installRoot
  );
  const shims = createShims(layout);
  const pathUpdate =
    layout.platform === 'linux' && args.updatePath
      ? updatePosixPath(layout.binDir)
      : { changed: false, location: null };

  console.log(`Installed ${path.basename(installed)} at ${installed}`);
  console.log('Command launchers:');
  for (const shim of shims) console.log(`  ${shim}`);
  if (pathUpdate.changed) console.log(`Updated PATH via ${pathUpdate.location}.`);
  console.log(
    pathUpdate.changed
      ? `Open a new terminal, then run: ${COMMAND}`
      : `Run: ${COMMAND}`
  );
  console.log(`BIN_DIR=${layout.binDir}`);
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`error: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = {
  createShims,
  defaultLayout,
  parseArguments,
  updatePosixPath
};
