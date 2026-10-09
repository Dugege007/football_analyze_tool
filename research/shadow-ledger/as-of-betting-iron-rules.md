# 模拟下注铁律（用户 2026-10-07 定稿）

须写入记忆、代码校验，并让后端／前端／算法顾问共知。  
2026-10-08：盘口术语对齐用户定稿（[`../../schema/v2_0-odds-phase-terminology.md`](../../schema/v2_0-odds-phase-terminology.md)）——初盘=`open`＝各家首次开盘；中盘／临盘默认=`mid`/`close`（规则）；开赛 ≥23:00 例外保留：规则＝当天 15:00／22:00，真实＝`mid_real`/`close_real`（对照）；其他时刻「即时（抓取时间）」/`live`；竞彩日 11:10＝即时（11:10）；「用临盘代替即时」作废。

1. **钉在下注时刻**：模拟时当作自己就在那个时间点下注；本场赛果及之后任意赛果，禁止进入当时的特征、权重、参数或方向。
2. **长跑有效性**：从半年／一年／两年或若干月前起，一路往今模拟；方案须从头至尾表现稳健才算有效。
3. **权重也钉在当时**：越早的场参考价值越低，用滚动衰减；评某一天时，只按该日 cutoff 往回的远近**当场重算**权重，禁止用「今天」视角给整段历史一次性定死权重矩阵。
4. **已产出预测冻结**：在既定决策阶段（中盘／临盘／「即时（抓取时间）」等，指纹写明）算出的方向当快照保留；赛果出来后只用于结算与后续场，不得回头改旧场方向（扩库用 splice，禁止整表重算覆盖）。不得把「临盘」当作任意即时盘的代称。
5. **新衰减须新码**：新 weight_profile／strategy_code；影子验证达标后再谈晋升；现网 V3 主列另拍板。
6. **竞彩日早场特殊带（口径已定，代码待改）**：连续窗里「前一日序号、次日清晨／午前开赛」原用 `0<=h<=10` → `cutoff_hour=21`。用户 2026-10-07：须拓宽覆盖至约 **11:30**（有分钟用 `[00:00,11:30]`；仅整点暂 `h<=11`）。**只改特殊带宽度，不改晚场 `h-4`**。改码后影子重跑；禁止用新 cutoff 整表重写已冻结现网 V3。详见 [`acceptance-leakage-checklist.md`](./acceptance-leakage-checklist.md) §3、[`../schema/v2_0-jingcai-day-early-kickoff.md`](../../schema/v2_0-jingcai-day-early-kickoff.md)。

对照实现：`analysis_window_cutoff`、`time_decay_weight_utils`、影子包 `shadow-walkforward-twdecay-v1`。  
验收清单：[`acceptance-leakage-checklist.md`](./acceptance-leakage-checklist.md)。
