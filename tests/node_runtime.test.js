'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { PassThrough } = require('node:stream');
const test = require('node:test');

const runtime = require('../scripts/release-runtime.js');
const installer = require('../scripts/install-command.js');

function digest(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function assetNames(tag) {
  return [
    `managed-cli-demo_${tag}.zip`,
    runtime.MANIFEST,
    runtime.CHECKSUMS,
    runtime.PROVENANCE
  ];
}

function release(tag, prerelease = false, overrides = {}) {
  return {
    assets: assetNames(tag).map((name) => ({ name })),
    draft: false,
    immutable: true,
    prerelease,
    tag_name: tag,
    ...overrides
  };
}

function makeStoredZip(entries) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.name, 'utf8');
    const value = Buffer.from(entry.value || '');
    const mode = entry.mode === undefined ? 0o644 : entry.mode;
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(0, 8);
    local.writeUInt32LE(0, 14);
    local.writeUInt32LE(value.length, 18);
    local.writeUInt32LE(value.length, 22);
    local.writeUInt16LE(name.length, 26);
    local.writeUInt16LE(0, 28);
    locals.push(local, name, value);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE((3 << 8) | 20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt32LE(0, 16);
    central.writeUInt32LE(value.length, 20);
    central.writeUInt32LE(value.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt16LE(0, 34);
    central.writeUInt16LE(0, 36);
    central.writeUInt32LE((mode * 65536) >>> 0, 38);
    central.writeUInt32LE(offset, 42);
    centrals.push(central, name);
    offset += local.length + name.length + value.length;
  }
  const centralDirectory = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralDirectory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralDirectory, end]);
}

function validManifest(zip, fileValue = Buffer.from('demo')) {
  const filePath = 'managed-cli-demo/app/demo.txt';
  return {
    artifact: {
      entryCount: 1,
      format: 'zip-store-v1',
      name: 'managed-cli-demo_v1.2.3.zip',
      sha256: digest(zip),
      sizeBytes: zip.length
    },
    contractSha256: '0'.repeat(64),
    files: [
      {
        mode: '0644',
        path: filePath,
        sha256: digest(fileValue),
        sizeBytes: fileValue.length
      }
    ],
    generatedAt: '2026-01-01T00:00:00Z',
    limits: {
      maxArchiveBytes: 10 * 1024 * 1024,
      maxEntryBytes: 1024 * 1024
    },
    product: 'managed-cli-release-demo',
    release: 'v1.2.3',
    requiredFiles: [filePath],
    schemaVersion: 1,
    source: {
      commit: 'a'.repeat(40),
      repository: 'owner/repository'
    }
  };
}

function createInstalledVersion(installRoot, tag, content) {
  const directory = path.join(installRoot, 'versions', tag);
  const file = Buffer.from(content);
  const manifest = {
    artifact: { sha256: digest(Buffer.from(`artifact-${tag}`)) },
    files: [
      {
        mode: '0644',
        path: 'managed-cli-demo/app/demo.txt',
        sha256: digest(file),
        sizeBytes: file.length
      }
    ],
    release: tag
  };
  fs.mkdirSync(path.join(directory, 'app'), { recursive: true });
  fs.writeFileSync(path.join(directory, 'app', 'demo.txt'), file);
  fs.writeFileSync(
    path.join(directory, '.release-manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`
  );
  fs.writeFileSync(
    path.join(directory, '.artifact-sha256'),
    `${manifest.artifact.sha256}\n`
  );
  return directory;
}

function temporaryDirectory(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'managed-cli-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test('SemVer comparison is strict and numeric', () => {
  assert.equal(runtime.compareVersions('v1.10.0', 'v1.9.9'), 1);
  assert.equal(runtime.compareVersions('v2.0.0', 'v2.0.0-rc.99'), 1);
  assert.equal(runtime.compareVersions('v1.0.0-rc.10', 'v1.0.0-rc.2'), 1);
  assert.equal(
    runtime.compareVersions(
      'v999999999999999999999999.0.0',
      'v999999999999999999999998.999.999'
    ),
    1
  );
  assert.throws(() => runtime.parseVersion('v1.0.0-01'), runtime.ReleaseError);
  assert.throws(() => runtime.parseVersion('1.0.0'), runtime.ReleaseError);
  assert.throws(
    () => runtime.validateRepository('../releases'),
    runtime.ReleaseError
  );
});

test('channel selection requires immutable complete releases', () => {
  const stable = runtime.selectRelease(
    [
      release('v1.9.0'),
      release('v1.10.0'),
      release('v2.0.0', false, { immutable: false }),
      release('v3.0.0', false, { draft: true }),
      release('v4.0.0', false, { assets: [] }),
      release('v5.0.0', false, { assets: [null] }),
      release('v6.0.0-01')
    ],
    'stable'
  );
  assert.equal(stable.tag_name, 'v1.10.0');
  assert.equal(
    runtime.selectRelease(
      [release('v2.0.0-rc.2', true), release('v2.0.0-rc.10', true)],
      'preview'
    ).tag_name,
    'v2.0.0-rc.10'
  );
  assert.equal(runtime.selectRelease([release('v2.0.0', false)], 'preview'), null);
});

test('stored ZIP parser accepts safe files', () => {
  const zip = makeStoredZip([
    { name: 'managed-cli-demo/app/demo.txt', value: 'demo', mode: 0o644 }
  ]);
  const entries = runtime.parseStoredZip(zip);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].name, 'managed-cli-demo/app/demo.txt');
  assert.equal(entries[0].value.toString(), 'demo');
});

test('stored ZIP parser rejects traversal, links, and duplicate paths', () => {
  assert.throws(
    () => runtime.parseStoredZip(makeStoredZip([{ name: '../escape', value: 'x' }])),
    runtime.ReleaseError
  );
  assert.throws(
    () =>
      runtime.parseStoredZip(
        makeStoredZip([
          { name: 'managed-cli-demo/link', value: 'target', mode: 0o120777 }
        ])
      ),
    /symbolic links/
  );
  assert.throws(
    () =>
      runtime.parseStoredZip(
        makeStoredZip([
          { name: 'managed-cli-demo/app/demo.txt', value: 'one' },
          { name: 'managed-cli-demo/app/DEMO.txt', value: 'two' }
        ])
      ),
    /Duplicate ZIP path/
  );
});

test('checksum and manifest validation reject tampering', (t) => {
  const directory = temporaryDirectory(t);
  const value = Buffer.from('demo');
  const zip = makeStoredZip([
    { name: 'managed-cli-demo/app/demo.txt', value }
  ]);
  const manifest = validManifest(zip, value);
  const manifestBytes = Buffer.from(`${JSON.stringify(manifest)}\n`);
  const zipName = manifest.artifact.name;
  fs.writeFileSync(path.join(directory, zipName), zip);
  fs.writeFileSync(path.join(directory, runtime.MANIFEST), manifestBytes);
  fs.writeFileSync(
    path.join(directory, runtime.CHECKSUMS),
    `${digest(zip)}  ${zipName}\n${digest(manifestBytes)}  ${runtime.MANIFEST}\n`
  );

  runtime.verifyChecksums(directory, 'v1.2.3');
  runtime.validateManifest(
    manifest,
    'owner/repository',
    'v1.2.3',
    'a'.repeat(40),
    zip
  );

  fs.appendFileSync(path.join(directory, runtime.MANIFEST), 'tamper');
  assert.throws(
    () => runtime.verifyChecksums(directory, 'v1.2.3'),
    /Checksum mismatch/
  );
  const changed = structuredClone(manifest);
  changed.files[0].sha256 = 'f'.repeat(64);
  assert.throws(
    () => runtime.extractRelease(zip, changed, path.join(directory, 'extract')),
    /ZIP content mismatch/
  );
});

test('async update lock remains held until work finishes', async (t) => {
  const root = temporaryDirectory(t);
  let entered;
  let release;
  const started = new Promise((resolve) => {
    entered = resolve;
  });
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const operation = runtime.withLock(root, async () => {
    entered();
    await gate;
    return 42;
  });
  await started;
  assert.equal(fs.existsSync(path.join(root, '.update.lock')), true);
  release();
  assert.equal(await operation, 42);
  assert.equal(fs.existsSync(path.join(root, '.update.lock')), false);
});

test('rollback verifies and atomically switches installed versions', async (t) => {
  const root = temporaryDirectory(t);
  const previous = createInstalledVersion(root, 'v1.0.0', 'old');
  createInstalledVersion(root, 'v2.0.0', 'new');
  runtime.writePointer(root, 'v2.0.0', 'v1.0.0');
  assert.equal(await runtime.rollbackInstall(root), previous);
  assert.deepEqual(
    JSON.parse(fs.readFileSync(path.join(root, 'current.json'), 'utf8')),
    {
      current: 'v1.0.0',
      previous: 'v2.0.0',
      schemaVersion: 1
    }
  );
});

test('command shims are created for Linux and Windows', (t) => {
  const root = temporaryDirectory(t);
  const linuxBin = path.join(root, 'linux-bin');
  const linuxShims = installer.createShims({
    platform: 'linux',
    installRoot: path.join(root, 'linux-root'),
    binDir: linuxBin
  });
  assert.deepEqual(linuxShims, [path.join(linuxBin, 'managed-cli-demo')]);
  assert.match(fs.readFileSync(linuxShims[0], 'utf8'), /current\.json/);
  if (process.platform !== 'win32') {
    assert.notEqual(fs.statSync(linuxShims[0]).mode & 0o111, 0);
  }

  const windowsBin = path.join(root, 'windows-bin');
  const windowsShims = installer.createShims({
    platform: 'windows',
    installRoot: 'C:\\Users\\demo\\ManagedCliDemo',
    binDir: windowsBin
  });
  assert.equal(windowsShims.length, 3);
  assert.equal(fs.existsSync(path.join(windowsBin, 'managed-cli-demo.ps1')), true);
  assert.equal(fs.existsSync(path.join(windowsBin, 'managed-cli-demo.cmd')), true);
  assert.equal(fs.existsSync(path.join(windowsBin, 'managed-cli-demo')), true);
});

test('POSIX PATH persistence is idempotent', (t) => {
  const home = temporaryDirectory(t);
  const bin = path.join(home, '.local', 'bin');
  const originalPath = process.env.PATH;
  process.env.PATH = '/usr/bin';
  t.after(() => {
    process.env.PATH = originalPath;
  });
  const first = installer.updatePosixPath(bin, home, '/bin/bash');
  const second = installer.updatePosixPath(bin, home, '/bin/bash');
  assert.equal(first.changed, true);
  assert.equal(second.changed, false);
  const profile = fs.readFileSync(path.join(home, '.profile'), 'utf8');
  assert.equal(profile.match(/managed-cli-demo PATH/g).length, 2);
  assert.equal(profile.match(/# >>> managed-cli-demo PATH >>>/g).length, 1);
});

test('POSIX PATH persistence rejects a partial managed block', (t) => {
  const home = temporaryDirectory(t);
  fs.writeFileSync(
    path.join(home, '.profile'),
    '# >>> managed-cli-demo PATH >>>\n'
  );
  assert.throws(
    () => installer.updatePosixPath('/example/bin', home, '/bin/bash'),
    /PATH block is incomplete/
  );
});

async function answerPrompt(answer, isTTY = true) {
  const input = new PassThrough();
  const output = new PassThrough();
  input.isTTY = isTTY;
  input.end(answer);
  return runtime.confirmUpdate(
    {
      current: 'v1.0.0',
      latest: {
        html_url: 'https://github.com/owner/repository/releases/tag/v1.1.0',
        tag_name: 'v1.1.0'
      }
    },
    input,
    output
  );
}

test('update confirmation defaults to no and requires a TTY', async () => {
  assert.equal(await answerPrompt('', false), false);
  assert.equal(await answerPrompt('\n'), false);
  assert.equal(await answerPrompt('no\n'), false);
  assert.equal(await answerPrompt('yes\n'), true);
});
