#!/usr/bin/env node
// 许可检查：禁止引入商业/非商业许可的表格组件（只允许 MIT 的 ag-grid-community 等）。
// 用法：npm run check:licenses（发现禁用包时退出码 1）
import { readFileSync, existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

/** 禁用包：名字精确匹配，或以 scope 前缀匹配 */
const FORBIDDEN = [
  { match: (n) => n === 'ag-grid-enterprise', why: 'AG Grid 企业版（商业 EULA）' },
  { match: (n) => n.startsWith('@ag-grid-enterprise/'), why: 'AG Grid 企业版模块（商业 EULA）' },
  { match: (n) => n === 'ag-charts-enterprise', why: 'AG Charts 企业版（商业 EULA）' },
  { match: (n) => n.startsWith('@univerjs-pro/'), why: 'Univer Pro（商业许可）' },
  { match: (n) => n === 'handsontable' || n.startsWith('@handsontable/'), why: 'Handsontable（仅非商业免费）' },
]

const problems = []
function check(name, where) {
  for (const f of FORBIDDEN) {
    if (f.match(name)) problems.push(`${name} —— ${f.why}（出现在 ${where}）`)
  }
}

const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'))
for (const field of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
  for (const name of Object.keys(pkg[field] ?? {})) check(name, `package.json ${field}`)
}

const lockPath = join(root, 'package-lock.json')
if (existsSync(lockPath)) {
  const lock = JSON.parse(readFileSync(lockPath, 'utf8'))
  for (const key of Object.keys(lock.packages ?? {})) {
    if (!key) continue
    const name = key.slice(key.lastIndexOf('node_modules/') + 'node_modules/'.length)
    check(name, `package-lock.json ${key}`)
  }
  for (const name of Object.keys(lock.dependencies ?? {})) check(name, 'package-lock.json dependencies')
} else {
  console.warn('提示：未找到 package-lock.json，只检查了 package.json')
}

if (problems.length) {
  console.error('许可检查失败，发现禁用包：')
  for (const p of [...new Set(problems)]) console.error('  - ' + p)
  process.exit(1)
}
console.log('许可检查通过：未发现 ag-grid-enterprise / @univerjs-pro / handsontable 等禁用包')
