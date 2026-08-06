#!/usr/bin/env python3
"""합성 시세로 규칙 엔진을 검증한다. 네트워크 없이 실행된다.

  python selftest.py

핵심은 '같은 수익률, 다른 경로' 케이스다. 기초자산이 똑같이 올랐어도
오르내리며 도달했으면 레버리지는 깎인다. 그걸 걸러내는지 확인한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.engine import evaluate, evaluate_regime, check_exit
from core.indicators import enrich, decay_estimate
from core.scan import load_config

N = 520


def _frame(close: np.ndarray) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n = len(close)
    wig = np.abs(rng.normal(0, 0.003, n)) * close
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    return enrich(pd.DataFrame({
        "open": np.r_[close[0], close[:-1]], "high": close + wig,
        "low": close - wig, "close": close, "volume": 1e6}, index=idx))


def smooth(n=N, total=0.55, vol=0.006, seed=1, start=100.0):
    """완만하고 꾸준한 추세. 노이즈가 작다."""
    rng = np.random.default_rng(seed)
    drift = np.log(1 + total) / n
    return _frame(start * np.exp(np.cumsum(rng.normal(drift, vol, n))))


def choppy(n=N, total=0.55, amp=0.075, period=17, vol=0.012, seed=1, start=100.0):
    """같은 총 수익률에 도달하지만 오르내림이 크다. 레버리지가 녹는 구간."""
    rng = np.random.default_rng(seed)
    drift = np.log(1 + total) / n
    t = np.arange(n)
    base = np.cumsum(rng.normal(drift, vol, n))
    return _frame(start * np.exp(base + amp * np.sin(2 * np.pi * t / period)))


def falling(n=N, total=-0.55, vol=0.005, seed=1, start=100.0):
    """확실히 쭉 내려가는 국면. 드리프트 대비 노이즈가 작고 저점에서 끝난다."""
    rng = np.random.default_rng(seed)
    drift = np.log(1 + total) / n
    return _frame(start * np.exp(np.cumsum(rng.normal(drift, vol, n))))


ITEM = {"u": "U", "name": "테스트", "group": "미국 지수", "market": "US",
        "vol_max": 0.030, "long": [["LEV", 3]], "short": [["INV", -3]]}
OK = {"US": {"long_ok": True, "short_ok": True, "detail": "테스트 국면"},
      "KR": {"long_ok": True, "short_ok": True, "detail": "테스트 국면"}}

results = []


def show(title, ok, extra=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {title}")
    if extra:
        print(f"      {extra}")


def main():
    cfg = load_config()
    print("─" * 72)
    print("■ 핵심: 같은 수익률, 다른 경로")
    print("─" * 72)

    ds, dc = smooth(), choppy()
    rs, rc = ds.iloc[-1], dc.iloc[-1]
    net_s = ds["close"].iloc[-1] / ds["close"].iloc[0] - 1
    net_c = dc["close"].iloc[-1] / dc["close"].iloc[0] - 1
    print(f"      매끄러운 쪽 총수익 {net_s * 100:+.0f}% · 출렁이는 쪽 {net_c * 100:+.0f}%")
    print(f"      직진성 ER60  {rs['er60']:.2f} vs {rc['er60']:.2f}")
    print(f"      경로 R²      {rs['r2_60']:.2f} vs {rc['r2_60']:.2f}")
    print(f"      실현변동성   {rs['rvol20'] * 100:.0f}% vs {rc['rvol20'] * 100:.0f}%")
    d_s = decay_estimate(float(rs["rvol20"]), 3, 20)
    d_c = decay_estimate(float(rc["rvol20"]), 3, 20)
    print(f"      3배 20일 예상감가  {d_s * 100:.1f}% vs {d_c * 100:.1f}%")
    print()

    vs = evaluate(ITEM, ds, OK, cfg)
    vc = evaluate(ITEM, dc, OK, cfg)
    show("매끄러운 상승 → 진입", vs.status == "entry",
         f"상태={vs.status} {vs.passed}/{vs.total}")
    show("같은 수익률인데 출렁이는 상승 → 진입 없음", vc.status != "entry",
         f"상태={vc.status} {vc.passed}/{vc.total} · {vc.reason[:60]}")
    blocked = [g.key for g in vc.gates if not g.passed]
    show("막은 게 직진성 계열인지", any(k in blocked for k in ("straight", "smooth", "edge")),
         f"미충족 게이트: {', '.join(blocked)}")

    print()
    print("─" * 72)
    print("■ 나머지 규칙")
    print("─" * 72)

    vf = evaluate(ITEM, falling(), OK, cfg)
    show("매끄러운 하락 → 인버스 진입", vf.status == "entry" and vf.direction == "short",
         f"방향={vf.direction} 상태={vf.status} {vf.passed}/{vf.total}")

    vfc = evaluate(ITEM, choppy(total=-0.45, amp=0.09), OK, cfg)
    show("출렁이는 하락 → 인버스 진입 없음", vfc.status != "entry",
         f"상태={vfc.status} {vfc.passed}/{vfc.total}")

    blocked_regime = {"US": {"long_ok": False, "short_ok": False, "detail": "차단"},
                      "KR": {"long_ok": False, "short_ok": False, "detail": "차단"}}
    vr = evaluate(ITEM, smooth(), blocked_regime, cfg)
    show("시장 국면 차단 → 조건 다 맞아도 진입 없음", vr.status != "entry",
         f"상태={vr.status} · {vr.reason[:60]}")

    no_short = dict(ITEM, short=[])
    vn = evaluate(no_short, falling(), OK, cfg)
    show("하락 추세인데 인버스 상품 없음 → 신호 없음", vn.status == "none",
         f"상태={vn.status} · {vn.reason}")

    hot = smooth()
    hot.loc[hot.index[-6:], "close"] *= np.linspace(1.03, 1.16, 6)
    hot = enrich(hot[["open", "high", "low", "close", "volume"]])
    vh = evaluate(ITEM, hot, OK, cfg)
    show("급등 직후 과열 → 진입 없음", vh.status != "entry",
         f"상태={vh.status} {vh.passed}/{vh.total}")

    d = smooth()
    d.loc[d.index[-4:], "close"] *= np.linspace(0.98, 0.90, 4)
    d = enrich(d[["open", "high", "low", "close", "volume"]])
    show("보유 중 추세 이탈 → 청산 사유", check_exit(d, "long", {"atr_pct": 0.006}, cfg) is not None,
         f"사유={check_exit(d, 'long', {'atr_pct': 0.006}, cfg)}")
    show("추세 유지 중 → 청산 사유 없음",
         check_exit(smooth(), "long", {"atr_pct": 0.006}, cfg) is None)

    show("인버스2X 감가가 정방향 2X 보다 3배",
         abs(decay_estimate(0.5, -2, 20) / decay_estimate(0.5, 2, 20) - 3.0) < 1e-9,
         f"2X {decay_estimate(0.5, 2, 20) * 100:.1f}% vs -2X {decay_estimate(0.5, -2, 20) * 100:.1f}% (σ=50%)")

    r = evaluate_regime(cfg, {"SPY": smooth(seed=21), "^VIX": smooth(total=0.0, seed=22, start=17.0),
                              "069500.KS": smooth(seed=23)})
    show("국면 평가 동작", isinstance(r["US"]["long_ok"], bool),
         f"US: {r['US']['detail']} / KR: {r['KR']['detail']}")

    print("─" * 72)
    print(f"{sum(results)}/{len(results)} 통과")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
