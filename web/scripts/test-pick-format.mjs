// 赛程页预测方向格式与当日消息模板的单元测试：用 tsc 把两个文件编译到临时目录后用 node:test 运行。
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import assert from 'node:assert/strict'

const out = mkdtempSync(join(tmpdir(), 'pick-format-'))
execFileSync('node_modules/.bin/tsc', ['src/api/pickFormat.ts', 'src/api/dailyMessageTemplate.ts',
  '--outDir', out, '--module', 'es2022', '--target', 'es2022', '--skipLibCheck', '--ignoreConfig'], { stdio: 'inherit' })
const msgPath = join(out, 'dailyMessageTemplate.js')
writeFileSync(msgPath, readFileSync(msgPath, 'utf8').replace("from './pickFormat'", "from './pickFormat.js'"))
writeFileSync(join(out, 'package.json'), '{"type":"module"}')
const F = await import(pathToFileURL(join(out, 'pickFormat.js')).href)
const M = await import(pathToFileURL(msgPath).href)

test('亚盘：六208 葡萄牙 vs 智利，澳门临盘在库里是 2.0（主队让 2 球），下主队时让球方显示为 +', () => {
  assert.equal(F.formatPick({ market: 'ah', side: '主', line: 2.0, stake: 1 }), '主+2 x1')
  assert.equal(F.formatPick({ market: 'ah', side: '客', line: 2.0, stake: 1 }), '客-2 x1')
})
test('亚盘：主队受让（库里为负数）时，下客队显示为 +，下主队显示为 -', () => {
  assert.equal(F.formatPick({ market: 'ah', side: '客', line: -0.75, stake: 2 }), '客+0.75 x2')
  assert.equal(F.formatPick({ market: 'ah', side: '主', line: -0.25, stake: 1 }), '主-0.25 x1')
  assert.equal(F.formatPick({ market: 'ah', side: '主', line: 0.25, stake: 1 }), '主+0.25 x1')
  assert.equal(F.formatPick({ market: 'ah', side: '主', line: 0, stake: 1 }), '主0 x1')
})
test('不下注与没有预测显示 -', () => {
  assert.equal(F.formatPick({ market: 'ah', side: '不下注', line: 2.0 }), '-')
  assert.equal(F.formatPick(null), '-')
  assert.equal(F.formatMarketCell([], 'ou'), '-')
})
test('大小、欧盘、竞彩、竞彩让球的格式', () => {
  assert.equal(F.formatPick({ market: 'ou', side: '大', line: 2.25, stake: 2 }), '2.25大 x2')
  assert.equal(F.formatPick({ market: '1x2', side: '胜', stake: 3 }), '胜 x3')
  assert.equal(F.formatPick({ market: 'jc_had', side: '负平', stake: 1 }), '平负 x1')
  assert.equal(F.formatPick({ market: 'jc_hhad', side: '负', line: -1, stake: 2 }), '-1让负 x2')
  assert.equal(F.formatPick({ market: 'jc_hhad', side: '让胜', line_text: '+1', stake: 1 }), '+1让胜 x1')
})
test('当日消息：只列下注的场次，只复制未开赛时按开赛时刻过滤', () => {
  const ms = [
    { jcId: '六204', league: '日职', home: '横滨水手', away: '清水心跳', kickoffText: '2026-06-06 16:00',
      kickoffMs: 1000, picks: [{ market: 'ah', side: '主', line: 0.25, stake: 1, stake_estimated: true }] },
    { jcId: '六208', league: '友谊赛', home: '葡萄牙', away: '智利', kickoffText: '2026-06-07 01:45',
      kickoffMs: 3000, picks: [{ market: 'ah', side: '不下注', line: 2 }] },
  ]
  assert.equal(M.buildDailyMessage('2026-06-06', ms, { nowMs: 0 }),
    '【当日预测】竞彩日 2026-06-06\n- 六204 日职 横滨水手 vs 清水心跳 2026-06-06 16:00：主+0.25 x1')
  assert.equal(M.buildDailyMessage('2026-06-06', ms, { onlyNotStarted: true, nowMs: 2000 }), null)
})
