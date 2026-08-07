"""보수적 합류(confluence) 규칙 엔진.

판단 단위는 기초자산이다. 같은 기초자산을 쓰는 레버리지 상품은
전부 같은 추세 판단을 공유하므로, 카드 하나에 묶어서 다룬다.

기초자산이 MA200 위면 롱 쪽 조건을, 아래면 인버스 쪽 조건을 본다.
둘은 동시에 성립할 수 없으므로 한 기초자산의 후보 방향은 항상 하나다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np
import pandas as pd

from .indicators import decay_estimate


@dataclass
class Gate:
    key: str
    label: str
    mandatory: bool
    passed: bool
    detail: str


@dataclass
class Verdict:
    id: str
    name: str
    group: str
    market: str
    direction: str                  # 이번에 평가한 방향
    status: str                     # entry | exit | holding | watch | flat | none | blocked
    passed: int = 0
    total: int = 9
    gates: list[Gate] = field(default_factory=list)
    products: list[dict] = field(default_factory=list)
    products_other: list[dict] = field(default_factory=list)
    price: float | None = None
    change_pct: float | None = None
    er60: float | None = None          # 직진성 (0~1)
    r2: float | None = None            # 경로 매끄러움 (0~1)
    decay_20d: float | None = None     # 20일 예상 감가
    gain_20d: float | None = None      # 20일 예상 추세이익
    regime_ok: bool = True
    leverage: int = 1
    pick_leverage: int | None = None      # 감당 가능한 배수
    pick_net: float | None = None         # 그 배수의 20일 기대 순이익
    pick: dict | None = None              # 최종 추천 종목 1개
    plan: dict | None = None              # 매수·손절·목표·시간한도
    leverage_table: list = field(default_factory=list)
    asof: str | None = None
    reason: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["gates"] = [asdict(g) for g in self.gates]
        return d


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


# ── 시장 국면 ────────────────────────────────────────────────

def evaluate_regime(cfg: dict, frames: dict[str, pd.DataFrame]) -> dict[str, dict]:
    out: dict[str, dict] = {}

    us = cfg["regime"]["us"]
    spy, vix = frames.get(us["index"]), frames.get(us["vix"])
    if spy is not None and len(spy) and not pd.isna(spy["ma200"].iloc[-1]):
        row = spy.iloc[-1]
        above = bool(row["close"] > row["ma200"])
        vix_last = float(vix["close"].iloc[-1]) if vix is not None and len(vix) else None
        vix_calm = vix_last is None or vix_last <= us["vix_max"]
        out["US"] = {
            "long_ok": above and vix_calm,
            "short_ok": (not above) and (vix_last is None or vix_last >= us["vix_min_for_short"]),
            "detail": f"S&P500 {'MA200 위' if above else 'MA200 아래'}"
                      + (f" · VIX {vix_last:.1f}" if vix_last is not None else ""),
            "vix": vix_last, "index_above_ma200": above,
        }
    else:
        out["US"] = {"long_ok": False, "short_ok": False, "detail": "데이터 부족",
                     "vix": None, "index_above_ma200": None}

    kr = cfg["regime"]["kr"]
    k = frames.get(kr["index"])
    if k is not None and len(k) and not pd.isna(k["ma200"].iloc[-1]):
        row = k.iloc[-1]
        above = bool(row["close"] > row["ma200"])
        rv = None if pd.isna(row["rvol20"]) else float(row["rvol20"])
        calm = rv is None or rv <= kr["realized_vol_max"]
        out["KR"] = {
            "long_ok": above and calm,
            "short_ok": not above,
            "detail": f"코스피200 {'MA200 위' if above else 'MA200 아래'}"
                      + (f" · 실현변동성 {rv * 100:.0f}%" if rv is not None else ""),
            "vix": rv, "index_above_ma200": above,
        }
    else:
        out["KR"] = {"long_ok": False, "short_ok": False, "detail": "데이터 부족",
                     "vix": None, "index_above_ma200": None}

    return out


def _self_regime(row) -> dict:
    """채권·원자재·크립토는 주식 국면과 따로 논다. 자기 52주 레인지 위치로 대체."""
    pos = row.get("range_pos")
    if pos is None or pd.isna(pos):
        return {"long_ok": False, "short_ok": False, "detail": "레인지 데이터 부족"}
    pos = float(pos)
    return {"long_ok": pos > 0.5, "short_ok": pos < 0.5,
            "detail": f"52주 레인지 {pos * 100:.0f}% 지점"}


# ── 게이트 ──────────────────────────────────────────────────

def choose_leverage(row, item, cfg, direction: str) -> dict:
    """이 종목에서 어떤 배수까지 감당되는지 고른다.

    지금까지는 가장 높은 배수만 보고 판정해서, 3배가 과하면 2배도 같이
    탈락시켰다. 2배가 감가를 이긴다면 2배는 사도 되는 게 맞다.
    그래서 배수별로 따로 계산하고, 통과하는 것 중 기대이익이 가장 큰 걸 고른다.
    """
    s = cfg["rules"]["straight"]
    horizon, budget = int(s["horizon_days"]), float(s["decay_budget"])
    sigma = float(row["rvol20"]) if not pd.isna(row["rvol20"]) else float("nan")
    slope = float(row["logslope"]) if not pd.isna(row["logslope"]) else 0.0

    levels = sorted({int(x) for _, x in (item.get(direction) or [])}, key=abs, reverse=True)
    rows, best = [], None
    for L in levels:
        gain = L * slope * horizon
        decay = decay_estimate(sigma, L, horizon)
        ok = bool(gain > 0 and not pd.isna(decay) and decay <= gain * budget)
        net = gain - (0.0 if pd.isna(decay) else decay)
        rows.append({"L": L, "gain": gain, "decay": None if pd.isna(decay) else decay,
                     "ok": ok, "net": net})
        if ok and (best is None or net > best["net"]):
            best = rows[-1]

    return {"levels": rows, "best": best,
            "top": rows[0] if rows else None,
            "horizon": horizon, "budget": budget}


def _straightness(row, item, cfg, L: int, choice: dict | None = None) -> tuple[list[Gate], dict]:
    """레버리지의 핵심. 같은 폭을 움직여도 경로가 다르면 결과가 다르다.

    L 은 그 방향에서 배수가 가장 큰 상품 기준. 감가는 최악을 본다.
    """
    s = cfg["rules"]["straight"]
    horizon = int(s["horizon_days"])
    er_min = float(item.get("er_min", s["er_min"]))
    r2_min = float(item.get("r2_min", s["r2_min"]))
    pct_min = float(s.get("pctile_min", 0.70))
    er_strong = float(s.get("er_strong", 0.15))
    r2_strong = float(s.get("r2_strong", 0.45))

    er = float(row["er60"]) if not pd.isna(row["er60"]) else 0.0
    r2 = float(row["r2_60"]) if not pd.isna(row["r2_60"]) else 0.0
    erp = float(row["er_pct"]) if not pd.isna(row.get("er_pct")) else 0.0
    r2p = float(row["r2_pct"]) if not pd.isna(row.get("r2_pct")) else 0.0
    sigma = float(row["rvol20"]) if not pd.isna(row["rvol20"]) else float("nan")

    # 기초자산이 지금 속도로 horizon 일 더 가면 레버리지 상품이 얻을 몫
    gain = L * float(row["logslope"]) * horizon
    decay = decay_estimate(sigma, L, horizon)
    budget = float(s["decay_budget"])
    edge_ok = bool(gain > 0 and not pd.isna(decay) and decay <= gain * budget)

    # 배수별로 따져서, 감당 가능한 배수가 하나라도 있으면 통과시킨다
    edge_detail = None
    if choice:
        best, top = choice.get("best"), choice.get("top")
        edge_ok = best is not None
        if best is None and top is not None and top["decay"] is not None:
            edge_detail = (f"{abs(top['L'])}배 기준 {horizon}일 추세이익 "
                           f"{top['gain'] * 100:+.1f}% vs 감가 −{top['decay'] * 100:.1f}%"
                           " — 감당 가능한 배수 없음")
        elif best is not None:
            note = "" if best is top else f" ({abs(top['L'])}배는 감가 과다)"
            edge_detail = (f"{abs(best['L'])}배 기준 추세이익 {best['gain'] * 100:+.1f}% vs "
                           f"감가 −{best['decay'] * 100:.1f}%{note}")

    gates = [
        # 두 갈래 중 하나만 맞으면 된다.
        #   절대적으로 곧거나 (지수처럼 원래 매끄러운 자산)
        #   자기 이력 기준으로 유난히 곧거나 (개별주처럼 원래 거친 자산)
        # 절대 기준만 쓰면 변동성 큰 종목은 영원히 통과 못 한다.
        Gate("straight", "직진성", True,
             bool(er >= er_strong or (er >= er_min and erp >= pct_min)),
             f"ER {er:.2f} — 절대 {er_strong:.2f} 이상이거나, "
             f"{er_min:.2f} 이상이면서 자기 이력 상위 {(1 - erp) * 100:.0f}%"),
        Gate("smooth", "경로 매끄러움", True,
             bool(r2 >= r2_strong or (r2 >= r2_min and r2p >= pct_min)),
             f"R² {r2:.2f} — 절대 {r2_strong:.2f} 이상이거나, "
             f"{r2_min:.2f} 이상이면서 자기 이력 상위 {(1 - r2p) * 100:.0f}%"),
        Gate("edge", f"감가 여유 (감가 ≤ 추세이익×{budget:.0%})", True, edge_ok,
             edge_detail or (
                 f"{horizon}일 예상 추세이익 {gain * 100:+.1f}% vs 감가 −{decay * 100:.1f}%"
                 if not pd.isna(decay) else "변동성 산출 불가")),
    ]
    stats = {"er60": er, "r2": r2, "decay_20d": None if pd.isna(decay) else decay,
             "gain_20d": gain}
    return gates, stats


def _long_gates(row, item, cfg, vmax, L, choice=None) -> tuple[list[Gate], dict]:
    straight, stats = _straightness(row, item, cfg, L, choice)
    gates = [
        Gate("trend", "종가 > MA200", True,
             bool(row["close"] > row["ma200"]),
             f"MA200 대비 {_pct(row['close'] / row['ma200'] - 1)}"),
        Gate("align", "정배열 + MA200 우상향", True,
             bool(row["ma50"] > row["ma200"] and row["ma200_up"]),
             f"MA50/MA200 이격 {_pct(row['ma50'] / row['ma200'] - 1)}"),
        Gate("near", "종가 > MA20", True,
             bool(row["close"] > row["ma20"]),
             f"MA20 대비 {_pct(row['stretch'])}"),
        *straight,
        Gate("vol", f"ATR20 ≤ {vmax * 100:.1f}%", True,
             bool(row["atr_pct"] <= vmax),
             f"현재 {row['atr_pct'] * 100:.1f}%"),
        Gate("not_hot", "과열 아님", True,
             bool(row["rsi14"] < 75 and row["stretch"] < 0.08),
             f"RSI {row['rsi14']:.0f} · 이격 {_pct(row['stretch'])}"),
        Gate("momentum", "MACD 정배열", False,
             bool(row["macd"] > row["macd_signal"] and row["macd_hist"] > 0),
             f"히스토그램 {row['macd_hist']:+.2f}"),
        Gate("high_zone", "고점권 유지", False,
             bool(row["dd252"] > -0.10),
             f"52주 고점 대비 {_pct(row['dd252'])}"),
    ]
    return gates, stats


def _short_gates(row, item, cfg, vmax, L, choice=None) -> tuple[list[Gate], dict]:
    straight, stats = _straightness(row, item, cfg, L, choice)
    gates = [
        Gate("trend", "종가 < MA200", True,
             bool(row["close"] < row["ma200"]),
             f"MA200 대비 {_pct(row['close'] / row['ma200'] - 1)}"),
        Gate("align", "역배열 + MA200 우하향", True,
             bool(row["ma50"] < row["ma200"] and not row["ma200_up"]),
             f"MA50/MA200 이격 {_pct(row['ma50'] / row['ma200'] - 1)}"),
        Gate("near", "종가 < MA20", True,
             bool(row["close"] < row["ma20"]),
             f"MA20 대비 {_pct(row['stretch'])}"),
        *straight,
        Gate("breakdown", "60일 저점 이탈", True,
             bool(row["close"] <= row["low60"]),
             f"60일 저점 대비 {_pct(row['close'] / row['low60'] - 1)}"),
        Gate("not_panic", "투매 구간 아님", True,
             bool(row["rsi14"] > 22 and row["stretch"] > -0.12),
             f"RSI {row['rsi14']:.0f} · 이격 {_pct(row['stretch'])}"),
        Gate("vol", f"ATR20 ≤ {vmax * 100:.1f}%", False,
             bool(row["atr_pct"] <= vmax),
             f"현재 {row['atr_pct'] * 100:.1f}%"),
        Gate("momentum", "MACD 역배열", False,
             bool(row["macd"] < row["macd_signal"] and row["macd_hist"] < 0),
             f"히스토그램 {row['macd_hist']:+.2f}"),
    ]
    return gates, stats


def _fail_reason(gates: list[Gate]) -> str:
    missed = [g for g in gates if not g.passed]
    if not missed:
        return "모든 조건 충족"
    hard = [g for g in missed if g.mandatory]
    pick = (hard or missed)[:2]
    return "미충족: " + ", ".join(g.label for g in pick) + (
        f" 외 {len(missed) - len(pick)}건" if len(missed) > len(pick) else "")


# ── 이탈(청산) 판정 ─────────────────────────────────────────

def position_return(df: pd.DataFrame, entry: dict) -> float | None:
    """보유 중인 레버리지 상품의 현재 손익률.

    상품 가격을 매번 받아오지 않아도 되게, 기초자산 일간수익률을 L배로
    복리해서 추정한다. 일간 리밸런싱 상품의 정의 그대로다.
    """
    L, opened = entry.get("leverage"), entry.get("opened")
    if not L or not opened:
        return None
    try:
        since = df.loc[str(opened):]
        if len(since) < 2:
            return None
        r = since["close"].pct_change().fillna(0.0).to_numpy()[1:]
        return float(np.prod(1.0 + int(L) * r) - 1.0)
    except Exception:
        return None


def check_exit(df: pd.DataFrame, direction: str, entry: dict, cfg: dict) -> str | None:
    ex = cfg["rules"]["exit"]
    n = int(ex["below_ma20_days"])
    tail, row = df.iloc[-n:], df.iloc[-1]

    if direction == "long":
        if len(tail) == n and bool((tail["close"] < tail["ma20"]).all()):
            return f"MA20 {n}일 연속 하회"
        if bool(row["close"] < row["ma50"]) and bool(row["macd_hist"] < 0):
            return "MA50 이탈 + MACD 음전"
    else:
        if len(tail) == n and bool((tail["close"] > tail["ma20"]).all()):
            return f"기초자산이 MA20 {n}일 연속 상회"
        if bool(row["close"] > row["ma50"]) and bool(row["macd_hist"] > 0):
            return "기초자산 MA50 회복 + MACD 양전"

    # 보유 중 상품 손익. 손절·익절·시간손절이 전부 이걸 본다.
    lev_r = position_return(df, entry)

    if lev_r is not None:
        tp = ex.get("take_profit")
        if tp and lev_r >= abs(float(tp)):
            return f"익절 {lev_r * 100:+.0f}% (기준 +{abs(float(tp)) * 100:.0f}%)"

        # 시간 손절: 레버리지는 제자리여도 감가로 계속 깎인다.
        # 정해진 기간 안에 못 벌면 추세가 없는 것으로 본다.
        tsd = ex.get("time_stop_days")
        if tsd and entry.get("opened"):
            try:
                held = len(df.loc[str(entry["opened"]):]) - 1
                floor = float(ex.get("time_stop_min_return", 0.0))
                if held >= int(tsd) and lev_r <= floor:
                    return (f"시간 손절 — {held}거래일 지났는데 {lev_r * 100:+.1f}% "
                            f"(기준 {int(tsd)}일 / {floor * 100:+.0f}%)")
            except Exception:
                pass

    # 손절: 레버리지 상품 기준 손실률. 기초자산 일간수익률을 L배로 복리해
    # 상품 손익을 추정한다 (일간 리밸런싱 상품의 정의 그대로).
    stop = ex.get("stop_loss")
    if stop and lev_r is not None and lev_r <= -abs(float(stop)):
        return f"손절 {lev_r * 100:.0f}% (기준 −{abs(float(stop)) * 100:.0f}%)"

    ref = entry.get("atr_pct")
    if ref and float(row["atr_pct"]) > float(ref) * float(ex["vol_spike_mult"]):
        return f"변동성 급등 (진입 시 {ref * 100:.1f}% → {row['atr_pct'] * 100:.1f}%)"
    return None


# ── 기초자산 1건 평가 ───────────────────────────────────────

def evaluate(item: dict, df: pd.DataFrame | None, regimes: dict, cfg: dict) -> Verdict:
    v = Verdict(id=item["u"], name=item["name"], group=item["group"],
                market=item["market"], direction="long", status="blocked")

    if df is None or len(df) < 210 or pd.isna(df["ma200"].iloc[-1]):
        v.error = "기초자산 데이터 부족 (200거래일 이상 필요)"
        v.reason = v.error
        return v

    row = df.iloc[-1]
    v.asof = str(df.index[-1].date())
    v.price = float(row["close"])
    if len(df) > 1:
        v.change_pct = float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100

    # 후보 방향: MA200 위면 롱, 아래면 인버스. 둘은 동시에 성립할 수 없다.
    v.direction = "long" if bool(row["close"] > row["ma200"]) else "short"
    other = "short" if v.direction == "long" else "long"
    v.products = [{"t": t, "x": x, "side": v.direction}
                  for t, x in (item.get(v.direction) or [])]
    # 반대 방향 상품도 목록에는 보여준다. 지금 추세가 아니라 판정 대상은 아니지만,
    # 이 기초자산에 뭐가 있는지 한눈에 보이는 편이 낫다.
    v.products_other = [{"t": t, "x": x, "side": other}
                        for t, x in (item.get(other) or [])]

    if not v.products:
        v.status = "none"
        v.reason = ("추세는 하락 쪽인데 인버스 상품이 없습니다"
                    if v.direction == "short" else "해당 방향 상품이 없습니다")
        return v

    exempt = set(cfg.get("regime_exempt_groups", []))
    regime = _self_regime(row) if item["group"] in exempt else regimes[item["market"]]
    v.regime_ok = bool(regime["long_ok"] if v.direction == "long" else regime["short_ok"])

    vmax = float(item["vol_max"])
    if v.direction == "short":
        vmax *= float(cfg["rules"]["short"].get("vol_multiplier", 1.0))

    # 감가는 그 방향 상품 중 배수가 가장 큰 것 기준으로 계산한다
    L = max((int(x) for _, x in (item.get(v.direction) or [])), key=abs, default=1)
    choice = choose_leverage(row, item, cfg, v.direction)
    fn = _long_gates if v.direction == "long" else _short_gates
    gates, stats = fn(row, item, cfg, vmax, L, choice)
    v.leverage_table = choice["levels"]
    if choice["best"]:
        v.pick_leverage = int(choice["best"]["L"])
        v.pick_net = float(choice["best"]["net"])
    v.gates = gates
    v.passed = sum(1 for g in gates if g.passed)
    v.total = len(gates)
    v.er60, v.r2 = stats["er60"], stats["r2"]
    v.decay_20d, v.gain_20d = stats["decay_20d"], stats["gain_20d"]
    v.leverage = L

    r = cfg["rules"][v.direction]
    missed_mandatory = sum(1 for g in gates if g.mandatory and not g.passed)
    qualifies = missed_mandatory == 0 and v.passed >= int(r["min_gates"])

    # 시장 국면은 게이트가 아니라 거부권이다. 종목 조건이 다 맞아도
    # 시장이 아니면 진입시키지 않는다.
    if qualifies and not v.regime_ok:
        v.status = "watch"
        v.reason = f"종목 조건은 충족했지만 시장 국면이 막고 있습니다 — {regime['detail']}"
    elif qualifies:
        v.status = "entry"
        v.reason = f"필수 조건 전부 + {v.passed}/{v.total} 충족 · {regime['detail']}"
    elif missed_mandatory <= 1 and v.passed >= int(r["watch_gates"]):
        v.status = "watch"
        v.reason = _fail_reason(gates)
    else:
        v.status = "flat"
        v.reason = _fail_reason(gates)
    return v
