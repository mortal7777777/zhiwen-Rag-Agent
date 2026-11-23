/** 行级 diff：返回 {type: 'same'|'add'|'del', sign, text} 序列。 */

export function diffLines(oldText, newText) {
  const oldLines = String(oldText || '').split('\n')
  const newLines = String(newText || '').split('\n')
  const n = oldLines.length
  const m = newLines.length
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] =
        oldLines[i] === newLines[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (oldLines[i] === newLines[j]) {
      out.push({ type: 'same', sign: ' ', text: oldLines[i] })
      i += 1
      j += 1
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', sign: '−', text: oldLines[i] })
      i += 1
    } else {
      out.push({ type: 'add', sign: '+', text: newLines[j] })
      j += 1
    }
  }
  while (i < n) {
    out.push({ type: 'del', sign: '−', text: oldLines[i] })
    i += 1
  }
  while (j < m) {
    out.push({ type: 'add', sign: '+', text: newLines[j] })
    j += 1
  }
  return out
}
