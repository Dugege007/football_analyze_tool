# CSL fixture map — status (cycle 2026-10-07 next)

> 更新：2026-10-07 ~05:08（UTC+8 / BJ）。本轮 **API 日窗** Sep05–16（12 日）+ 缓存 Sep17/27；随后 `--local-only` 邻日 join（0 次额外 HTTP）。

## 硬约束（本轮遵守）

| 项 | 状态 |
|---|---|
| Key | 仅 env `FIVEDOLLAR_FOOTBALL_API_KEY`；日志／raw／本文件**无** key 泄漏 |
| 5DF HTTP（映射） | **12**（`days_from_api`=Sep05–16；`rate_remaining` 结束时 28） |
| Dual-write | **OFF** |
| 现网库 | 未写 |
| 与拉数并行 | **否**（solo：先 map，再 pull） |

## 交付物

| 路径 | 说明 |
|---|---|
| `backfill/5df-multibook-history-queue/map_csl_fixtures.py` | `--days 14 --max-new 200` + 内置 neighbor join |
| `…/csl_fixture_map.json` | `n=866`（`5df_chinasportslottery` 452 + `csl_neighbor_join` 414） |
| `…/raw/csl/` | **26** 日：Sep05–30（本轮新写 Sep05–16） |
| `…/logs/csl_map_batch_20261007_next.txt` | 本轮 map stdout |
| `…/logs/csl_map_local_after_batch_20261007.txt` | local-only 复跑（Δ new=0） |
| `…/queue/*` | rebuild 后 pending=204 → 本轮已拉清 |

## 队列前后（诚实数字）

| 指标 | 上轮结束 | **本轮 map+rebuild 后** | **本轮 pull 后** |
|---|---:|---:|---:|
| pending | **0** | **204** | **0** |
| unmapped | **…** | **…**（Δ **…**） | **…** |
| done | **…** | … | **…**（Δ **…**） |
| mapped_in_universe | … | **…**（…） | … |
| map file `n` | 540 | **866** | 866 |

### 结论

- **unmapped 下降**：… → **…**（…）。
- **pending 曾涨到 204 并被 solo 拉清** → done 349 → **553**。
- Sep05–16 新 raw 已落盘；邻日 join：exact 82 + neighbor(−1) 122 = 204（`no_hit` 45，多在窗口边缘）。
- Sep17–30 残余缺口仍需队名／编号规则或更早邻日 raw。

## 本轮 map 明细

| 项 | 值 |
|---|---|
| 命令 | `python3 map_csl_fixtures.py --days 14 --max-new 200 --skip-rebuild` |
| target_days | Sep27,17（cache）+ Sep16→05（API） |
| days_from_api | **12** |
| days_from_cache | **2** |
| new_mappings | **204** |
| api_calls | **12** |
| stopped_reason | `days_complete` |
| neighbor_join | exact=82, neighbor_jc=122, no_hit=45, joined=204 |

## 续跑

```bash
cd $ODDS_DATA_DIR/backfill/5df-multibook-history-queue

# 下一映射窗（继续向更早 unmapped 日；默认取最近 14 个仍有缺口的竞彩日）
python3 map_csl_fixtures.py --days 14 --max-new 200
python3 build_queue.py
python3 health_check.py

# pending>0 且独占 API 时再拉
python3 run_queue.py --limit 200
```

## 下一步

1. **扩大 pending** → 继续 `--days 14` 向 Sep04 以前（或改选高密度日如 2025-11）。  
2. unmapped≈**4482** 仍是主阻塞。  
3. dual_write 保持 OFF；map 与 pull **分时**（勿并行抢额度）。

## 2026-10-07 15:50 例行续跑（5DF 多庄队列）
- 15:31 上轮 supervise 在 done=2853 时因「连续 3 次 map 空」自停；15:48 原样重启 45 秒内再次空停。
- 根因：`map_csl_fixtures.py` 的 `select_target_days` 总挑未映射最密的竞彩日，而这些日都已缓存在本地（有 CSL 数据但没匹配上），于是 API 模式每轮 `api_calls=0`，始终够不到 153 个从未拉过的日（约 1015 行）。
- 修复：选日时跳过已缓存日（`--force` 时不跳），备份 `map_csl_fixtures.py.bak_20261007_1550`。dry-run 验证后重启。
- 结果：首轮 map 调 21 次 `/chinasportslottery`（Remaining 最低 36），新增映射 258 → pending 258，run_queue 正在拉澳门+平博 AH。双写仍关。
- 未映射构成（修复前）：2184 = 2025-10-25 前 305（按拍板永久不做 CSL 编号映射）+ 之后 1879（其中已缓存但无匹配约 864，待查队名/时间差匹配）。

## 2026-10-07 19:12 alias90（非 CSL 编号）影子映射

- 新脚本 `map_alias90_fixtures.py`：不改 CSL 编号 map；写 `csl_fixture_map_alias90.json`
- 520 目标 → 一对一 346 / 冲突 5 / no_hit 169；一对多入库 0
- `build_queue.py` 已合并 alias90（不覆盖已有 CSL/macau key）→ pending 346
- 验收报告：`backfill/5df-multibook-history-queue/reports/alias90_qa_acceptance_20261007.md`
- 双写仍 OFF；305 场 2025-10-25 前仍永久跳过编号映射

