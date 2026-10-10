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
test('大小、欧盘、竞彩、竞彩让球的格式（表格列）', () => {
  assert.equal(F.formatPick({ market: 'ou', side: '大', line: 2.25, stake: 2 }), '2.25大 x2')
  assert.equal(F.formatPick({ market: '1x2', side: '胜', stake: 3 }), '(欧) 胜 x3')
  assert.equal(F.formatPick({ market: 'jc_had', side: '负平', stake: 1 }), '(竞) 平负 x1')
  assert.equal(F.formatPick({ market: 'jc_hhad', side: '负', line: -1, stake: 2 }), '-1让负 x2')
  assert.equal(F.formatPick({ market: 'jc_hhad', side: '让胜', line_text: '+1', stake: 1 }), '+1让胜 x1')
})

test('当日消息：用户确认的定稿格式（逐字比对）', () => {
  const ms = [
    {
      jcId: '六001',
      league: '英超',
      home: '曼城',
      away: '阿森纳',
      kickoffText: '2026-06-06 03:00',
      kickoffMs: 1000,
      picks: [{ market: 'ah', side: '主', line: 0.5, stake: 2 }],
    },
    {
      jcId: '六002',
      league: '西甲',
      home: '皇马',
      away: '巴萨',
      kickoffText: '2026-06-06 03:30',
      kickoffMs: 3000,
      picks: [
        { market: 'ah', side: '客', line: 1.25, stake: 1 },
        { market: 'ou', side: '大', line: 2.25, stake: 3 },
      ],
    },
    {
      jcId: '六003',
      league: '德甲',
      home: '拜仁',
      away: '多特',
      kickoffText: '2026-06-06 04:00',
      kickoffMs: 4000,
      picks: [{ market: 'ah', side: '不下注', line: 0.5 }],
    },
  ]
  const expected = [
    '- 六001 03:00 英超 曼城-阿森纳：',
    '  主+0.5  x2；',
    '- 六002 03:30 西甲 皇马-巴萨：',
    '  客-1.25  x1；',
    '  2.25大  x3；',
  ].join('\n')
  assert.equal(M.buildDailyMessage('2026-06-06', ms, { nowMs: 0 }), expected)
  // 不下注的场次不列；推算份数的星号不进消息；没有标题行
  assert.ok(!expected.includes('【'))
  assert.ok(!expected.includes('*'))
  assert.ok(!expected.includes('拜仁'))
  // 只复制未开赛：六001 已开赛（kickoffMs=1000 < nowMs=2000），只剩六002
  const pending = M.buildDailyMessage('2026-06-06', ms, { onlyNotStarted: true, nowMs: 2000 })
  assert.equal(
    pending,
    ['- 六002 03:30 西甲 皇马-巴萨：', '  客-1.25  x1；', '  2.25大  x3；'].join('\n'),
  )
  assert.equal(M.countBetMatches(ms), 2)
})

test('当日消息：正式库六204 写法（完整编号、时:分、主客用连字符、方向缩进）', () => {
  const ms = [
    {
      jcId: '六204',
      league: '日职联',
      home: '横滨水手',
      away: '清水心跳',
      kickoffText: '2026-06-06 16:00',
      kickoffMs: 1000,
      picks: [{ market: 'ah', side: '主', line: 0.25, stake: 1, stake_estimated: true }],
    },
  ]
  assert.equal(
    M.buildDailyMessage('2026-06-06', ms, { nowMs: 0 }),
    ['- 六204 16:00 日职联 横滨水手-清水心跳：', '  主+0.25  x1；'].join('\n'),
  )
})

test('当日消息：欧盘与竞彩胜平负带前缀，方向与 x 之间两个空格', () => {
  const ms = [
    {
      jcId: '六010',
      league: '英超',
      home: '切尔西',
      away: '利物浦',
      kickoffText: '2026-06-06 05:00',
      kickoffMs: 5000,
      picks: [
        { market: '1x2', side: '胜', stake: 3 },
        { market: 'jc_had', side: '负平', stake: 1 },
      ],
    },
  ]
  assert.equal(
    M.buildDailyMessage('2026-06-06', ms, { nowMs: 0 }),
    ['- 六010 05:00 英超 切尔西-利物浦：', '  (欧) 胜  x3；', '  (竞) 平负  x1；'].join('\n'),
  )
})
