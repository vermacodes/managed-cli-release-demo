#!/usr/bin/env node
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const readline = require('readline');

const MANIFEST = 'release-manifest.json';
const CHECKSUMS = 'SHA256SUMS.txt';
const PROVENANCE = 'build-provenance.jsonl';
const API_ROOT = 'https://api.github.com';
const USER_AGENT = 'managed-cli-release-demo/2';
const MAX_ASSET_BYTES = 25 * 1024 * 1024;
const MAX_ARCHIVE_BYTES = 10 * 1024 * 1024;
const MAX_ENTRY_BYTES = 1024 * 1024;
const RELEASE_PATTERN =
  /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;
const SHA_PATTERN = /^[0-9a-f]{64}$/;

class ReleaseError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ReleaseError';
  }
}

function assert(condition, message) {
  if (!condition) throw new ReleaseError(message);
}

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function parseVersion(version) {
  const match = RELEASE_PATTERN.exec(version);
  assert(match, `Invalid release version: ${version}`);
  const prerelease = match[4]
    ? match[4].split('.').map((part) => {
        const numeric = /^[0-9]+$/.test(part);
        assert(
          !numeric || part === '0' || !part.startsWith('0'),
          `Invalid release version: ${version}`
        );
        return {
          numeric,
          value: numeric ? BigInt(part) : part
        };
      })
    : null;
  return {
    core: [BigInt(match[1]), BigInt(match[2]), BigInt(match[3])],
    prerelease
  };
}

function compareVersions(left, right) {
  const a = parseVersion(left);
  const b = parseVersion(right);
  for (let index = 0; index < 3; index += 1) {
    if (a.core[index] !== b.core[index]) {
      return a.core[index] < b.core[index] ? -1 : 1;
    }
  }
  if (a.prerelease === null || b.prerelease === null) {
    if (a.prerelease === b.prerelease) return 0;
    return a.prerelease === null ? 1 : -1;
  }
  const length = Math.max(a.prerelease.length, b.prerelease.length);
  for (let index = 0; index < length; index += 1) {
    const av = a.prerelease[index];
    const bv = b.prerelease[index];
    if (!av || !bv) return av ? 1 : -1;
    if (av.numeric && bv.numeric && av.value !== bv.value) {
      return av.value < bv.value ? -1 : 1;
    }
    if (av.numeric !== bv.numeric) return av.numeric ? -1 : 1;
    if (av.value !== bv.value) return av.value < bv.value ? -1 : 1;
  }
  return 0;
}

function validateRepository(repository) {
  const parts =
    typeof repository === 'string' ? repository.split('/') : [];
  assert(
    parts.length === 2 &&
      parts.every(
        (part) => /^[A-Za-z0-9_.-]+$/.test(part) && !['.', '..'].includes(part)
      ),
    `Invalid repository: ${repository}`
  );
  return repository;
}

function expectedAssetNames(version) {
  parseVersion(version);
  return [
    `managed-cli-demo_${version}.zip`,
    MANIFEST,
    CHECKSUMS,
    PROVENANCE
  ];
}

async function fetchResponse(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      headers: {
        Accept: 'application/vnd.github+json',
        'User-Agent': USER_AGENT,
        ...(options.headers || {})
      },
      signal: AbortSignal.timeout(options.timeout || 30000)
    });
  } catch (error) {
    throw new ReleaseError(`Network request failed: ${error.message}`);
  }
  assert(response.ok, `Request failed (${response.status}): ${url}`);
  return response;
}

async function fetchJson(url) {
  const response = await fetchResponse(url);
  try {
    return await response.json();
  } catch (error) {
    throw new ReleaseError(`Invalid JSON from ${url}: ${error.message}`);
  }
}

async function listReleases(repository) {
  validateRepository(repository);
  const releases = [];
  for (let page = 1; page <= 100; page += 1) {
    const values = await fetchJson(
      `${API_ROOT}/repos/${repository}/releases?per_page=100&page=${page}`
    );
    assert(Array.isArray(values), 'GitHub returned invalid release metadata.');
    releases.push(...values);
    if (values.length < 100) return releases;
  }
  throw new ReleaseError('Release pagination exceeded its safety limit.');
}

async function getRelease(repository, tag) {
  validateRepository(repository);
  parseVersion(tag);
  const release = await fetchJson(
    `${API_ROOT}/repos/${repository}/releases/tags/${encodeURIComponent(tag)}`
  );
  assert(release && release.tag_name === tag, 'GitHub returned the wrong release.');
  return release;
}

function selectRelease(releases, channel) {
  assert(channel === 'stable' || channel === 'preview', 'Invalid channel.');
  const candidates = releases.filter((release) => {
    if (
      !release ||
      release.draft !== false ||
      release.immutable !== true ||
      (channel === 'preview') !== (release.prerelease === true) ||
      !RELEASE_PATTERN.test(release.tag_name || '') ||
      !Array.isArray(release.assets) ||
      !release.assets.every(
        (asset) => asset && typeof asset.name === 'string'
      )
    ) {
      return false;
    }
    const names = new Set(release.assets.map((asset) => asset.name));
    let expected;
    try {
      expected = expectedAssetNames(release.tag_name);
    } catch (error) {
      if (error instanceof ReleaseError) return false;
      throw error;
    }
    return (
      release.assets.length === expected.length &&
      names.size === expected.length &&
      expected.every((name) => names.has(name))
    );
  });
  candidates.sort((left, right) =>
    compareVersions(right.tag_name, left.tag_name)
  );
  return candidates[0] || null;
}

async function resolveTagCommit(repository, tag) {
  validateRepository(repository);
  let object = (
    await fetchJson(
      `${API_ROOT}/repos/${repository}/git/ref/tags/${encodeURIComponent(tag)}`
    )
  ).object;
  for (let depth = 0; depth < 5; depth += 1) {
    assert(
      object && /^[0-9a-f]{40}$/.test(object.sha || ''),
      `Tag ${tag} has invalid metadata.`
    );
    if (object.type === 'commit') return object.sha;
    assert(object.type === 'tag', `Tag ${tag} does not resolve to a commit.`);
    object = (
      await fetchJson(`${API_ROOT}/repos/${repository}/git/tags/${object.sha}`)
    ).object;
  }
  throw new ReleaseError(`Tag ${tag} nesting exceeded its limit.`);
}

async function downloadAsset(url, destination) {
  const parsed = new URL(url);
  assert(parsed.protocol === 'https:', 'Asset URL must use HTTPS.');
  const response = await fetchResponse(url, {
    headers: { Accept: 'application/octet-stream' },
    timeout: 120000
  });
  const finalUrl = new URL(response.url);
  const host = finalUrl.hostname.toLowerCase();
  assert(
    finalUrl.protocol === 'https:' &&
      (host === 'github.com' ||
        host === 'objects.githubusercontent.com' ||
        host.endsWith('.githubusercontent.com')),
    `Asset redirected to an untrusted host: ${host}`
  );
  const declared = Number(response.headers.get('content-length') || 0);
  assert(
    !declared || declared <= MAX_ASSET_BYTES,
    'Asset exceeds its size limit.'
  );
  const value = Buffer.from(await response.arrayBuffer());
  assert(value.length <= MAX_ASSET_BYTES, 'Asset exceeds its size limit.');
  fs.writeFileSync(destination, value, { flag: 'wx', mode: 0o600 });
  return value;
}

function verifyChecksums(directory, version) {
  const zipName = `managed-cli-demo_${version}.zip`;
  const lines = fs
    .readFileSync(path.join(directory, CHECKSUMS), 'ascii')
    .trim()
    .split(/\r?\n/);
  const values = new Map();
  for (const line of lines) {
    const match = /^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$/.exec(line);
    assert(match && !values.has(match[2]), 'Checksum file is invalid.');
    values.set(match[2], match[1]);
  }
  assert(
    values.size === 2 && values.has(zipName) && values.has(MANIFEST),
    'Checksum file does not cover the expected assets.'
  );
  for (const [name, digest] of values) {
    assert(
      sha256(fs.readFileSync(path.join(directory, name))) === digest,
      `Checksum mismatch: ${name}`
    );
  }
}

function validateArchivePath(value) {
  assert(
    value &&
      !value.includes('\0') &&
      !value.includes('\\') &&
      !value.startsWith('/') &&
      !/^[A-Za-z]:/.test(value) &&
      /^[A-Za-z0-9._/-]+$/.test(value),
    `Unsafe ZIP path: ${value}`
  );
  const parts = value.split('/');
  assert(
    parts.every((part) => part && part !== '.' && part !== '..'),
    `Unsafe ZIP path: ${value}`
  );
  return parts;
}


function strictKeys(value, expected, label) {
  assert(
    value && typeof value === 'object' && !Array.isArray(value),
    `${label} must be an object.`
  );
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  assert(
    actual.length === wanted.length &&
      actual.every((key, index) => key === wanted[index]),
    `${label} has unexpected fields.`
  );
}

function validateManifest(manifest, repository, version, commit, zip) {
  strictKeys(
    manifest,
    [
      'artifact',
      'contractSha256',
      'files',
      'generatedAt',
      'limits',
      'product',
      'release',
      'requiredFiles',
      'schemaVersion',
      'source'
    ],
    'manifest'
  );
  assert(manifest.schemaVersion === 1, 'Unsupported manifest schema.');
  assert(manifest.product === 'managed-cli-release-demo', 'Unexpected product.');
  assert(manifest.release === version, 'Manifest release identity is invalid.');
  assert(SHA_PATTERN.test(manifest.contractSha256), 'Invalid contract digest.');
  assert(
    typeof manifest.generatedAt === 'string' &&
      Number.isFinite(Date.parse(manifest.generatedAt)),
    'Invalid manifest timestamp.'
  );

  strictKeys(manifest.source, ['commit', 'repository'], 'source');
  assert(
    /^[0-9a-f]{40}$/.test(manifest.source.commit) &&
      manifest.source.commit === commit &&
      /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(manifest.source.repository) &&
      manifest.source.repository.toLowerCase() === repository.toLowerCase(),
    'Manifest source identity is invalid.'
  );

  strictKeys(manifest.limits, ['maxArchiveBytes', 'maxEntryBytes'], 'limits');
  assert(
    Number.isSafeInteger(manifest.limits.maxArchiveBytes) &&
      manifest.limits.maxArchiveBytes > 0 &&
      manifest.limits.maxArchiveBytes <= MAX_ARCHIVE_BYTES &&
      Number.isSafeInteger(manifest.limits.maxEntryBytes) &&
      manifest.limits.maxEntryBytes > 0 &&
      manifest.limits.maxEntryBytes <= MAX_ENTRY_BYTES,
    'Manifest limits are invalid.'
  );

  strictKeys(
    manifest.artifact,
    ['entryCount', 'format', 'name', 'sha256', 'sizeBytes'],
    'artifact'
  );
  const zipName = `managed-cli-demo_${version}.zip`;
  assert(
    manifest.artifact.name === zipName &&
      manifest.artifact.format === 'zip-store-v1' &&
      SHA_PATTERN.test(manifest.artifact.sha256) &&
      Number.isSafeInteger(manifest.artifact.sizeBytes) &&
      manifest.artifact.sizeBytes > 0 &&
      manifest.artifact.sizeBytes <= manifest.limits.maxArchiveBytes &&
      manifest.artifact.sizeBytes === zip.length &&
      manifest.artifact.sha256 === sha256(zip) &&
      Number.isSafeInteger(manifest.artifact.entryCount) &&
      manifest.artifact.entryCount > 0,
    'Manifest artifact identity is invalid.'
  );

  assert(Array.isArray(manifest.files) && manifest.files.length > 0, 'Invalid files.');
  assert(
    manifest.files.length === manifest.artifact.entryCount,
    'Manifest entry count is invalid.'
  );
  const exact = new Set();
  const folded = new Set();
  let totalSize = 0;
  for (const [index, record] of manifest.files.entries()) {
    strictKeys(record, ['mode', 'path', 'sha256', 'sizeBytes'], `files[${index}]`);
    validateArchivePath(record.path);
    assert(
      record.path.startsWith('managed-cli-demo/') &&
        !exact.has(record.path) &&
        !folded.has(record.path.toLowerCase()),
      `Invalid or duplicate manifest path: ${record.path}`
    );
    exact.add(record.path);
    folded.add(record.path.toLowerCase());
    assert(['0644', '0755'].includes(record.mode), `Invalid mode: ${record.path}`);
    assert(SHA_PATTERN.test(record.sha256), `Invalid digest: ${record.path}`);
    assert(
      Number.isSafeInteger(record.sizeBytes) &&
        record.sizeBytes >= 0 &&
        record.sizeBytes <= manifest.limits.maxEntryBytes,
      `Invalid size: ${record.path}`
    );
    totalSize += record.sizeBytes;
  }
  assert(totalSize <= manifest.limits.maxArchiveBytes, 'Manifest payload is too large.');

  assert(
    Array.isArray(manifest.requiredFiles) &&
      manifest.requiredFiles.every((value) => typeof value === 'string'),
    'Invalid required files.'
  );
  const required = new Set();
  for (const value of manifest.requiredFiles) {
    validateArchivePath(value);
    assert(!required.has(value) && exact.has(value), `Invalid required file: ${value}`);
    required.add(value);
  }
  return manifest;
}

function parseStoredZip(buffer) {
  const minimum = Math.max(0, buffer.length - 65557);
  let eocd = -1;
  for (let index = buffer.length - 22; index >= minimum; index -= 1) {
    if (buffer.readUInt32LE(index) === 0x06054b50) {
      eocd = index;
      break;
    }
  }
  assert(eocd >= 0, 'ZIP end record is missing.');
  const disk = buffer.readUInt16LE(eocd + 4);
  const centralDisk = buffer.readUInt16LE(eocd + 6);
  const diskCount = buffer.readUInt16LE(eocd + 8);
  const count = buffer.readUInt16LE(eocd + 10);
  const centralSize = buffer.readUInt32LE(eocd + 12);
  const centralOffset = buffer.readUInt32LE(eocd + 16);
  const commentLength = buffer.readUInt16LE(eocd + 20);
  assert(disk === 0 && centralDisk === 0, 'Multi-disk ZIPs are not supported.');
  assert(
    diskCount === count && eocd + 22 + commentLength === buffer.length,
    'ZIP end record is invalid.'
  );
  assert(
    count !== 0xffff &&
      centralSize !== 0xffffffff &&
      centralOffset !== 0xffffffff &&
      centralOffset + centralSize === eocd,
    'ZIP64 or invalid ZIP metadata is not supported.'
  );

  const entries = [];
  const exact = new Set();
  const folded = new Set();
  let position = centralOffset;
  for (let index = 0; index < count; index += 1) {
    assert(
      position + 46 <= buffer.length &&
        buffer.readUInt32LE(position) === 0x02014b50,
      'ZIP central directory is invalid.'
    );
    const madeBy = buffer.readUInt16LE(position + 4);
    const flags = buffer.readUInt16LE(position + 8);
    const method = buffer.readUInt16LE(position + 10);
    const compressedSize = buffer.readUInt32LE(position + 20);
    const size = buffer.readUInt32LE(position + 24);
    const nameLength = buffer.readUInt16LE(position + 28);
    const extraLength = buffer.readUInt16LE(position + 30);
    const commentLength = buffer.readUInt16LE(position + 32);
    const external = buffer.readUInt32LE(position + 38);
    const localOffset = buffer.readUInt32LE(position + 42);
    const end = position + 46 + nameLength + extraLength + commentLength;
    assert(end <= buffer.length, 'ZIP entry metadata is truncated.');
    const nameBytes = buffer.subarray(
      position + 46,
      position + 46 + nameLength
    );
    const name = nameBytes.toString('utf8');
    assert(Buffer.from(name, 'utf8').equals(nameBytes), 'ZIP path is not valid UTF-8.');
    validateArchivePath(name);
    assert(!exact.has(name) && !folded.has(name.toLowerCase()), 'Duplicate ZIP path.');
    exact.add(name);
    folded.add(name.toLowerCase());
    assert(!(flags & 1) && !(flags & 8), 'Encrypted/data-descriptor ZIPs are rejected.');
    assert(method === 0 && compressedSize === size, 'Only stored ZIP entries are supported.');
    const unixMode = madeBy >> 8 === 3 ? external >>> 16 : 0;
    assert((unixMode & 0xf000) !== 0xa000, 'ZIP symbolic links are rejected.');
    assert(!name.endsWith('/'), 'Directory ZIP entries are rejected.');
    assert(
      localOffset + 30 <= buffer.length &&
        buffer.readUInt32LE(localOffset) === 0x04034b50,
      'ZIP local header is invalid.'
    );
    const localFlags = buffer.readUInt16LE(localOffset + 6);
    const localMethod = buffer.readUInt16LE(localOffset + 8);
    const localCompressed = buffer.readUInt32LE(localOffset + 18);
    const localSize = buffer.readUInt32LE(localOffset + 22);
    const localName = buffer.readUInt16LE(localOffset + 26);
    const localExtra = buffer.readUInt16LE(localOffset + 28);
    const localNameStart = localOffset + 30;
    const dataOffset = localNameStart + localName + localExtra;
    assert(
      localFlags === flags &&
        localMethod === method &&
        localCompressed === compressedSize &&
        localSize === size &&
        localName === nameLength &&
        buffer.subarray(localNameStart, localNameStart + localName).equals(nameBytes),
      'ZIP local and central headers differ.'
    );
    assert(dataOffset + size <= centralOffset, 'ZIP entry data is truncated.');
    entries.push({
      name,
      mode: unixMode ? unixMode & 0o777 : 0o644,
      value: buffer.subarray(dataOffset, dataOffset + size)
    });
    position = end;
  }
  assert(position === centralOffset + centralSize, 'ZIP central directory size mismatch.');
  return entries;
}

function atomicWrite(filePath, value, mode = 0o644) {
  const temporary = `${filePath}.${crypto.randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temporary, value, { flag: 'wx', mode });
    fs.renameSync(temporary, filePath);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

function verifyInstalled(versionDirectory, manifest) {
  assert(
    manifest &&
      typeof manifest === 'object' &&
      Array.isArray(manifest.files) &&
      manifest.files.length > 0 &&
      manifest.artifact &&
      SHA_PATTERN.test(manifest.artifact.sha256 || ''),
    'Installed manifest is invalid.'
  );
  parseVersion(manifest.release);
  const expected = new Map();
  for (const record of manifest.files) {
    strictKeys(record, ['mode', 'path', 'sha256', 'sizeBytes'], 'installed file');
    validateArchivePath(record.path);
    const parts = record.path.split('/');
    const relative = parts.slice(1).join('/');
    assert(
      parts[0] === 'managed-cli-demo' &&
        relative &&
        !expected.has(relative) &&
        ['0644', '0755'].includes(record.mode) &&
        SHA_PATTERN.test(record.sha256) &&
        Number.isSafeInteger(record.sizeBytes) &&
        record.sizeBytes >= 0,
      `Installed manifest record is invalid: ${record.path}`
    );
    expected.set(relative, record);
  }
  const actual = [];
  function walk(directory, relative = '') {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const current = path.join(directory, entry.name);
      const child = relative ? `${relative}/${entry.name}` : entry.name;
      const stats = fs.lstatSync(current);
      assert(!stats.isSymbolicLink(), `Installed link is rejected: ${child}`);
      if (stats.isDirectory()) walk(current, child);
      else actual.push(child);
    }
  }
  walk(versionDirectory);
  const allowed = new Set([
    ...expected.keys(),
    '.artifact-sha256',
    '.release-manifest.json'
  ]);
  assert(
    actual.length === allowed.size && actual.every((value) => allowed.has(value)),
    'Installed file set differs from its manifest.'
  );
  for (const [relative, record] of expected) {
    const value = fs.readFileSync(path.join(versionDirectory, relative));
    assert(value.length === record.sizeBytes, `Installed size mismatch: ${relative}`);
    assert(sha256(value) === record.sha256, `Installed digest mismatch: ${relative}`);
  }
  let storedManifest;
  try {
    storedManifest = JSON.parse(
      fs.readFileSync(path.join(versionDirectory, '.release-manifest.json'), 'utf8')
    );
  } catch (error) {
    throw new ReleaseError(`Installed manifest cannot be read: ${error.message}`);
  }
  assert(
    JSON.stringify(storedManifest) === JSON.stringify(manifest),
    'Stored release manifest differs from the verified manifest.'
  );
  assert(
    fs.readFileSync(path.join(versionDirectory, '.artifact-sha256'), 'ascii').trim() ===
      manifest.artifact.sha256,
    'Stored artifact digest is invalid.'
  );
}

async function verifyDownloadedRelease(repository, release, directory) {
  const version = release.tag_name;
  const commit = await resolveTagCommit(repository, version);
  const expectedNames = expectedAssetNames(version);
  assert(
    Array.isArray(release.assets) &&
      release.assets.every(
        (asset) => asset && typeof asset.name === 'string'
      ),
    'Release assets are invalid.'
  );
  const assets = new Map(release.assets.map((asset) => [asset.name, asset]));
  assert(
    release.assets.length === expectedNames.length &&
      assets.size === expectedNames.length &&
      expectedNames.every((name) => assets.has(name)),
    'Release assets do not match the contract.'
  );
  for (const name of expectedNames) {
    const asset = assets.get(name);
    assert(
      asset.state === 'uploaded' &&
        typeof asset.digest === 'string' &&
        asset.digest.startsWith('sha256:') &&
        SHA_PATTERN.test(asset.digest.slice(7)),
      `Invalid asset metadata: ${name}`
    );
    const value = await downloadAsset(
      asset.browser_download_url,
      path.join(directory, name)
    );
    assert(sha256(value) === asset.digest.slice(7), `Asset digest mismatch: ${name}`);
  }

  verifyChecksums(directory, version);
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(path.join(directory, MANIFEST), 'utf8'));
  } catch (error) {
    throw new ReleaseError(`Release manifest is invalid: ${error.message}`);
  }
  const zipName = `managed-cli-demo_${version}.zip`;
  const zip = fs.readFileSync(path.join(directory, zipName));
  validateManifest(manifest, repository, version, commit, zip);

  const bundle = path.join(directory, PROVENANCE);
  for (const name of [zipName, MANIFEST, CHECKSUMS]) {
    const result = spawnSync(
      'gh',
      [
        'attestation',
        'verify',
        path.join(directory, name),
        '--bundle',
        bundle,
        '--repo',
        repository,
        '--signer-workflow',
        `${repository}/.github/workflows/release.yml`,
        '--signer-digest',
        commit,
        '--source-digest',
        commit,
        '--source-ref',
        'refs/heads/main'
      ],
      { encoding: 'utf8' }
    );
    assert(
      result.status === 0,
      `Provenance verification failed for ${name}: ${result.stderr || result.stdout}`
    );
  }
  assert((await resolveTagCommit(repository, version)) === commit, 'Release tag changed.');
  return { manifest, zip };
}

function extractRelease(zip, manifest, destination) {
  const records = new Map(manifest.files.map((file) => [file.path, file]));
  const entries = parseStoredZip(zip);
  assert(entries.length === records.size, 'ZIP and manifest file counts differ.');
  fs.mkdirSync(destination, { recursive: false });
  for (const entry of entries) {
    const record = records.get(entry.name);
    assert(record, `ZIP contains unmanifested file: ${entry.name}`);
    assert(
      entry.value.length === record.sizeBytes && sha256(entry.value) === record.sha256,
      `ZIP content mismatch: ${entry.name}`
    );
    const relative = entry.name.split('/').slice(1);
    assert(relative.length > 0, 'ZIP root file is invalid.');
    const target = path.join(destination, ...relative);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, entry.value, { flag: 'wx', mode: Number.parseInt(record.mode, 8) });
  }
}

function readPointer(installRoot) {
  let value;
  try {
    value = JSON.parse(
      fs.readFileSync(path.join(installRoot, 'current.json'), 'utf8')
    );
  } catch (error) {
    throw new ReleaseError(`Installation pointer is invalid: ${error.message}`);
  }
  strictKeys(value, ['current', 'previous', 'schemaVersion'], 'installation pointer');
  assert(value.schemaVersion === 1, 'Installation pointer schema is invalid.');
  parseVersion(value.current);
  if (value.previous !== null) parseVersion(value.previous);
  return value;
}

function writePointer(installRoot, current, previous) {
  atomicWrite(
    path.join(installRoot, 'current.json'),
    `${JSON.stringify({ current, previous, schemaVersion: 1 }, null, 2)}\n`
  );
}

async function rollbackInstall(installRoot) {
  return withLock(installRoot, async () => {
    const pointer = readPointer(installRoot);
    assert(pointer.previous, 'No previous installed version is available.');
    assert(pointer.previous !== pointer.current, 'Rollback pointer is invalid.');
    const target = path.join(installRoot, 'versions', pointer.previous);
    const targetStats = fs.existsSync(target) ? fs.lstatSync(target) : null;
    assert(
      targetStats && targetStats.isDirectory() && !targetStats.isSymbolicLink(),
      `Previous version is missing: ${pointer.previous}`
    );
    let manifest;
    try {
      manifest = JSON.parse(
        fs.readFileSync(path.join(target, '.release-manifest.json'), 'utf8')
      );
    } catch (error) {
      throw new ReleaseError(`Previous version manifest is invalid: ${error.message}`);
    }
    assert(
      manifest.release === pointer.previous,
      'Previous version identity is invalid.'
    );
    verifyInstalled(target, manifest);
    writePointer(installRoot, pointer.previous, pointer.current);
    return target;
  });
}

async function withLock(installRoot, action) {
  fs.mkdirSync(installRoot, { recursive: true });
  const lock = path.join(installRoot, '.update.lock');
  let descriptor;
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    try {
      descriptor = fs.openSync(lock, 'wx', 0o600);
      break;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      try {
        const age = Date.now() - fs.statSync(lock).mtimeMs;
        if (age > 10 * 60 * 1000) fs.rmSync(lock, { force: true });
        else await new Promise((resolve) => setTimeout(resolve, 100));
      } catch (statError) {
        if (statError.code !== 'ENOENT') throw statError;
      }
    }
  }
  assert(descriptor !== undefined, 'Timed out waiting for the update lock.');
  try {
    return await action();
  } finally {
    fs.closeSync(descriptor);
    fs.rmSync(lock, { force: true });
  }
}

async function installRelease(repository, release, installRoot) {
  return withLock(installRoot, async () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'managed-cli-release-'));
    try {
      const { manifest, zip } = await verifyDownloadedRelease(
        repository,
        release,
        temporary
      );
      const versions = path.join(installRoot, 'versions');
      fs.mkdirSync(versions, { recursive: true });
      const finalDirectory = path.join(versions, release.tag_name);
      if (!fs.existsSync(finalDirectory)) {
        const staging = path.join(
          versions,
          `.${release.tag_name}.staging-${crypto.randomUUID()}`
        );
        try {
          extractRelease(zip, manifest, staging);
          atomicWrite(
            path.join(staging, '.release-manifest.json'),
            `${JSON.stringify(manifest, null, 2)}\n`
          );
          atomicWrite(
            path.join(staging, '.artifact-sha256'),
            `${manifest.artifact.sha256}\n`
          );
          const health = spawnSync(
            process.execPath,
            [path.join(staging, 'app', 'managed-demo.js'), '--self-test'],
            { encoding: 'utf8' }
          );
          assert(health.status === 0, `Health check failed: ${health.stderr}`);
          verifyInstalled(staging, manifest);
          fs.renameSync(staging, finalDirectory);
        } finally {
          fs.rmSync(staging, { recursive: true, force: true });
        }
      } else {
        const finalStats = fs.lstatSync(finalDirectory);
        assert(
          finalStats.isDirectory() && !finalStats.isSymbolicLink(),
          `Installed version path is invalid: ${release.tag_name}`
        );
        verifyInstalled(finalDirectory, manifest);
      }
      const pointerPath = path.join(installRoot, 'current.json');
      const pointer = fs.existsSync(pointerPath)
        ? readPointer(installRoot)
        : { current: null, previous: null };
      const previous =
        pointer.current === release.tag_name ? pointer.previous : pointer.current;
      writePointer(installRoot, release.tag_name, previous);
      return finalDirectory;
    } finally {
      fs.rmSync(temporary, { recursive: true, force: true });
    }
  });
}

async function checkUpdate(repository, channel, installRoot) {
  const current = fs.existsSync(path.join(installRoot, 'current.json'))
    ? readPointer(installRoot).current
    : null;
  const latest = selectRelease(await listReleases(repository), channel);
  return {
    current,
    latest,
    updateAvailable: Boolean(
      latest && (!current || compareVersions(latest.tag_name, current) > 0)
    )
  };
}

async function confirmUpdate(
  status,
  input = process.stdin,
  output = process.stdout
) {
  output.write(
    `Update available: ${status.current || 'not-installed'} -> ${status.latest.tag_name}\n` +
      `Release: ${status.latest.html_url}\n`
  );
  if (!input.isTTY) {
    output.write('Update skipped: interactive confirmation is required.\n');
    return false;
  }
  const reader = readline.createInterface({
    input,
    output
  });
  const answer = await new Promise((resolve) =>
    reader.question('Install this update now? [y/N] ', resolve)
  );
  reader.close();
  return ['y', 'yes'].includes(answer.trim().toLowerCase());
}

module.exports = {
  CHECKSUMS,
  MANIFEST,
  PROVENANCE,
  ReleaseError,
  checkUpdate,
  compareVersions,
  confirmUpdate,
  extractRelease,
  getRelease,
  installRelease,
  listReleases,
  parseStoredZip,
  parseVersion,
  readPointer,
  rollbackInstall,
  selectRelease,
  validateManifest,
  validateRepository,
  verifyChecksums,
  verifyInstalled,
  withLock,
  writePointer
};
