"""백테스트.

레버리지 상품 백테스트에는 일반 주식과 다른 함정이 세 개 있다.

1. 상품 가격을 그대로 못 쓴다.
   국내 단일종목 상품은 상장 두 달, MSTX 는 액면병합을 겪었다.
   그래서 기초자산 수익률에서 상품을 재구성한다. 일간 리밸런싱이므로
   하루 수익률 = L × 기초자산 하루 수익률 − 하루치 비용.
   이렇게 하면 QQQ 20년 이력으로 TQQQ 를 20년치 시뮬레이션할 수 있다.

2. 비용을 빼먹으면 결과가 거짓말이 된다.
   운용보수 + 차입비용(|L−1| × 금리) + 매매 슬리피지를 전부 뺀다.

3. 신호가 드물어서 표본이 작다.
   보수적으로 만들었으니 종목당 신호가 몇 번 안 나온다. 게다가
   지수 종목들은 같은 시기에 동시에 켜져서 서로 독립이 아니다.
   그래서 수익률만 보지 말고 순열검정으로 운과 구분해야 한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import _self_regime
from .indicators import enrich


# ── 레버리지 상품 재구성 ────────────────────────────────────

def simulate_leveraged(close: pd.Series, L: int, cfg: dict) -> pd.Series:
    """기초자산 종가에서 일간 리밸런싱 상품의 가격 경로를 만든다.

    변동성 감가는 별도로 더하지 않는다. L배 일간 수익률을 복리로
    쌓으면 감가는 자동으로 생긴다. 그게 감가의 정의다.
    """
    c = cfg.get("costs", {})
    expense = float(c.get("expense_ratio_annual", 0.0095))
    financing = float(c.get("financing_spread_annual", 0.045))
    daily_cost = (expense + abs(L - 1) * financing) / 252.0

    r = close.pct_change().fillna(0.0)
    lev_r = L * r - daily_cost
    lev_r = lev_r.clip(lower=-0.99)          # 하루 -100% 는 상장폐지
    return 100.0 * (1.0 + lev_r).cumprod()


# ── 게이트 벡터화 ───────────────────────────────────────────
#  engine.py 는 하루치만 판정한다. 백테스트는 전 구간이 필요해서
#  같은 규칙을 벡터 연산으로 다시 쓴다. 두 구현이 어긋나면 안 되므로
#  backtest.py --verify 가 무작위 날짜를 뽑아 둘을 대조한다.

def gate_frame(df: pd.DataFrame, item: dict, cfg: dict, direction: str,
               L: int, regime_ok: pd.Series) -> pd.DataFrame:
    s = cfg["rules"]["straight"]
    er_min, r2_min = float(s["er_min"]), float(s["r2_min"])
    horizon, budget = int(s["horizon_days"]), float(s["decay_budget"])
    vmax = float(item["vol_max"])
    if direction == "short":
        vmax *= float(cfg["rules"]["short"].get("vol_multiplier", 1.0))

    gain = L * df["logslope"] * horizon
    decay = 0.5 * L * (L - 1.0) * (df["rvol20"] ** 2) * (horizon / 252.0)

    g = pd.DataFrame(index=df.index)
    g["straight"] = df["er60"] >= er_min
    g["smooth"] = df["r2_60"] >= r2_min
    g["edge"] = (gain > 0) & (decay <= gain * budget)
    g["vol"] = df["atr_pct"] <= vmax

    if direction == "long":
        g["trend"] = df["close"] > df["ma200"]
        g["align"] = (df["ma50"] > df["ma200"]) & df["ma200_up"]
        g["near"] = df["close"] > df["ma20"]
        g["not_hot"] = (df["rsi14"] < 75) & (df["stretch"] < 0.08)
        g["momentum"] = (df["macd"] > df["macd_signal"]) & (df["macd_hist"] > 0)
        g["high_zone"] = df["dd252"] > -0.10
        mandatory = ["trend", "align", "near", "straight", "smooth", "edge", "vol", "not_hot"]
    else:
        g["trend"] = df["close"] < df["ma200"]
        g["align"] = (df["ma50"] < df["ma200"]) & (~df["ma200_up"])
        g["near"] = df["close"] < df["ma20"]
        g["breakdown"] = df["close"] <= df["low60"]
        g["not_panic"] = (df["rsi14"] > 22) & (df["stretch"] > -0.12)
        g["momentum"] = (df["macd"] < df["macd_signal"]) & (df["macd_hist"] < 0)
        mandatory = ["trend", "align", "near", "straight", "smooth", "edge",
                     "breakdown", "not_panic"]

    g = g.fillna(False).astype(bool)
    g.attrs["mandatory"] = mandatory
    g.attrs["regime_ok"] = regime_ok.reindex(g.index).fillna(False).astype(bool)
    return g


def entry_signal(g: pd.DataFrame, cfg: dict, direction: str,
                 use_straight: bool = True) -> pd.Series:
    """진입 조건 충족 여부. use_straight=False 면 직진성 3종을 빼고 본다."""
    mand = list(g.attrs["mandatory"])
    cols = list(g.columns)
    if not use_straight:
        drop = {"straight", "smooth", "edge"}
        mand = [m for m in mand if m not in drop]
        cols = [c for c in cols if c not in drop]

    min_gates = int(cfg["rules"][direction]["min_gates"])
    if not use_straight:
        min_gates -= 3

    ok_mand = g[mand].all(axis=1)
    total = g[cols].sum(axis=1)
    return ok_mand & (total >= min_gates) & g.attrs["regime_ok"]


def exit_signal(df: pd.DataFrame, direction: str, cfg: dict,
                entry_atr: float) -> pd.Series:
    ex = cfg["rules"]["exit"]
    n = int(ex["below_ma20_days"])
    if direction == "long":
        broke = (df["close"] < df["ma20"]).rolling(n, min_periods=n).sum() == n
        cross = (df["close"] < df["ma50"]) & (df["macd_hist"] < 0)
    else:
        broke = (df["close"] > df["ma20"]).rolling(n, min_periods=n).sum() == n
        cross = (df["close"] > df["ma50"]) & (df["macd_hist"] > 0)
    spike = df["atr_pct"] > entry_atr * float(ex["vol_spike_mult"])
    return (broke | cross | spike).fillna(False)


# ── 거래 생성 ───────────────────────────────────────────────

def run_trades(df: pd.DataFrame, item: dict, cfg: dict, direction: str, L: int,
               regime_ok: pd.Series, use_straight: bool = True) -> list[dict]:
    """신호일 종가에 판정하고 다음 날 시가에 체결한다고 가정한다."""
    g = gate_frame(df, item, cfg, direction, L, regime_ok)
    sig = entry_signal(g, cfg, direction, use_straight)

    lev = simulate_leveraged(df["close"], L, cfg)
    slip = float(cfg.get("costs", {}).get("slippage_bps", 15)) / 10000.0
    cooldown = int(cfg["rules"][direction]["cooldown_days"])

    trades: list[dict] = []
    i, n = 210, len(df)
    last_exit = -10_000

    while i < n - 1:
        if not bool(sig.iloc[i]) or (i - last_exit) < cooldown:
            i += 1
            continue

        entry_i = i + 1                       # 다음 날 체결
        entry_atr = float(df["atr_pct"].iloc[i])
        ex = exit_signal(df.iloc[entry_i:], direction, cfg, entry_atr)
        hit = np.flatnonzero(ex.to_numpy())
        exit_i = min(entry_i + int(hit[0]) + 1, n - 1) if len(hit) else n - 1

        # 손절 · 익절 · 시간손절 중 먼저 걸리는 날로 앞당긴다
        exr = cfg["rules"]["exit"]
        seg = lev.iloc[entry_i:exit_i + 1].to_numpy()
        pnl = seg / seg[0] - 1.0
        first = None

        stop = exr.get("stop_loss")
        if stop:
            h = np.flatnonzero(pnl <= -abs(float(stop)))
            if len(h):
                first = int(h[0])

        tp = exr.get("take_profit")
        if tp:
            h = np.flatnonzero(pnl >= abs(float(tp)))
            if len(h):
                first = int(h[0]) if first is None else min(first, int(h[0]))

        tsd = exr.get("time_stop_days")
        if tsd:
            k, floor = int(tsd), float(exr.get("time_stop_min_return", 0.0))
            if len(pnl) > k and pnl[k] <= floor:
                first = k if first is None else min(first, k)

        if first is not None:
            exit_i = entry_i + first

        p_in, p_out = float(lev.iloc[entry_i]), float(lev.iloc[exit_i])
        ret = (p_out / p_in) - 1.0 - 2 * slip
        u_ret = float(df["close"].iloc[exit_i] / df["close"].iloc[entry_i] - 1.0)

        path = lev.iloc[entry_i:exit_i + 1]
        mdd = float((path / path.cummax() - 1.0).min())

        trades.append({
            "종목": item["name"], "방향": direction, "배수": L,
            "진입일": str(df.index[entry_i].date()),
            "청산일": str(df.index[exit_i].date()),
            "보유일": int(exit_i - entry_i),
            "수익률": ret, "기초자산수익률": u_ret, "최대낙폭": mdd,
            "진입ER": float(df["er60"].iloc[i]), "진입R2": float(df["r2_60"].iloc[i]),
        })
        last_exit = exit_i
        i = exit_i + 1

    return trades


# ── 성과 요약 ───────────────────────────────────────────────

def summarize(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"이름": label, "거래수": 0}
    r = np.array([t["수익률"] for t in trades])
    hold = np.array([t["보유일"] for t in trades])
    equity = np.cumprod(1 + r)
    return {
        "이름": label,
        "거래수": len(r),
        "승률": float((r > 0).mean()),
        "평균수익": float(r.mean()),
        "중앙값": float(np.median(r)),
        "최고": float(r.max()),
        "최악": float(r.min()),
        "누적": float(equity[-1] - 1.0),
        "거래MDD": float((equity / np.maximum.accumulate(equity) - 1).min()),
        "평균보유일": float(hold.mean()),
        "총보유일": int(hold.sum()),
        "평균개별낙폭": float(np.mean([t["최대낙폭"] for t in trades])),
    }


def permutation_test(trades: list[dict], frames: dict, cfg: dict,
                     n_iter: int = 500, seed: int = 0) -> float:
    """같은 종목·같은 보유기간으로 아무 날에나 들어갔을 때와 비교한다.

    반환값은 p-value. 0.05 보다 크면 '운과 구분이 안 된다'는 뜻이다.
    """
    if not trades:
        return float("nan")
    rng = np.random.default_rng(seed)
    actual = float(np.mean([t["수익률"] for t in trades]))

    pool: dict[tuple, pd.Series] = {}
    for t in trades:
        key = (t["종목"], t["배수"])
        if key not in pool:
            df = frames.get(t["종목"])
            if df is None:
                continue
            pool[key] = simulate_leveraged(df["close"], t["배수"], cfg)

    wins = 0
    for _ in range(n_iter):
        rs = []
        for t in trades:
            lev = pool.get((t["종목"], t["배수"]))
            if lev is None or len(lev) < 260:
                continue
            hold = max(1, t["보유일"])
            hi = len(lev) - hold - 1
            if hi <= 210:
                continue
            j = int(rng.integers(210, hi))
            rs.append(float(lev.iloc[j + hold] / lev.iloc[j] - 1.0))
        if rs and np.mean(rs) >= actual:
            wins += 1
    return (wins + 1) / (n_iter + 1)
