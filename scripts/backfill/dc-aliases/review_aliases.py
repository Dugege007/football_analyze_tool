# -*- coding: utf-8 -*-
"""只读审查 research/dc_train/team_aliases_proposed.csv（2026-10-08）。
- 现网 app.db 只以 sqlite mode=ro 打开；不写任何库。
- 输出：conflicts.csv、review_stats.json（同目录）。
依赖：opencc-python-reimplemented（繁→简，仅用于比对；PYTHONPATH=/tmp/pylib）。
"""
# --- public repo: paths are env-overridable (see config.example.env) ---
import os as _rp_os
from pathlib import Path as _RpPath
_REPO_ROOT = _RpPath(__file__).resolve().parents[3]
def _rp_load_dotenv(path=_REPO_ROOT / '.env'):
    # 读仓库根 .env（KEY=VALUE；已存在的环境变量优先，不覆盖）；不打印任何值
    if _rp_os.environ.get('FAT_DISABLE_DOTENV') == '1':  # 测试时由 conftest 设置，避免本机 .env 干扰
        return
    try:
        for _ln in path.read_text(encoding='utf-8').splitlines():
            _ln = _ln.strip()
            if not _ln or _ln.startswith('#') or '=' not in _ln:
                continue
            _k, _v = _ln.split('=', 1)
            _k = _k.strip().removeprefix('export ').strip()
            _v = _v.strip().strip('"').strip("'")
            if _v.startswith('YOUR_'):  # config.example.env 占位值视为未填写
                continue
            if _k and _k not in _rp_os.environ:
                _rp_os.environ[_k] = _v
    except FileNotFoundError:
        pass
_rp_load_dotenv()
def _rp_env(name, default, base=None):
    v = _rp_os.environ.get(name, '').strip()
    p = _RpPath(v).expanduser() if v else default
    return p if p.is_absolute() else (base or _REPO_ROOT) / p
_MA_API_ROOT = _rp_env('MA_API_ROOT', _REPO_ROOT / 'api')
_ODDS_DATA_DIR = _rp_env('ODDS_DATA_DIR', _REPO_ROOT / 'data' / 'odds-data')
_APP_DB = _rp_env('APP_DB_PATH', _MA_API_ROOT / 'data' / 'app.db', _MA_API_ROOT)
_V2D3_DB = _rp_env('V2D3_DB_PATH', _MA_API_ROOT / 'data' / 'v2d3' / 'app.db', _MA_API_ROOT)
_BACKUP_DIR = _rp_env('BACKUP_DIR', _REPO_ROOT / 'backups' / 'football')
# --- end path config ---

import csv, gzip, json, re, sqlite3, collections, random, unicodedata, sys
from pathlib import Path
try:
    from opencc import OpenCC
    T2S = OpenCC("t2s").convert
except Exception:  # 没装就退化成不转换
    T2S = lambda s: s

HERE = Path(__file__).resolve().parent
DC = Path(str(_ODDS_DATA_DIR / "research/dc_train"))
DB = Path(str(_APP_DB))
PROP = DC / "team_aliases_proposed.csv"

def norm(s):
    s = T2S(unicodedata.normalize("NFKC", s)).lower()
    s = re.sub(r"[\s·\.\-_'’]", "", s)
    s = re.sub(r"(足球俱乐部|足球会|俱乐部|队)$", "", s)
    s = re.sub(r"^(fc|sc|cf|afc|ac)|(fc|sc|cf|afc|ac)$", "", s)
    return s

# ---- 人工复核结论（逐条看过 506 行后写死在这里，便于复跑）----
MANUAL = [
    # (category, severity, alias, team_canonical, league, detail, suggestion)
    ("alias_wrong_target", "blocker", "西悉尼漫步者", "悉尼", "澳超",
     "西悉尼漫步者=Western Sydney Wanderers，却挂到 Sydney FC（悉尼）名下；fixture_id_join n=1",
     "改挂 西悉尼流浪者"),
    ("alias_wrong_target", "blocker", "悉尼FC", "西悉尼流浪者", "澳超",
     "悉尼FC=Sydney FC，却挂到 Western Sydney Wanderers（西悉尼流浪者）名下；与上一条正好互换",
     "改挂 悉尼"),
    ("new_team_is_prod", "blocker", "冈山雉鸡", "冈山雉鸡", "日职联",
     "Fagiano Okayama；现网已有 teams.id=21 冈山绿雉（日职联）。导入会给同一队建第二个 id",
     "改为 add_alias 冈山雉鸡→21；team_map 规范名用 冈山绿雉"),
    ("new_team_is_prod", "blocker", "柏雷索尔", "柏雷索尔", "日职联",
     "Kashiwa Reysol；现网已有 teams.id=84 柏太阳神（日职联）",
     "改为 add_alias 柏雷索尔→84；team_map 规范名用 柏太阳神"),
    ("likely_duplicate_new", "fix", "Iwaki SC", "Iwaki SC", "日职乙",
     "5DF 换了 id：Iwaki SC(219049598) 只在 2024-25 出现 42 场，Iwaki FC(1972510351) 只在 2026 出现 9 场，从未同季；同一家いわきFC",
     "并成一队：规范名 磐城FC，Iwaki SC 作 alias；team_map 同步，否则 2026 只有 9 场判 team_n_lt_min"),
    ("missing_alias", "fix", "横滨FC", "FC横滨", "日职联",
     "竞彩名 横滨FC 共现证据 low 未采用，但就是 Yokohama FC（与现网 87 横滨水手=Marinos 不同队）",
     "补 alias 横滨FC→FC横滨（人工确认）"),
    ("missing_alias", "fix", "柏太阳神", "柏太阳神", "日职联",
     "现网名 柏太阳神 共现 low 未采用，与上面 柏雷索尔 同队",
     "随 柏雷索尔 合并处理"),
    ("canonical_wrong_name", "fix", "亚美尼亚比勒费尔德队", "亚美尼亚比勒费尔德队", "德乙",
     "Arminia≠亚美尼亚(Armenia)，且带「队」后缀",
     "规范名改 比勒费尔德（alias 已有），原名留作 alias"),
    ("canonical_typo", "fix", "爱嫒FC", "爱嫒FC", "日职乙",
     "Ehime 应为「爱媛」，「嫒」是错字", "规范名改 爱媛FC，错字留作 alias"),
    ("global_ambiguous", "fix", "国民", "国民", "葡超",
     "team_aliases.alias 全局唯一；「国民」= CD Nacional(马德拉)，乌拉圭国民等以后进来会撞", "规范名改 马德拉国民，「国民」不作全局 alias 或只挂这一队并记 note"),
    ("global_ambiguous", "fix", "阿赫利", "阿赫利", "沙特联",
     "Al Ahli Jeddah；埃及阿赫利（开罗）、卡塔尔阿赫利同名", "规范名改 吉达阿赫利"),
    ("global_ambiguous", "fix", "伊蒂哈德", "伊蒂哈德", "沙特联",
     "Al Ittihad Jeddah；阿联酋/埃及都有 Ittihad；alias 已有 吉达联合", "规范名改 吉达联合，伊蒂哈德作 alias"),
    ("global_ambiguous", "fix", "水原", "水原", "韩K联",
     "Suwon FC；水原三星同城不同队", "规范名改 水原FC（alias 已有），「水原」不建 alias"),
    ("global_ambiguous", "fix", "悉尼", "悉尼", "澳超",
     "Sydney FC；与 西悉尼流浪者 易混", "规范名改 悉尼FC（修正互换后）"),
    ("global_ambiguous", "info", "维多利亚", "维多利亚", "巴西甲",
     "EC Vitória；其他国家也有 Vitória/Victoria", "可改 巴伊亚维多利亚；不改也可（当前无撞名）"),
    ("global_ambiguous", "info", "首尔", "FC首尔", "韩K联",
     "add_alias；首尔衣恋(Seoul E-Land) 在 K2，以后进来会撞", "可接受，记 note"),
]

def kups_refs():
    """KuPS：teams 2（PAUN古比斯）/ 34（古比斯）在现网的引用（只读）。"""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out = {}
    for tid in (2, 34):
        ms = [r[0] for r in c.execute("select id from matches where home_team_id=? or away_team_id=?", (tid, tid))]
        ph = ",".join("?" * len(ms)) or "NULL"
        d = dict(team=c.execute("select name_zh_canonical from teams where id=?", (tid,)).fetchone()[0],
                 aliases=[r[0] for r in c.execute("select alias from team_aliases where team_id=?", (tid,))],
                 matches_home=c.execute("select count(*) from matches where home_team_id=?", (tid,)).fetchone()[0],
                 matches_away=c.execute("select count(*) from matches where away_team_id=?", (tid,)).fetchone()[0],
                 match_ids=ms)
        for t in ("results", "stats", "odds_asian", "odds_euro_home", "odds_jc_home", "odds_jc_hhad", "odds_raw",
                  "match_meta", "predictions", "prediction_legs", "strategy_validation_cache"):
            d[t] = c.execute(f"select count(*) from {t} where match_id in ({ph})", ms).fetchone()[0]
        d["predictions_by_strategy"] = dict(c.execute(
            f"select strategy, count(*) from predictions where match_id in ({ph}) group by 1", ms).fetchall())
        out[tid] = d
    c.close()
    return out


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    teams = dict(c.execute("select id, name_zh_canonical from teams"))
    aliases = {a: (t, s) for a, t, s in c.execute("select alias, team_id, source from team_aliases")}
    tl = collections.defaultdict(collections.Counter)
    for h, a, comp in c.execute("select home_team_id, away_team_id, competition_name from matches"):
        for t in (h, a):
            if t: tl[t][comp] += 1
    c.close()
    name2tid = {nm: tid for tid, nm in teams.items()}
    for a, (t, _) in aliases.items():
        name2tid.setdefault(a, t)
    nname = collections.defaultdict(set)
    for nm, tid in name2tid.items():
        nname[norm(nm)].add(tid)

    rows = list(csv.DictReader(open(PROP, encoding="utf-8")))
    cols = list(rows[0].keys())
    out = []
    def add(cat, sev, r, detail, sugg, prod_tid=""):
        out.append(dict(category=cat, severity=sev, action=r.get("action", ""), alias=r["alias"],
                        team_canonical=r["team_canonical"], team_id=r.get("team_id", ""),
                        league_name_zh=r["league_name_zh"], model_source_name=r.get("model_source_name", ""),
                        confidence=r.get("confidence", ""), evidence=r.get("evidence", ""),
                        prod_team_id=prod_tid, prod_team_name=teams.get(int(prod_tid), "") if str(prod_tid).isdigit() else "",
                        detail=detail, suggestion=sugg))

    # 1. 格式 / 必填
    fmt = dict(n_rows=len(rows), columns=cols, actions=collections.Counter(r["action"] for r in rows),
               empty_alias=sum(not r["alias"].strip() for r in rows),
               empty_canonical=sum(not r["team_canonical"].strip() for r in rows),
               add_alias_missing_tid=sum(1 for r in rows if r["action"] != "new_team" and not r["team_id"]),
               add_alias_bad_tid=[r["alias"] for r in rows if r["team_id"] and int(r["team_id"]) not in teams],
               add_alias_tid_name_mismatch=[r["alias"] for r in rows if r["team_id"] and teams.get(int(r["team_id"])) != r["team_canonical"]],
               leading_trailing_ws=[r["alias"] for r in rows if r["alias"] != r["alias"].strip() or r["team_canonical"] != r["team_canonical"].strip()],
               dup_rows=len(rows) - len({(r["action"], r["alias"], r["team_canonical"], r["league_name_zh"]) for r in rows}))
    nt = [r for r in rows if r["action"] == "new_team"]
    fmt["new_team_distinct_canonical"] = len({r["team_canonical"] for r in nt})
    fmt["new_team_distinct_alias"] = len({r["alias"] for r in nt})
    fmt["new_team_alias_ne_canonical"] = sum(r["alias"] != r["team_canonical"] for r in nt)
    fmt["confidence"] = collections.Counter(r["confidence"] for r in rows)

    # 2a. 同一 alias → 多个规范名
    a2c = collections.defaultdict(set)
    for r in rows:
        a2c[r["alias"]].add(r["team_canonical"])
    multi = {a: v for a, v in a2c.items() if len(v) > 1}
    for a, v in multi.items():
        for r in rows:
            if r["alias"] == a:
                add("alias_multi_target", "blocker", r, f"同一 alias 指向 {sorted(v)}", "只保留一个目标")
    # 2b. alias / 新规范名 与现网 alias / 队名撞（精确或规范化后）
    for r in rows:
        if r["action"] == "merge_teams":
            continue
        tgt = int(r["team_id"]) if r["team_id"] else None
        for nm in {r["alias"], r["team_canonical"]}:
            if nm in name2tid and name2tid[nm] != tgt:
                add("alias_vs_prod_exact", "blocker", r, f"「{nm}」现网已指向 team {name2tid[nm]}", "改挂或删除", name2tid[nm])
            else:
                hit = nname.get(norm(nm), set()) - ({tgt} if tgt else set())
                if hit:
                    add("alias_vs_prod_normalized", "check", r, f"「{nm}」规范化后={norm(nm)}，与现网 team {sorted(hit)} 相同", "人工确认是否同队", min(hit))
    # 2c. 新球队之间规范化后同名（不同写法建两队）
    ncan = collections.defaultdict(set)
    for r in nt:
        ncan[norm(r["team_canonical"])].add(r["team_canonical"])
    for k, v in ncan.items():
        if len(v) > 1:
            for r in nt:
                if r["team_canonical"] in v and r["alias"] == r["team_canonical"]:
                    add("new_team_dup_normalized", "fix", r, f"规范化后同名：{sorted(v)}", "并成一个规范名")
    # 2d. 同一规范名跨联赛：模型原名不同 → 人工确认是同一俱乐部（升降级）还是误合并
    can_lg = collections.defaultdict(set)
    for r in nt:
        can_lg[r["team_canonical"]].add((r["league_name_zh"], r["model_source_name"]))
    cross_ok = []
    for cn, v in sorted(can_lg.items()):
        if len({l for l, _ in v}) > 1:
            cross_ok.append((cn, sorted(v)))
    # 人工清单
    for cat, sev, al, cn, lg, det, sug in MANUAL:
        r = next((x for x in rows if x["alias"] == al and x["league_name_zh"] == lg), None) or \
            dict(action="(not_in_proposal)", alias=al, team_canonical=cn, team_id="", league_name_zh=lg)
        prod = {"冈山雉鸡": 21, "柏雷索尔": 84, "柏太阳神": 84}.get(al, "")
        add(cat, sev, r, det, sug, prod)
    # 规范名书写质量（繁体 / 空格 / 「队」后缀 / 中英混写）
    seen = set()
    for r in nt:
        cn = r["team_canonical"]
        if cn in seen: continue
        seen.add(cn)
        why = []
        if T2S(cn) != cn: why.append(f"繁体→{T2S(cn)}")
        if " " in cn: why.append("含空格")
        if cn.endswith("队"): why.append("带「队」后缀")
        if re.search(r"[A-Za-z]{3,}", cn) and re.search(r"[\u4e00-\u9fff]", cn): why.append("中英混写")
        elif not re.search(r"[\u4e00-\u9fff]", cn): why.append("纯拉丁名")
        if why and not any(o["alias"] == cn and o["category"].startswith("canonical") for o in out):
            add("canonical_style", "info", r, "；".join(why), "规范名用简体常用写法（2026-10-06 alias-merge 约定），原写法留作 alias")
    # 低证据（med 且 n=1 或 share<0.6）——已人工看过语义，列出备查
    for r in rows:
        m = re.search(r"n=(\d+)(?: share=([\d.]+))?", r["evidence"])
        if r["confidence"] == "med" and m and (int(m.group(1)) == 1 or (m.group(2) and float(m.group(2)) < 0.6)):
            if not any(o["alias"] == r["alias"] and o["severity"] in ("blocker", "fix") for o in out):
                add("low_evidence_checked_ok", "info", r, r["evidence"], "人工核对语义正确，可导")

    # 跨联赛同一俱乐部、team_map 规范名不一致（只影响 team_map / 以后入库，不影响本次 alias 导入）
    res = list(csv.DictReader(gzip.open(DC / "results.csv.gz", "rt", encoding="utf-8")))
    tmrows = list(csv.DictReader(open(DC / "team_map.csv", encoding="utf-8")))
    tm = {(r["league_name_zh"], r["source"], r["source_team_name"]): r["canonical_name"] for r in tmrows
          if r["source"] in ("football-data", "5df") and r["canonical_name"]}
    raw_by = collections.defaultdict(dict)
    for (lg, src, raw), cn in tm.items():
        raw_by[(src, raw)][lg] = cn
    FALSE = {"Yokohama FC"}
    for (src, raw), d in sorted(raw_by.items()):
        if len(set(d.values())) > 1 and raw not in FALSE:
            add("cross_league_canonical_mismatch", "info",
                dict(action="(team_map)", alias=raw, team_canonical=" | ".join(f"{l}:{c}" for l, c in sorted(d.items())),
                     team_id="", league_name_zh="/".join(sorted(d)), model_source_name=raw),
                "同一来源同一原名在不同联赛得到不同规范名（升降级队）", "team_map 按俱乐部统一用中文名")
    EXTRA = [("日职乙", "Shimizu S-Pulse", "日职联", "清水心跳"), ("日职乙", "Fagiano Okayama", "日职联", "冈山雉鸡"),
             ("日职乙", "Jubilo Iwata", "日职联", "Iwata"), ("日职乙", "Hokkaido Consadole Sapporo", "日职联", "Hokkaido Consadole Sapporo"),
             ("日职乙", "Sagan Tosu", "日职联", "Sagan Tosu"), ("日职乙", "Albirex Niigata", "日职联", "Albirex Niigata"),
             ("荷乙", "Telstar", "荷甲", "特士达"), ("荷乙", "Excelsior", "荷甲", "精英"), ("荷乙", "Almere City", "荷甲", "Almere City"),
             ("圣保罗锦", "Palmeiras", "巴西甲", "帕尔梅拉斯"), ("圣保罗锦", "Corinthians", "巴西甲", "科林蒂安"),
             ("圣保罗锦", "Santos", "巴西甲", "桑托斯"), ("圣保罗锦", "Sao Paulo", "巴西甲", "圣保罗"),
             ("圣保罗锦", "Bragantino", "巴西甲", "布拉干提罗"), ("圣保罗锦", "Mirassol", "巴西甲", "米拉索")]
    for lg1, raw1, lg2, cn2 in EXTRA:
        cn1 = next((v for (l, s, r_), v in tm.items() if l == lg1 and r_ == raw1), raw1)
        if cn1 != cn2:
            add("cross_league_canonical_mismatch", "info",
                dict(action="(team_map)", alias=raw1, team_canonical=f"{lg1}:{cn1} | {lg2}:{cn2}", team_id="",
                     league_name_zh=f"{lg1}/{lg2}", model_source_name=raw1),
                "跨来源（5DF↔FD）同一俱乐部规范名不一致", "team_map 统一")

    # 4. 解析：训练数据队名 → 规范名 → 现网 team（现网名/alias + 提议）
    tm_miss = collections.Counter()
    for r in res:
        for s in ("home", "away"):
            if (r["league_name_zh"], r["source"], r[s]) not in tm:
                tm_miss[(r["league_name_zh"], r["source"], r[s])] += 1
    prop_names = {r["alias"] for r in rows} | {r["team_canonical"] for r in rows}
    resolvable = set(name2tid) | prop_names
    model_keys = collections.Counter()
    model_keys_train = collections.Counter()
    for r in res:
        for s in ("home", "away"):
            k = (r["league_name_zh"], tm.get((r["league_name_zh"], r["source"], r[s]), r[s]))
            model_keys[k] += 1
            if r["train_include"] == "1" and r["status"] == "finished":
                model_keys_train[k] += 1
    unres = {k: n for k, n in model_keys.items() if k[1] not in resolvable}
    unres_train = {k: n for k, n in model_keys_train.items() if k[1] not in resolvable}
    by_lg = collections.Counter(k[0] for k in unres)
    total_slots = sum(model_keys.values())
    # 抽样 30 支新球队
    random.seed(20261008)
    new_canon = sorted({r["team_canonical"] for r in nt})
    sample = random.sample(new_canon, 30)
    samp = []
    canon_to_raw = collections.defaultdict(set)
    for (lg, src, raw), cn in tm.items():
        canon_to_raw[cn].add((lg, src, raw))
    raw_in_res = collections.Counter()
    for r in res:
        for s in ("home", "away"):
            raw_in_res[(r["league_name_zh"], r["source"], r[s])] += 1
    for cn in sample:
        raws = sorted(canon_to_raw.get(cn, []))
        n = sum(raw_in_res[x] for x in raws)
        lgp = sorted({r["league_name_zh"] for r in nt if r["team_canonical"] == cn})
        msn = sorted({r["model_source_name"] for r in nt if r["team_canonical"] == cn})
        samp.append(dict(canonical=cn, proposal_leagues=lgp, model_source_name=msn,
                         team_map_raw=[f"{l}/{s}/{r_}" for l, s, r_ in raws], rows_in_results=n,
                         ok=bool(raws) and n > 0 and set(msn) <= {r_ for _, _, r_ in raws}))
    stats_sample_ok = sum(x["ok"] for x in samp)

    stats = dict(format=fmt, alias_multi_target=sorted(multi), cross_league_same_canonical_new=cross_ok,
                 counts=collections.Counter((o["category"], o["severity"]) for o in out),
                 resolution=dict(results_rows=len(res), team_slots=total_slots,
                                 raw_not_in_team_map=sum(tm_miss.values()), raw_not_in_team_map_keys=len(tm_miss),
                                 model_teams=len(model_keys),
                                 model_teams_unresolved_to_db_name=len(unres), unresolved_slots=sum(unres.values()),
                                 unresolved_slots_train=sum(unres_train.values()), model_teams_train_unresolved=len(unres_train),
                                 unresolved_by_league=by_lg,
                                 unresolved_examples=sorted(unres)[:40]),
                 sample30=samp, sample30_ok=stats_sample_ok, kups=kups_refs())
    stats["counts"] = {f"{k[0]}|{k[1]}": v for k, v in stats["counts"].items()}
    fields = ["category", "severity", "action", "alias", "team_canonical", "team_id", "league_name_zh",
              "model_source_name", "confidence", "evidence", "prod_team_id", "prod_team_name", "detail", "suggestion"]
    order = {"blocker": 0, "fix": 1, "check": 2, "info": 3}
    out.sort(key=lambda o: (order[o["severity"]], o["category"], o["league_name_zh"], o["alias"]))
    with open(HERE / "conflicts.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(out)
    json.dump(stats, open(HERE / "review_stats.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=list)
    print(json.dumps({k: stats[k] for k in ("format", "counts", "alias_multi_target")}, ensure_ascii=False, indent=1, default=list))
    print(json.dumps({k: v for k, v in stats["resolution"].items() if k != "unresolved_examples"}, ensure_ascii=False, default=list))

if __name__ == "__main__":
    main()
