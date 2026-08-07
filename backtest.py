#!/usr/bin/env python3
"""백테스트 실행.

  python backtest.py                전 종목 백테스트
  python backtest.py --years 15     조회 기간 (기본 15년)
  python backtest.py --verify       벡터 구현이 실전 엔진과 같은지 대조
  python backtest.py --group "미국 지수"   특정 그룹만
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from core import data
from core.backtest import (gate_frame, entry_signal, run_trades,
                           simulate_leveraged, summarize, permutation_test)
from core.engine import evaluate
from core.scan import load_config


def regime_series(cfg: dict, frames: dict) -> dict:
    """전 구간에 대해 날짜별 국면 허용 여부를 만든다."""
    out = {}

    spy = frames.get(cfg["regime"]["us"]["index"])
    vix = frames.get(cfg["regime"]["us"]["vix"])
    if spy is not None:
        above = spy["close"] > spy["ma200"]
        if vix is not None:
            v = vix["close"].reindex(spy.index).ffill()
            calm = v <= float(cfg["regime"]["us"]["vix_max"])
            fear = v >= float(cfg["regime"]["us"]["vix_min_for_short"])
        else:
            calm = pd.Series(True, index=spy.index)
            fear = pd.Series(True, index=spy.index)
        out["US"] = {"long": (above & calm).fillna(False),
                     "short": ((~above) & fear).fillna(False)}

    kr = frames.get(cfg["regime"]["kr"]["index"])
    if kr is not None:
        above = kr["close"] > kr["ma200"]
        calm = kr["rvol20"] <= float(cfg["regime"]["kr"]["realized_vol_max"])
        out["KR"] = {"long": (above & calm).fillna(False),
                     "short": (~above).fillna(False)}
    return out


def _regime_for(item, direction, cfg, regimes, df):
    if item["group"] in set(cfg.get("regime_exempt_groups", [])):
        pos = df["range_pos"]
        return (pos > 0.5).fillna(False) if direction == "long" else (pos < 0.5).fillna(False)
    r = regimes.get(item["market"])
    if r is None:
        return pd.Series(False, index=df.index)
    return r[direction].reindex(df.index).ffill().fillna(False)


def verify(cfg, frames, regimes, n=200) -> int:
    """벡터 게이트와 engine.evaluate 가 같은 답을 내는지 무작위 대조."""
    rng = np.random.default_rng(0)
    checked = bad = 0
    for item in cfg["universe"]:
        df = frames.get(item["u"])
        if df is None or len(df) < 400:
            continue
        for direction in ("long", "short"):
            prods = item.get(direction) or []
            if not prods:
                continue
            L = max((int(x) for _, x in prods), key=abs)
            ro = _regime_for(item, direction, cfg, regimes, df)
            g = gate_frame(df, item, cfg, direction, L, ro)
            sig = entry_signal(g, cfg, direction)

            for _ in range(3):
                i = int(rng.integers(260, len(df)))
                sub = df.iloc[:i + 1]
                other = "short" if direction == "long" else "long"
                solo = dict(item); solo[other] = []
                rd = {"US": {"long_ok": bool(ro.iloc[i]), "short_ok": bool(ro.iloc[i]),
                             "detail": ""},
                      "KR": {"long_ok": bool(ro.iloc[i]), "short_ok": bool(ro.iloc[i]),
                             "detail": ""}}
                v = evaluate(solo, sub, rd, cfg)
                if v.direction != direction:
                    continue
                checked += 1
                if (v.status == "entry") != bool(sig.iloc[i]):
                    bad += 1
                    if bad <= 5:
                        print(f"  불일치 {item['name']} {direction} {df.index[i].date()}: "
                              f"엔진={v.status} 벡터={bool(sig.iloc[i])}")
                if checked >= n:
                    break
    print(f"\n대조 {checked}건 중 불일치 {bad}건")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=15)
    ap.add_argument("--group", default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--iters", type=int, default=500)
    args = ap.parse_args()

    cfg = load_config()
    universe = [u for u in cfg["universe"]
                if not args.group or u["group"] == args.group]

    tickers = [u["u"] for u in universe] + [
        cfg["regime"]["us"]["index"], cfg["regime"]["us"]["vix"],
        cfg["regime"]["kr"]["index"]]
    print(f"기초자산 {len(set(tickers))}건 · 최대 {args.years}년 …")
    frames = data.load_many(sorted(set(tickers)), years=args.years)
    if len(frames) < 3:
        print("데이터를 거의 받지 못했습니다. 네트워크를 확인하세요.")
        return 1
    regimes = regime_series(cfg, frames)

    if args.verify:
        return verify(cfg, frames, regimes)

    by_name = {u["name"]: frames.get(u["u"]) for u in universe}
    on, off, bench = [], [], []

    for item in universe:
        df = frames.get(item["u"])
        if df is None or len(df) < 400:
            continue
        for direction in ("long", "short"):
            prods = item.get(direction) or []
            if not prods:
                continue
            L = max((int(x) for _, x in prods), key=abs)
            ro = _regime_for(item, direction, cfg, regimes, df)
            on += run_trades(df, item, cfg, direction, L, ro, use_straight=True)
            off += run_trades(df, item, cfg, direction, L, ro, use_straight=False)

            lev = simulate_leveraged(df["close"], L, cfg)
            bench.append({"종목": item["name"], "방향": direction, "배수": L,
                          "수익률": float(lev.iloc[-1] / lev.iloc[210] - 1.0),
                          "보유일": len(df) - 210, "최대낙폭":
                          float((lev / lev.cummax() - 1).min())})

    if not on:
        print("신호가 한 번도 없었습니다. 임계값이 너무 빡빡합니다.")
        return 0

    a = summarize(on, "직진성 게이트 켬")
    b = summarize(off, "직진성 게이트 끔")
    c = summarize(bench, "레버리지 계속 보유")

    def show(s):
        if not s.get("거래수"):
            print(f"{s['이름']:<20} 거래 없음"); return
        print(f"{s['이름']:<20}{s['거래수']:>6}{s['승률']*100:>8.0f}%"
              f"{s['평균수익']*100:>9.1f}%{s['중앙값']*100:>9.1f}%"
              f"{s['최악']*100:>9.1f}%{s['거래MDD']*100:>9.1f}%"
              f"{s['평균보유일']:>8.0f}일")

    print()
    print(f"{'':<20}{'거래':>6}{'승률':>9}{'평균':>9}{'중앙':>9}{'최악':>9}{'MDD':>9}{'보유':>9}")
    print("─" * 82)
    show(a); show(b); show(c)

    print()
    print("■ 직진성 게이트가 실제로 기여했는가")
    if b.get("거래수"):
        d_ret = (a["평균수익"] - b["평균수익"]) * 100
        d_win = (a["승률"] - b["승률"]) * 100
        print(f"   평균수익 {d_ret:+.2f}%p · 승률 {d_win:+.1f}%p · "
              f"거래수 {a['거래수']} vs {b['거래수']}")
        print("   거래를 줄인 만큼 건당 성과가 올라가야 의미가 있습니다.")

    print()
    print("■ 운과 구분되는가 (순열검정)")
    p = permutation_test(on, by_name, cfg, n_iter=args.iters)
    print(f"   같은 종목·같은 보유기간으로 아무 날에나 들어간 경우와 비교")
    print(f"   p-value = {p:.3f}", end="  ")
    if p < 0.05:
        print("→ 우연으로 보기 어렵습니다")
    elif p < 0.15:
        print("→ 애매합니다. 표본이 더 필요합니다")
    else:
        print("→ 운과 구분되지 않습니다")

    print()
    print("■ 표본 경고")
    yrs = args.years
    print(f"   {yrs}년 동안 신호 {a['거래수']}건 = 연 {a['거래수']/yrs:.1f}건")
    if a["거래수"] < 30:
        print("   30건 미만이면 어떤 통계도 신뢰하기 어렵습니다.")
    idx = [t for t in on if "지수" in str(t.get("종목", ""))]
    print(f"   지수 종목들은 같은 강세장에 동시에 켜지므로 서로 독립이 아닙니다.")
    print(f"   실질 표본은 표시된 {a['거래수']}건보다 훨씬 적습니다.")

    print()
    print("■ 최악의 거래 5건")
    for t in sorted(on, key=lambda x: x["수익률"])[:5]:
        print(f"   {t['수익률']*100:>7.1f}%  {t['종목']:<16}{t['방향']:<6}"
              f"{t['진입일']}~{t['청산일']} ({t['보유일']}일) "
              f"기초자산 {t['기초자산수익률']*100:+.1f}%")

    out = pd.DataFrame(on)
    out.to_csv("state/backtest_trades.csv", index=False, encoding="utf-8-sig")
    print(f"\n거래 내역 {len(out)}건 → state/backtest_trades.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
