#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const infoPath = path.join(root, 'release-info.json');
let info = {
  product: 'managed-cli-release-demo',
  release: 'development',
  sourceCommit: 'working-tree'
};

if (fs.existsSync(infoPath)) {
  info = JSON.parse(fs.readFileSync(infoPath, 'utf8'));
}

function validReleaseInfo(value) {
  return value &&
    value.schemaVersion === 1 &&
    value.product === 'managed-cli-release-demo' &&
    typeof value.release === 'string' &&
    typeof value.sourceCommit === 'string';
}

if (process.argv.includes('--self-test')) {
  if (!validReleaseInfo(info)) {
    console.error('release-info.json failed validation');
    process.exit(1);
  }
  console.log('self-test passed');
  process.exit(0);
}

if (process.argv.includes('--json')) {
  console.log(JSON.stringify(info, null, 2));
  process.exit(0);
}

const commit = info.sourceCommit.substring(0, 12);
console.log(`Managed CLI Release Demo ${info.release} (${commit})`);
