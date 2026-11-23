'use strict'

/** 版本号与 GitHub Release 解析（纯函数，scripts/check_updates.cjs 自检）。 */

function parseVersion(value) {
  const text = String(value || '').trim()
  const match = text.match(/^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/)
  if (!match) return null
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    pre: match[4] || '',
  }
}

/** 返回 1 / 0 / -1；无法解析按 0 处理（调用方应先用 parseVersion 过滤）。 */
function compareVersions(a, b) {
  const va = typeof a === 'string' ? parseVersion(a) : a
  const vb = typeof b === 'string' ? parseVersion(b) : b
  if (!va || !vb) return 0
  for (const key of ['major', 'minor', 'patch']) {
    if (va[key] !== vb[key]) return va[key] > vb[key] ? 1 : -1
  }
  if (va.pre === vb.pre) return 0
  // 正式版高于同号预发布版
  if (!va.pre) return 1
  if (!vb.pre) return -1
  return va.pre > vb.pre ? 1 : -1
}

/** 从 tag / 标题里取第一个 semver（兼容 "v0.3.3" 与 "知问 ZhiWen v0.3.3"）。 */
function findVersionInText(value) {
  const match = String(value || '').match(/v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)/)
  if (!match) return ''
  return parseVersion(match[1]) ? match[1] : ''
}

/** 只有带桌面安装包或 latest.yml 的 Release 才参与桌面端版本比较。 */
function isDesktopRelease(release) {
  const assets = Array.isArray(release && release.assets) ? release.assets : []
  return assets.some((asset) => {
    const name = String((asset && asset.name) || '')
    return /^zhiwen-setup-.+\.exe$/i.test(name) || /^latest\.ya?ml$/i.test(name)
  })
}

/**
 * 从 GitHub Releases 列表里挑出比 currentVersion 新的最新桌面版本。
 * 忽略 draft / prerelease 与没有桌面产物的 Release。
 */
function pickNewerRelease(releases, currentVersion) {
  const current = parseVersion(currentVersion)
  let best = null
  for (const release of Array.isArray(releases) ? releases : []) {
    if (!release || release.draft || release.prerelease) continue
    if (!isDesktopRelease(release)) continue
    const raw = findVersionInText(release.tag_name) || findVersionInText(release.name)
    const parsed = parseVersion(raw)
    if (!parsed) continue
    if (current && compareVersions(parsed, current) <= 0) continue
    if (!best || compareVersions(parsed, best.parsed) > 0) {
      best = { version: raw, parsed, release }
    }
  }
  return best
}

module.exports = {
  parseVersion,
  compareVersions,
  findVersionInText,
  isDesktopRelease,
  pickNewerRelease,
}
