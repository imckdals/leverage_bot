"""가격 시계열에서 판단에 쓰는 지표만 계산한다."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(100.0).where(roll_down.notna(), np.nan)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    ema_f = close.ewm(span=fast, adjust=False, min_periods=slow).mean()
    ema_s = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    line = ema_f - ema_s
    signal = line.ewm(span=sig, adjust=False, min_periods=slow + sig).mean()
    return line, signal, line - signal


def atr(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Wilder ATR. df 는 high/low/close 컬럼을 가진다."""
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def realized_vol(close: pd.Series, n: int = 20) -> pd.Series:
    """연율화 실현변동성."""
    r = np.log(close / close.shift(1))
    return r.rolling(n, min_periods=n).std() * np.sqrt(252)


def drawdown_from_high(close: pd.Series, n: int = 252) -> pd.Series:
    peak = close.rolling(n, min_periods=20).max()
    return close / peak - 1.0


# ── 직진성: 레버리지에서 가장 중요한 부분 ────────────────────
#  기초자산이 같은 폭을 움직여도, 한 방향으로 쭉 간 경우와
#  오르내리며 도달한 경우의 레버리지 수익률은 완전히 다르다.

def efficiency_ratio(close: pd.Series, n: int = 60) -> pd.Series:
    """카우프만 효율성 비율. 순이동 ÷ 실제 이동거리.

    1.0 에 가까우면 직선에 가까운 추세, 0 에 가까우면 제자리 등락.
    """
    net = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n, min_periods=n).sum()
    return (net / path.replace(0.0, np.nan)).clip(0.0, 1.0)


def trend_fit(close: pd.Series, n: int = 60):
    """로그가격을 직선으로 회귀했을 때의 R² 와 하루당 기울기.

    R² 가 높다 = 경로가 매끄럽다. 기울기 = 추세의 속도.
    """
    logc = np.log(close)
    x = np.arange(n, dtype=float)
    x_c = x - x.mean()
    denom = (x_c ** 2).sum()

    def _slope(w):
        return float((x_c * (w - w.mean())).sum() / denom)

    def _r2(w):
        b = (x_c * (w - w.mean())).sum() / denom
        ss_res = float((((w - w.mean()) - b * x_c) ** 2).sum())
        ss_tot = float(((w - w.mean()) ** 2).sum())
        return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    slope = logc.rolling(n, min_periods=n).apply(_slope, raw=True)
    r2 = logc.rolling(n, min_periods=n).apply(_r2, raw=True)
    return r2, slope


def decay_estimate(sigma: float, leverage: int, days: int = 20) -> float:
    """일간 리밸런싱 상품이 기초자산 제자리일 때 잃는 몫 (로그 근사).

        drag ≈ ½ · L · (L−1) · σ² · T

    3배는 2배의 3배, 인버스2X 는 정방향 2배의 3배로 감가가 붙는다.
    """
    if sigma is None or np.isnan(sigma):
        return float("nan")
    L = float(leverage)
    return 0.5 * L * (L - 1.0) * (sigma ** 2) * (days / 252.0)


def slope_ok(series: pd.Series, lookback: int = 20) -> pd.Series:
    """lookback 일 전보다 높은지 (이동평균 기울기 판정용)."""
    return series > series.shift(lookback)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV 프레임에 지표 컬럼을 붙여 반환한다."""
    out = df.copy()
    c = out["close"]
    out["ma20"] = sma(c, 20)
    out["ma50"] = sma(c, 50)
    out["ma200"] = sma(c, 200)
    out["ma200_up"] = slope_ok(out["ma200"], 20)
    out["rsi14"] = rsi(c, 14)
    line, sig, hist = macd(c)
    out["macd"] = line
    out["macd_signal"] = sig
    out["macd_hist"] = hist
    out["atr20"] = atr(out, 20)
    out["atr_pct"] = out["atr20"] / c
    out["rvol20"] = realized_vol(c, 20)
    out["dd252"] = drawdown_from_high(c, 252)
    out["stretch"] = c / out["ma20"] - 1.0
    out["low60"] = out["close"].rolling(60, min_periods=60).min().shift(1)
    out["high60"] = out["close"].rolling(60, min_periods=60).max().shift(1)
    lo = c.rolling(252, min_periods=120).min()
    hi = c.rolling(252, min_periods=120).max()
    out["range_pos"] = (c - lo) / (hi - lo).replace(0.0, np.nan)
    out["er20"] = efficiency_ratio(c, 20)
    out["er60"] = efficiency_ratio(c, 60)
    r2, logslope = trend_fit(c, 60)
    out["r2_60"] = r2
    out["logslope"] = logslope        # 하루당 로그수익률 추세 속도

    # 절대 기준만 쓰면 변동성이 큰 자산은 영원히 통과 못 한다.
    # 지수는 원래 매끄럽고 개별주는 원래 거칠기 때문이다.
    # 그래서 "이 종목 자기 이력 대비 지금이 얼마나 곧은가" 를 같이 본다.
    out["er_pct"] = out["er60"].rolling(500, min_periods=200).rank(pct=True)
    out["r2_pct"] = out["r2_60"].rolling(500, min_periods=200).rank(pct=True)
    return out
