"""赛前盘口／水位折线：GET /matches/{match_id}/odds/timeline（API 0.3.23）。

契约：docs/schema/v2_0-prematch-odds-timeline-chart.md
- 点开才拉 5DollarFootballAPI /odds/history；默认 macauslot + asian。
- 禁止把 ticks 写入 odds_asian 等主表；短时内存缓存；忙则 503（有缓存可 200）。
- 横轴：tau_min = 距开赛剩余分钟；x = -log10(max(tau_min, 1))。
- 亚盘 line 统一为主队让球为正（对 5DollarFootballAPI 负号取反）。
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app import shared_api_yield as Y

TZ_CN = timezone(timedelta(hours=8))
API_BASE = os.environ.get("FIVEDOLLAR_API_BASE", "https://api.5dollarfootballapi.com/v1").rstrip("/")
DEFAULT_CACHE_TTL_SEC = int(os.environ.get("ODDS_TIMELINE_CACHE_TTL_SEC", "600"))
MAX_CACHE_TTL_SEC = 900
MAX_PAGES = int(os.environ.get("ODDS_TIMELINE_MAX_PAGES", "3"))
# 历史补数让路判断模块所在目录（仓库内 scripts/backfill/5df-multibook-history-queue/hist_yield.py）
HIST_YIELD_DIR = Path(__file__).resolve().parents[2] / "scripts" / "backfill" / "5df-multibook-history-queue"

BOOK_ALIASES = {
    "macau": "macauslot",
    "macauslot": "macauslot",
    "crown": "crown",
    "william": "williamhill",
    "williamhill": "williamhill",
    "pinnacle": "pinnacle",
    "bet365": "bet365",
    "jc": "chinasportslottery",
    "chinasportslottery": "chinasportslottery",
}
BOOK_LABELS = {
    "macauslot": "澳门",
    "crown": "皇冠",
    "williamhill": "威廉希尔",
    "pinnacle": "平博",
    "bet365": "Bet365",
    "chinasportslottery": "竞彩",
}
ALLOWED_MARKETS = frozenset({"asian", "1x2", "goalline"})

AXIS_TAU = (
    ("还剩 8h", 480),
    ("还剩 2h", 120),
    ("还剩 60m", 60),
    ("还剩 15m", 15),
    ("开赛", 0),
)

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # key -> (expires_epoch, payload)


class TimelineError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        err = {"code": self.code, "message": self.message, **self.extra}
        return {"error": err}


def _cn(dt: Optional[datetime] = None) -> datetime:
    n = dt or datetime.now(TZ_CN)
    if n.tzinfo is None:
        n = n.replace(tzinfo=TZ_CN)
    return n.astimezone(TZ_CN)


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    return dt.astimezone(TZ_CN)


def iso_cn(dt: datetime) -> str:
    return _cn(dt).isoformat()


def x_from_tau(tau_min: float) -> float:
    return -math.log10(max(float(tau_min), 1.0))


def axis_ticks() -> list[dict[str, Any]]:
    out = []
    for label, tau in AXIS_TAU:
        out.append({
            "label": label,
            "tau_min": tau,
            "x": round(x_from_tau(1.0 if tau == 0 else tau), 9),
        })
    out.sort(key=lambda a: a["x"])
    return out


def normalize_book(book: str | None) -> str:
    raw = (book or "macauslot").strip().lower()
    if raw not in BOOK_ALIASES:
        raise TimelineError(
            400, "bad_request",
            f"未知 book={book!r}；允许：macauslot／crown／williamhill／pinnacle／bet365（及 macau／william 别名）。",
            allowed_books=sorted(set(BOOK_ALIASES.values())),
        )
    return BOOK_ALIASES[raw]


def normalize_market(market: str | None) -> str:
    raw = (market or "asian").strip().lower()
    if raw not in ALLOWED_MARKETS:
        raise TimelineError(
            400, "bad_request",
            f"未知 market={market!r}；允许：asian／1x2／goalline。",
            allowed_markets=sorted(ALLOWED_MARKETS),
        )
    return raw


def hk_water(price: Any) -> float | None:
    if price is None:
        return None
    try:
        return round(float(price) - 1.0, 6)
    except (TypeError, ValueError):
        return None


def normalize_asian_line(line: Any) -> float | None:
    """5DollarFootballAPI：负数＝主队让球 → 本接口正数＝主队让球。"""
    if line is None:
        return None
    try:
        return round(-float(line), 6)
    except (TypeError, ValueError):
        return None


def display_for(market: str) -> dict[str, Any]:
    if market == "asian":
        return {
            "recommended_layout": "split_line_and_waters",
            "charts": [
                {"id": "line", "title": "盘口（主队视角）", "y_fields": ["line"]},
                {"id": "waters", "title": "主／客水位", "y_fields": ["home_water", "away_water"]},
            ],
        }
    if market == "goalline":
        return {
            "recommended_layout": "split_line_and_waters",
            "charts": [
                {"id": "line", "title": "大小球盘口", "y_fields": ["line"]},
                {"id": "waters", "title": "大／小水位", "y_fields": ["over_water", "under_water"]},
            ],
        }
    return {
        "recommended_layout": "single_1x2",
        "charts": [
            {"id": "1x2", "title": "胜平负赔率", "y_fields": ["home", "draw", "away"]},
        ],
    }


def cache_key(fixture_id: str | int, book: str, market: str) -> str:
    return f"timeline:{fixture_id}:{book}:{market}"


def cache_get(key: str) -> dict[str, Any] | None:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        exp, payload = hit
        if exp < now:
            _cache.pop(key, None)
            return None
        return json.loads(json.dumps(payload))  # copy


def cache_put(key: str, payload: dict[str, Any], ttl_sec: int) -> None:
    ttl = max(60, min(int(ttl_sec), MAX_CACHE_TTL_SEC))
    with _cache_lock:
        _cache[key] = (time.time() + ttl, json.loads(json.dumps(payload)))


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def _import_hist_yield():
    if str(HIST_YIELD_DIR) not in sys.path:
        sys.path.insert(0, str(HIST_YIELD_DIR))
    import hist_yield as hy  # type: ignore
    return hy


def check_live_busy(now: Optional[datetime] = None) -> tuple[str | None, int]:
    """返回 (paused_reason, retry_after_sec)。空闲则 (None, 0)。"""
    n = _cn(now)
    if Y.must_yield(n):
        retry = int(max(Y.seconds_until_clear(n), 30.0))
        w = Y.yield_window(n)
        reason = f"fixed_window_{w[0].strftime('%H%M')}_{w[1].strftime('%H%M')}" if w else "fixed_window"
        return reason, min(retry, 900)
    try:
        hy = _import_hist_yield()
        reason = hy.check_yield_reason(n, check_recent_write=True)
        if reason:
            retry = int(hy.suggest_sleep_seconds(reason, n))
            return reason, max(retry, 30)
    except Exception:
        # hist_yield 不可用时仍保留 shared 固定窗
        pass
    return None, 0


def resolve_fixture_id(
    conn,
    *,
    match_pk: int | None,
    match_uid: str | None,
    extras: dict | None,
    fixture_id_param: str | int | None,
) -> int:
    if fixture_id_param not in (None, ""):
        try:
            return int(fixture_id_param)
        except (TypeError, ValueError) as e:
            raise TimelineError(400, "bad_request", f"fixture_id 非法：{fixture_id_param!r}") from e

    ids = (extras or {}).get("ids") or {}
    if isinstance(ids, dict):
        for k in ("5df_fixture_id", "fixture_id", "5dollar_fixture_id"):
            if ids.get(k) not in (None, ""):
                try:
                    return int(ids[k])
                except (TypeError, ValueError):
                    pass

    if match_pk is not None:
        try:
            row = conn.execute(
                "SELECT fixture_id FROM odds_fetch_blob WHERE match_id = ? AND fixture_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (match_pk,),
            ).fetchone()
        except Exception:
            row = None
        if row and row["fixture_id"] not in (None, ""):
            try:
                return int(row["fixture_id"])
            except (TypeError, ValueError):
                pass
        try:
            row = conn.execute(
                "SELECT fixture_id FROM odds_fetch_queue WHERE match_id = ? AND fixture_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (match_pk,),
            ).fetchone()
        except Exception:
            row = None
        if row and row["fixture_id"] not in (None, ""):
            try:
                return int(row["fixture_id"])
            except (TypeError, ValueError):
                pass

    if match_uid and str(match_uid).startswith("probe:"):
        try:
            return int(str(match_uid).split(":", 1)[1])
        except (TypeError, ValueError):
            pass

    raise TimelineError(
        404, "fixture_unmapped",
        "库内无 5DollarFootballAPI 对阵编号映射；请先完成对阵映射，或传 fixture_id。",
    )


def resolve_kickoff(
    row,
    extras: dict | None,
) -> tuple[datetime, bool]:
    extras = extras or {}
    raw = None
    if row is not None:
        keys = row.keys()
        if "kickoff_at" in keys and row["kickoff_at"]:
            raw = row["kickoff_at"]
    if not raw:
        raw = extras.get("kickoff_at")
    kick = parse_dt(raw) if raw else None

    minute_known: bool | None = None
    if row is not None and "kickoff_minute_known" in row.keys() and row["kickoff_minute_known"] is not None:
        minute_known = bool(row["kickoff_minute_known"])
    elif extras.get("kickoff_minute_known") is not None:
        minute_known = bool(extras.get("kickoff_minute_known"))
    elif kick is not None:
        # 无列时：有带时区的完整开赛时刻即视为可用
        minute_known = True
    else:
        minute_known = False

    if kick is None or not minute_known:
        raise TimelineError(
            422, "kickoff_unknown",
            "无法得到可信开赛时刻，算不了距开赛剩余分钟；请补开赛分钟后再试。",
        )
    return kick, True


def _api_key() -> str:
    key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY") or os.environ.get("FIVEDOLLAR_API_KEY")
    if not key:
        raise TimelineError(
            503, "upstream_error",
            "本机未配置 FIVEDOLLAR_FOOTBALL_API_KEY，无法拉取变盘历史。",
        )
    return key


def fetch_history_pages(
    fixture_id: int,
    book: str,
    market: str,
    *,
    urlopen: Callable | None = None,
    gate_caller: str = "odds_timeline_chart",
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """返回 (raw_ticks, calls_used, last_meta)。受 MAX_PAGES 上限。"""
    if urlopen is None:
        urlopen = urllib.request.urlopen
    key = _api_key()
    gate = Y.Gate(gate_caller)
    ticks: list[dict[str, Any]] = []
    calls = 0
    last_meta: dict[str, Any] = {}
    page = 1
    while page <= MAX_PAGES:
        # 交互接口：不在让路窗内长睡；若进入让路窗则当作忙
        if Y.must_yield():
            raise TimelineError(
                503, "live_capture_busy",
                "今日实时采集需要额度，赛前折线暂不拉取 5DollarFootballAPI 变盘历史。请稍后重试。",
                paused_reason=f"fixed_window_entered_page={page}",
                retry_after_sec=int(max(Y.seconds_until_clear(), 30)),
                hint="可在实时采集空闲后再点开本场；短时缓存命中时仍可返回旧曲线。",
            )
        # 速率账本：若需等待则视为忙（不阻塞 HTTP）
        wait = gate._reserve_slot()
        if wait > 0.5:
            raise TimelineError(
                503, "live_capture_busy",
                "共享接口本分钟额度紧张，赛前折线暂不拉取。请稍后重试。",
                paused_reason=f"rate_slot_wait_sec={wait:.1f}",
                retry_after_sec=int(max(wait, 30)),
                hint="可在实时采集空闲后再点开本场。",
            )
        gate.stats["requests"] += 1
        q = urllib.parse.urlencode({
            "bookmaker": book,
            "market": market,
            "page": page,
            "per_page": 500,
        })
        url = f"{API_BASE}/fixtures/{fixture_id}/odds/history?{q}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
                headers = {k: v for k, v in getattr(resp, "headers", {}).items()} if hasattr(resp, "headers") else {}
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as e:
            calls += 1
            body = e.read().decode("utf-8", errors="replace")[:300]
            if e.code in (429, 403):
                raise TimelineError(
                    503, "rate_limit_exhausted",
                    "上游额度见底或被限流。",
                    retry_after_sec=61,
                    upstream_status=e.code,
                    upstream_body=body,
                ) from e
            raise TimelineError(
                502, "upstream_error",
                f"5DollarFootballAPI 失败 HTTP {e.code}。",
                upstream_status=e.code,
                upstream_body=body,
            ) from e
        except Exception as e:
            calls += 1
            raise TimelineError(502, "upstream_error", f"上游请求异常：{e}") from e

        calls += 1
        gate.after_response(headers)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise TimelineError(502, "upstream_error", "上游返回非 JSON。") from e
        if not payload.get("success", True) and "data" not in payload:
            raise TimelineError(502, "upstream_error", "上游 success=false。", upstream=payload)

        data = payload.get("data") or {}
        page_ticks = data.get("ticks") if isinstance(data, dict) else None
        if page_ticks is None and isinstance(payload.get("ticks"), list):
            page_ticks = payload["ticks"]
        if not isinstance(page_ticks, list):
            page_ticks = []
        ticks.extend(page_ticks)
        pag = payload.get("pagination") or {}
        last_meta = {"pagination": pag, "http_status": status}
        has_more = bool(pag.get("has_more"))
        if not has_more:
            break
        page += 1
    return ticks, calls, last_meta


def build_ticks(
    raw_ticks: list[dict[str, Any]],
    *,
    kickoff_at: datetime,
    market: str,
    as_of: datetime | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in raw_ticks:
        if t.get("suspended"):
            continue
        ra = parse_dt(t.get("recorded_at"))
        if ra is None:
            continue
        if ra >= kickoff_at:
            continue
        if as_of is not None and ra > as_of:
            continue
        tau = (kickoff_at - ra).total_seconds() / 60.0
        if tau <= 0:
            continue
        item: dict[str, Any] = {
            "recorded_at": iso_cn(ra),
            "tau_min": round(tau, 4),
            "x": round(x_from_tau(tau), 9),
            "line": None,
            "home_water": None,
            "away_water": None,
            "over_water": None,
            "under_water": None,
            "home": None,
            "draw": None,
            "away": None,
        }
        if market == "asian":
            line = normalize_asian_line(t.get("line"))
            if line is None and t.get("home") is None and t.get("away") is None:
                continue
            item["line"] = line
            item["home_water"] = hk_water(t.get("home"))
            item["away_water"] = hk_water(t.get("away"))
        elif market == "goalline":
            # goalline：line 不取反（大小球线）
            try:
                item["line"] = round(float(t["line"]), 6) if t.get("line") is not None else None
            except (TypeError, ValueError):
                item["line"] = None
            item["over_water"] = hk_water(t.get("over") if t.get("over") is not None else t.get("home"))
            item["under_water"] = hk_water(t.get("under") if t.get("under") is not None else t.get("away"))
        else:
            try:
                item["home"] = float(t["home"]) if t.get("home") is not None else None
                item["draw"] = float(t["draw"]) if t.get("draw") is not None else None
                item["away"] = float(t["away"]) if t.get("away") is not None else None
            except (TypeError, ValueError):
                continue
            if item["home"] is None and item["draw"] is None and item["away"] is None:
                continue
        out.append(item)
    out.sort(key=lambda x: x["recorded_at"])
    # 去重：同 recorded_at 保留最后
    dedup: dict[str, dict[str, Any]] = {}
    for it in out:
        dedup[it["recorded_at"]] = it
    return [dedup[k] for k in sorted(dedup.keys())]


def build_response(
    *,
    match_id: str | None,
    match_pk: int | None,
    fixture_id: int,
    kickoff_at: datetime,
    kickoff_minute_known: bool,
    book: str,
    market: str,
    as_of: datetime,
    ticks: list[dict[str, Any]],
    cached: bool,
    cache_expires_at: str | None,
    calls_used: int,
    pages_fetched: int,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    notes = list(notes or [])
    if not ticks:
        notes.append("无赛前变盘记录")
    return {
        "match_id": match_id,
        "match_pk": match_pk,
        "fixture_id": fixture_id,
        "kickoff_at": iso_cn(kickoff_at),
        "kickoff_minute_known": kickoff_minute_known,
        "book": book,
        "book_label": BOOK_LABELS.get(book, book),
        "market": market,
        "line_convention": "home_give_positive" if market == "asian" else None,
        "water_convention": "hk_from_decimal_minus_one",
        "as_of": iso_cn(as_of),
        "source": "5dollar_odds_history",
        "cached": cached,
        "cache_expires_at": cache_expires_at,
        "calls_used": calls_used,
        "pages_fetched": pages_fetched,
        "axis": {
            "x_transform": "neg_log10_tau_minutes",
            "x_formula": "x = -log10(max(tau_min, 1))",
            "tau_unit": "minutes_before_kickoff",
        },
        "axis_ticks": axis_ticks(),
        "ticks": ticks,
        "display": display_for(market),
        "notes": notes,
    }


def get_timeline(
    conn,
    *,
    match_id_path: str,
    row,
    extras: dict | None,
    book: str | None = None,
    market: str | None = None,
    fixture_id: str | int | None = None,
    as_of: str | None = None,
    cache_ttl_sec: int | None = None,
    force_refresh: bool = False,
    urlopen: Callable | None = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """主入口。成功返回响应 dict；失败抛 TimelineError。"""
    book_n = normalize_book(book)
    market_n = normalize_market(market)
    ttl = DEFAULT_CACHE_TTL_SEC if cache_ttl_sec is None else int(cache_ttl_sec)
    ttl = max(60, min(ttl, MAX_CACHE_TTL_SEC))
    as_of_dt = parse_dt(as_of) if as_of else _cn(now)
    if as_of_dt is None:
        raise TimelineError(400, "bad_request", f"as_of 非法：{as_of!r}")

    by_fixture_only = match_id_path == "by-fixture"
    match_pk = int(row["id"]) if row is not None else None
    match_uid = (row["match_uid"] if row is not None and "match_uid" in row.keys() else None)
    if by_fixture_only and fixture_id in (None, ""):
        raise TimelineError(400, "bad_request", "match_id=by-fixture 时必须传 fixture_id。")

    fid = resolve_fixture_id(
        conn,
        match_pk=None if by_fixture_only else match_pk,
        match_uid=None if by_fixture_only else match_uid,
        extras=None if by_fixture_only else extras,
        fixture_id_param=fixture_id,
    )
    if by_fixture_only:
        # 仅 fixture：仍尽量从库找 kickoff；找不到则 422
        try:
            blob = conn.execute(
                "SELECT match_id FROM odds_fetch_blob WHERE fixture_id = ? OR fixture_id = ? LIMIT 1",
                (str(fid), int(fid)),
            ).fetchone()
        except Exception:
            blob = None
        if blob:
            row = conn.execute("SELECT * FROM matches WHERE id = ?", (blob["match_id"],)).fetchone()
            match_pk = int(row["id"])
            match_uid = row["match_uid"]
            try:
                meta = conn.execute(
                    "SELECT extras_json FROM match_meta WHERE match_id = ?", (match_pk,)
                ).fetchone()
                if meta and meta["extras_json"]:
                    extras = json.loads(meta["extras_json"])
            except Exception:
                extras = extras or {}
        kick, minute_known = resolve_kickoff(row, extras)
    else:
        kick, minute_known = resolve_kickoff(row, extras)

    key = cache_key(fid, book_n, market_n)
    cached_payload = None if force_refresh else cache_get(key)

    # 忙闲以当前时刻为准（不用 as_of）
    busy_reason, retry_after = check_live_busy(now)

    if busy_reason and cached_payload is not None and not force_refresh:
        out = cached_payload
        out["cached"] = True
        notes = list(out.get("notes") or [])
        if "缓存返回；实时采集忙时未刷新" not in notes:
            notes.append("缓存返回；实时采集忙时未刷新")
        out["notes"] = notes
        # as_of 过滤在缓存副本上再裁一次
        out["ticks"] = [
            t for t in out["ticks"]
            if parse_dt(t["recorded_at"]) and parse_dt(t["recorded_at"]) <= as_of_dt
            and parse_dt(t["recorded_at"]) < kick
        ]
        out["as_of"] = iso_cn(as_of_dt)
        out["calls_used"] = 0
        return out

    if busy_reason:
        extra = {
            "paused_reason": busy_reason,
            "retry_after_sec": retry_after,
            "hint": "可在实时采集空闲后再点开本场；短时缓存命中时仍可返回旧曲线（若 cached=true 的 200）。",
        }
        if force_refresh and cache_get(key) is not None:
            extra["stale_available"] = True
        raise TimelineError(
            503, "live_capture_busy",
            "今日实时采集需要额度，赛前折线暂不拉取 5DollarFootballAPI 变盘历史。请稍后重试。",
            **extra,
        )

    if cached_payload is not None and not force_refresh:
        out = cached_payload
        out["cached"] = True
        out["calls_used"] = 0
        out["as_of"] = iso_cn(as_of_dt)
        out["ticks"] = [
            t for t in out["ticks"]
            if parse_dt(t["recorded_at"]) and parse_dt(t["recorded_at"]) <= as_of_dt
            and parse_dt(t["recorded_at"]) < kick
        ]
        return out

    raw_ticks, calls_used, meta = fetch_history_pages(fid, book_n, market_n, urlopen=urlopen)
    pages_fetched = calls_used
    built = build_ticks(raw_ticks, kickoff_at=kick, market=market_n, as_of=None)  # 缓存存全量赛前
    resp = build_response(
        match_id=match_uid,
        match_pk=match_pk,
        fixture_id=fid,
        kickoff_at=kick,
        kickoff_minute_known=minute_known,
        book=book_n,
        market=market_n,
        as_of=as_of_dt,
        ticks=built,
        cached=False,
        cache_expires_at=iso_cn(_cn(now) + timedelta(seconds=ttl)),
        calls_used=calls_used,
        pages_fetched=pages_fetched,
    )
    cache_put(key, {**resp, "ticks": built}, ttl)
    # 返回时按 as_of 裁剪
    resp["ticks"] = [
        t for t in built
        if parse_dt(t["recorded_at"]) and parse_dt(t["recorded_at"]) <= as_of_dt
    ]
    if not resp["ticks"] and "无赛前变盘记录" not in resp["notes"]:
        resp["notes"].append("无赛前变盘记录")
    return resp
