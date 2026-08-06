"""가격 데이터 수집.

기초자산이 50건을 넘어가므로 개별 요청 대신 묶음 다운로드를 쓴다.
받아온 건 디스크에 캐시하고, 실패하면 오래된 캐시라도 쓴다.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import logging
import re
import os
import time
import warnings

import pandas as pd

from .indicators import enrich

warnings.filterwarnings("ignore")

# yfinance 는 실패한 티커마다 로그를 쏟아낸다. 결과는 우리가 따로 요약한다.
for _name in ("yfinance", "urllib3", "peewee"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)
logging.getLogger("yfinance").propagate = False


@contextlib.contextmanager
def _quiet():
    """yfinance 가 stdout/stderr 로 직접 찍는 것까지 막는다."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield
    finally:
        pass

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "state", "cache")
COLS = ["open", "high", "low", "close", "volume"]
BATCH = 40
MIN_ROWS = 30


def _cache_path(ticker: str) -> str:
    safe = ticker.replace("^", "_idx_").replace(".", "_")
    return os.path.join(CACHE_DIR, f"{safe}.csv")


def _read_cache(ticker: str, max_age_hours: float) -> pd.DataFrame | None:
    p = _cache_path(ticker)
    if not os.path.exists(p):
        return None
    if (dt.datetime.now().timestamp() - os.path.getmtime(p)) / 3600 > max_age_hours:
        return None
    try:
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        return df if len(df) >= MIN_ROWS else None
    except Exception:
        return None


def _write_cache(ticker: str, df: pd.DataFrame) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        df[COLS].to_csv(_cache_path(ticker))
    except Exception:
        pass


def _normalize(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(-1)
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={"adj close": "close", "시가": "open", "고가": "high",
                            "저가": "low", "종가": "close", "거래량": "volume"})
    df = df.loc[:, ~df.columns.duplicated()]
    for c in COLS:
        if c not in df.columns:
            if c == "volume":
                df[c] = 0.0
            else:
                return None
    df = df[COLS].apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
    if not len(df):
        return None
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _fetch_batch(tickers: list[str], years: int) -> None:
    """묶음으로 받아 캐시에 저장한다. 실패한 티커는 조용히 건너뛴다."""
    try:
        import yfinance as yf
    except ImportError:
        return
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i + BATCH]
        try:
            with _quiet():
                raw = yf.download(chunk, period=f"{years}y", auto_adjust=True,
                                  group_by="ticker", progress=False, threads=True)
        except Exception:
            continue
        if raw is None or not len(raw):
            continue
        for t in chunk:
            try:
                sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except (KeyError, IndexError):
                continue
            df = _normalize(sub)
            if df is not None and len(df) >= MIN_ROWS:
                _write_cache(t, df)
        if i + BATCH < len(tickers):
            time.sleep(1.0)


def _fetch_one(ticker: str, years: int) -> pd.DataFrame | None:
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        with _quiet():
            raw = yf.Ticker(ticker).history(period=f"{years}y", auto_adjust=True)
        return _normalize(raw)
    except Exception:
        return None


def _fetch_pykrx(ticker: str, years: int) -> pd.DataFrame | None:
    code = ticker.split(".")[0]
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        return None
    try:
        from pykrx import stock
    except Exception:
        return None
    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=int(years * 372))
        return _normalize(stock.get_market_ohlcv(start.strftime("%Y%m%d"),
                                                 end.strftime("%Y%m%d"), code))
    except Exception:
        return None


def load(ticker: str, years: int = 3, max_age_hours: float = 6.0) -> pd.DataFrame | None:
    """지표까지 붙은 프레임. 실패하면 None."""
    df = _read_cache(ticker, max_age_hours)
    if df is None:
        df = _fetch_one(ticker, years)
        if (df is None or len(df) < MIN_ROWS) and ticker.endswith((".KS", ".KQ")):
            df = _fetch_pykrx(ticker, years)
        if df is not None and len(df) >= MIN_ROWS:
            _write_cache(ticker, df)
        else:
            df = _read_cache(ticker, max_age_hours=24 * 14)   # 오래된 캐시라도
            if df is None:
                return None
    return enrich(df)


def load_many(tickers: list[str], years: int = 3,
              max_age_hours: float = 6.0) -> dict[str, pd.DataFrame]:
    tickers = list(dict.fromkeys(tickers))
    need = [t for t in tickers if _read_cache(t, max_age_hours) is None]
    if need:
        _fetch_batch(need, years)

    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        cached = _read_cache(t, max_age_hours)
        if cached is not None:
            out[t] = enrich(cached)
            continue
        df = load(t, years=years, max_age_hours=max_age_hours)   # 개별 재시도
        if df is not None:
            out[t] = df
    return out
