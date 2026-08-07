"""실적 발표일과 거시 일정.

이 도구는 가격 추세만 본다. 실적 발표는 그 추세를 하루아침에 끊는데,
갭으로 움직이기 때문에 손절가에 팔 수도 없다. 2배 상품이 하루에
-40% 가 되는 건 대부분 이 경우다.

그래서 예측은 하지 않고 회피만 한다.
  · 실적 며칠 전이면 신규 진입을 막는다
  · 보유 중 실적이 다가오면 알려준다. 들고 갈지는 사용자가 정한다
"""

from __future__ import annotations

import datetime as dt
import json
import os
import warnings

warnings.filterwarnings("ignore")

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "state", "cache", "earnings.json")


def _load_cache() -> dict:
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(c: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(c, f)
    except Exception:
        pass


def _fetch_earnings(ticker: str) -> str | None:
    """다음 실적 발표일. 못 받으면 None."""
    try:
        import yfinance as yf
    except ImportError:
        return None

    today = dt.date.today()

    try:
        cal = yf.Ticker(ticker).calendar
        raw = None
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date")
        if raw is not None:
            dates = raw if isinstance(raw, (list, tuple)) else [raw]
            for d in dates:
                d = getattr(d, "date", lambda: d)()
                if isinstance(d, dt.date) and d >= today:
                    return d.isoformat()
    except Exception:
        pass

    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=12)
        if df is not None and len(df):
            for idx in df.index:
                d = idx.date()
                if d >= today:
                    return d.isoformat()
    except Exception:
        pass
    return None


# ── 국내 실적: 추정 ─────────────────────────────────────────
#  한국은 미국과 달리 실적 발표일을 미리 공지하지 않는다.
#  삼성전자·SK하이닉스는 분기 종료 후 영업일 5일 안팎에 잠정실적을
#  내는 패턴이 수년째 유지되고 있어, 그 패턴으로 구간을 잡는다.
#  정확한 날짜가 아니므로 '추정' 으로 표시하고 구간을 넉넉히 둔다.

KR_TICKERS = {"005930.KS", "000660.KS"}
QUARTER_ENDS = [(3, 31), (6, 30), (9, 30), (12, 31)]


def _add_business_days(d: dt.date, n: int) -> dt.date:
    added = 0
    while added < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def korean_earnings(today: dt.date | None = None) -> tuple[str, str]:
    """다음 잠정실적 추정일과 그 구간의 끝.

    분기 종료 후 영업일 +4 를 중심으로 보고, +10 까지를 여유 구간으로 둔다.
    설·추석이 끼면 며칠 밀리기 때문이다.
    """
    today = today or dt.date.today()
    for year in (today.year, today.year + 1):
        for m, d in QUARTER_ENDS:
            qe = dt.date(year, m, d)
            start = _add_business_days(qe, 4)
            end = _add_business_days(qe, 10)
            if today <= end:
                return start.isoformat(), end.isoformat()
    return "", ""


def next_earnings(ticker: str, max_age_days: int = 3) -> str | None:
    """캐시를 거쳐 다음 실적일을 반환한다. 실적일은 자주 안 바뀐다."""
    cache = _load_cache()
    rec = cache.get(ticker)
    today = dt.date.today()

    if rec:
        try:
            age = (today - dt.date.fromisoformat(rec["fetched"])).days
            stale_date = (rec.get("date") and
                          dt.date.fromisoformat(rec["date"]) < today)
            if age <= max_age_days and not stale_date:
                return rec.get("date")
        except Exception:
            pass

    d = _fetch_earnings(ticker)
    cache[ticker] = {"date": d, "fetched": today.isoformat()}
    _save_cache(cache)
    return d


def days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (dt.date.fromisoformat(iso) - dt.date.today()).days
    except Exception:
        return None


def next_macro(cfg: dict) -> tuple[str | None, str | None, int | None]:
    """설정에 적힌 거시 일정 중 가장 가까운 것."""
    cal = macro_rows(cfg)
    today = dt.date.today()
    best = None
    for row in cal:
        try:
            d = dt.date.fromisoformat(str(row.get("date")))
        except Exception:
            continue
        if d >= today and (best is None or d < best[0]):
            best = (d, row.get("name", "거시 일정"))
    if best is None:
        return None, None, None
    return best[0].isoformat(), best[1], (best[0] - today).days


def check(v, cfg: dict) -> dict:
    """진입을 막아야 하는 일정이 있는지 본다.

    반환: {"block": bool, "reason": str, "earnings": iso, "d_earnings": int, ...}
    """
    ev = cfg.get("events") or {}
    out: dict = {"block": False, "reason": "", "earnings": None,
                 "d_earnings": None, "macro": None, "d_macro": None,
                 "estimated": False}

    if not ev.get("enabled", True):
        return out

    if ev.get("use_earnings", True):
        if v.id in KR_TICKERS and ev.get("estimate_korean", True):
            start, end = korean_earnings()
            out["earnings"], out["estimated"] = start, True
            d, d_end = days_until(start), days_until(end)
            out["d_earnings"] = d
            # 추정이라 구간 전체를 막는다. 시작 전 며칠도 포함.
            block_d = int(ev.get("block_days_before_earnings", 5))
            if d is not None and d_end is not None and -99 < d <= block_d and d_end >= 0:
                out["block"] = True
                out["reason"] = (f"실적 발표 구간 (추정 {start}~{end}) — 진입 보류"
                                 if d > 0 else
                                 f"실적 발표 구간 (추정, ~{end}) — 진입 보류")
        else:
            iso = next_earnings(v.id)
            d = days_until(iso)
            out["earnings"], out["d_earnings"] = iso, d
            block_d = int(ev.get("block_days_before_earnings", 5))
            if d is not None and 0 <= d <= block_d:
                out["block"] = True
                out["reason"] = (f"{d}일 뒤 실적 발표 — 갭 위험으로 진입 보류"
                                 if d else "오늘 실적 발표 — 진입 보류")

    if ev.get("use_macro", True) and not out["block"]:
        iso, name, d = next_macro(cfg)
        out["macro"], out["d_macro"] = name, d
        block_m = int(ev.get("block_days_before_macro", 1))
        if d is not None and 0 <= d <= block_m:
            out["block"] = True
            out["reason"] = f"{name} {'당일' if d == 0 else f'{d}일 전'} — 진입 보류"

    return out


# ── FOMC 일정 자동 수집 ─────────────────────────────────────
#  연준은 1년 이상 앞서 일정을 공표한다. 공식 페이지에서 받아오되,
#  파싱이 깨지면 config.yaml 에 적힌 목록으로 그대로 돌아간다.
#  화면 구조가 바뀌어도 시스템이 멈추지 않게 하려는 것이다.

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
CAL_CACHE = os.path.join(os.path.dirname(CACHE), "fomc.json")
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def parse_fomc(html: str) -> list[dict]:
    """연준 일정 페이지에서 (연도, 월, 발표일) 을 뽑는다.

    회의는 이틀이고 금리 결정은 이튿날 발표된다. '27-28' 같은 표기에서
    뒤 숫자를 쓴다. 월을 넘기는 '29-30 / 1' 형태도 처리한다.
    """
    import re
    out: list[dict] = []
    for block in re.split(r'(?=<div[^>]*panel[^>]*>)', html):
        ym = re.search(r'(20\d{2})\s*FOMC\s*Meetings', block, re.I)
        if not ym:
            continue
        year = int(ym.group(1))
        months = re.findall(
            r'fomc-meeting__month[^>]*>\s*(?:<[^>]+>\s*)*([A-Z][a-z]+)', block)
        days = re.findall(
            r'fomc-meeting__date[^>]*>\s*(?:<[^>]+>\s*)*([0-9]{1,2}(?:\s*-\s*[0-9]{1,2})?)',
            block)
        for mon, day in zip(months, days):
            m = MONTHS.get(mon.split("/")[0].strip())
            if not m:
                continue
            last = day.split("-")[-1].strip()
            if not last.isdigit():
                continue
            d = int(last)
            mm = m
            if "-" in day:
                first = int(day.split("-")[0].strip())
                if d < first:          # 월을 넘긴 회의
                    mm = m % 12 + 1
                    if mm == 1:
                        year += 1
            try:
                out.append({"date": dt.date(year, mm, d).isoformat(), "name": "FOMC"})
            except ValueError:
                continue
    return out


def refresh_fomc(timeout: int = 20) -> list[dict]:
    """연준 페이지에서 일정을 받아 캐시에 저장한다. 실패하면 빈 목록."""
    import urllib.request

    try:
        req = urllib.request.Request(
            FOMC_URL, headers={"User-Agent": "Mozilla/5.0 (leverage-watch)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "ignore")
        rows = parse_fomc(html)
    except Exception as exc:
        print(f"  FOMC 일정 수집 실패 ({exc}). 설정 파일 목록을 씁니다.")
        return []

    if len(rows) < 4:
        print("  FOMC 일정을 제대로 못 읽었습니다. 설정 파일 목록을 씁니다.")
        return []

    try:
        os.makedirs(os.path.dirname(CAL_CACHE), exist_ok=True)
        with open(CAL_CACHE, "w", encoding="utf-8") as f:
            json.dump({"fetched": dt.date.today().isoformat(), "rows": rows}, f)
    except Exception:
        pass
    print(f"  FOMC 일정 {len(rows)}건 수집")
    return rows


def _cached_fomc(max_age_days: int = 14) -> list[dict]:
    try:
        with open(CAL_CACHE, encoding="utf-8") as f:
            c = json.load(f)
        if (dt.date.today() - dt.date.fromisoformat(c["fetched"])).days <= max_age_days:
            return c.get("rows", [])
    except Exception:
        pass
    return []


def macro_rows(cfg: dict) -> list[dict]:
    """설정 목록 + 자동 수집분을 합친다. 날짜 기준 중복 제거."""
    rows = list((cfg.get("events") or {}).get("macro_calendar") or [])
    if (cfg.get("events") or {}).get("auto_fomc", True):
        fetched = _cached_fomc() or refresh_fomc()
        rows += fetched
    seen, out = set(), []
    for r in rows:
        d = str(r.get("date"))
        if d in seen:
            continue
        seen.add(d)
        out.append(r)
    return out
