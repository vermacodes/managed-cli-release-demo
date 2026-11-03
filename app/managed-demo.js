#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const infoPath = path.join(root, 'release-info.json');
let info = {
  product: 'managed-cli-release-demo',
  release: 'development',
  sourceCommit: 'working-tree',
  sourceRepository: 'vermacodes/managed-cli-release-demo'
};

if (fs.existsSync(infoPath)) {
  info = JSON.parse(fs.readFileSync(infoPath, 'utf8'));
}

function validReleaseInfo(value) {
  return (
    value &&
    value.schemaVersion === 1 &&
    value.product === 'managed-cli-release-demo' &&
    typeof value.release === 'string' &&
    typeof value.sourceCommit === 'string' &&
    typeof value.sourceRepository === 'string'
  );
}

function display() {
  if (process.argv.includes('--json')) {
    console.log(JSON.stringify(info, null, 2));
    return;
  }
  const commit = info.sourceCommit.substring(0, 12);
  console.log(`Managed CLI Release Demo ${info.release} (${commit})`);
}

async function maybeUpdate() {
  if (
    !fs.existsSync(infoPath) ||
    process.env.MANAGED_CLI_REEXEC === '1' ||
    process.argv.includes('--self-test')
  ) {
    return false;
  }
  const versionsRoot = path.dirname(root);
  const installRoot = path.dirname(versionsRoot);
  const runtime = require('../scripts/release-runtime.js');
  const channel = process.env.MANAGED_CLI_UPDATE_CHANNEL || 'preview';
  const status = await runtime.checkUpdate(
    info.sourceRepository,
    channel,
    installRoot
  );
  if (!status.updateAvailable) return false;
  if (!(await runtime.confirmUpdate(status))) {
    console.log('Update declined; continuing with the current version.');
    return false;
  }
  const reviewed = status.latest.tag_name;
  const current = await runtime.checkUpdate(
    info.sourceRepository,
    channel,
    installRoot
  );
  if (!current.updateAvailable || current.latest.tag_name !== reviewed) {
    throw new runtime.ReleaseError(
      'The update channel changed after confirmation; review it again.'
    );
  }
  const installed = await runtime.installRelease(
    info.sourceRepository,
    current.latest,
    installRoot
  );
  console.log(`Updated to ${current.latest.tag_name}.`);
  const result = spawnSync(
    process.execPath,
    [path.join(installed, 'app', 'managed-demo.js'), ...process.argv.slice(2)],
    {
      stdio: 'inherit',
      env: { ...process.env, MANAGED_CLI_REEXEC: '1' }
    }
  );
  process.exitCode = result.status === null ? 1 : result.status;
  return true;
}

async function rollback() {
  if (!fs.existsSync(infoPath)) {
    throw new Error('Rollback is available only from an installed release.');
  }
  const installRoot = path.dirname(path.dirname(root));
  const runtime = require('../scripts/release-runtime.js');
  const installed = await runtime.rollbackInstall(installRoot);
  console.log(`Rolled back to ${path.basename(installed)}.`);
  const argumentsWithoutRollback = process.argv
    .slice(2)
    .filter((value) => value !== '--rollback');
  const result = spawnSync(
    process.execPath,
    [path.join(installed, 'app', 'managed-demo.js'), ...argumentsWithoutRollback],
    {
      stdio: 'inherit',
      env: { ...process.env, MANAGED_CLI_REEXEC: '1' }
    }
  );
  process.exitCode = result.status === null ? 1 : result.status;
}

async function main() {
  if (process.argv.includes('--self-test')) {
    if (!validReleaseInfo(info)) {
      console.error('release-info.json failed validation');
      process.exitCode = 1;
      return;
    }
    console.log('self-test passed');
    return;
  }
  if (process.argv.includes('--rollback')) {
    await rollback();
    return;
  }

  try {
    if (await maybeUpdate()) return;
  } catch (error) {
    console.error(
      `Update check failed; continuing with the current version: ${error.message}`
    );
  }
  display();
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
