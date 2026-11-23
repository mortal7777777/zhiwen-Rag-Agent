'use strict'

/** 更新逻辑纯函数自检：node scripts/check_updates.cjs */

const assert = require('assert')
const path = require('path')

const updates = require(path.join(__dirname, '..', 'lib', 'updates.cjs'))

const { parseVersion, compareVersions, findVersionInText, pickNewerRelease } = updates

assert.deepStrictEqual(parseVersion('v0.3.2'), { major: 0, minor: 3, patch: 2, pre: '' })
assert.deepStrictEqual(parseVersion('0.3.2-beta.1'), {
  major: 0,
  minor: 3,
  patch: 2,
  pre: 'beta.1',
})
assert.strictEqual(parseVersion('latest'), null)
assert.strictEqual(compareVersions('0.3.2', '0.3.2'), 0)
assert.strictEqual(compareVersions('v0.4.0', '0.3.9'), 1)
assert.strictEqual(compareVersions('0.3.2', '0.4.0'), -1)
assert.strictEqual(compareVersions('0.4.0-beta.1', '0.4.0'), -1)
assert.strictEqual(compareVersions('0.4.0', '0.4.0-beta.1'), 1)
assert.strictEqual(findVersionInText('知问 ZhiWen v0.5.1'), '0.5.1')
assert.strictEqual(findVersionInText('release-notes'), '')

const asset = (name) => ({ name })
const release = (tag, names, extra = {}) => ({
  tag_name: tag,
  name: tag,
  draft: false,
  prerelease: false,
  assets: names.map(asset),
  ...extra,
})

const releases = [
  release('v0.2.0', []),
  release('v0.3.2', ['zhiwen-setup-0.3.2.exe', 'latest.yml']),
  release('v0.4.0', ['zhiwen-setup-0.4.0.exe', 'latest.yml']),
  release('v0.5.0', ['zhiwen-setup-0.5.0.exe', 'latest.yml'], { draft: true }),
  release('v0.6.0', ['zhiwen-setup-0.6.0.exe', 'latest.yml'], { prerelease: true }),
  release('v0.7.0', ['source.zip']),
]

assert.strictEqual(pickNewerRelease(releases, '0.3.2').version, '0.4.0')
assert.strictEqual(pickNewerRelease(releases, '0.4.0'), null)
assert.strictEqual(pickNewerRelease(releases, '9.9.9'), null)
assert.strictEqual(pickNewerRelease([], '0.3.2'), null)
assert.strictEqual(pickNewerRelease(releases, 'not-a-version').version, '0.4.0')

console.log('check_updates: ok')
