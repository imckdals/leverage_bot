"""매매계획.

"이거 사라"만으로는 부족하다. 레버리지는 어디서 자를지를 먼저 정하지
않으면 3배가 하루에 -30% 를 만든다. 그래서 진입과 동시에 손절가·목표가·
시간 한도를 같이 계산한다.

손절 기준은 두 개를 비교해서 더 가까운 쪽을 쓴다.
  1) 구조적 손절 — 기초자산이 MA20 을 깨는 지점. 실제 청산 규칙이 걸리는 곳이다.
  2) 하드 손절   — 상품 기준 -20%. 구조적 손절이 너무 멀 때의 안전장치.
"""

from __future__ import annotations

import pandas as pd


def _fmt(x: float | None, market: str) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}" if market == "KR" else f"{x:,.2f}"


def make_plan(v, df: pd.DataFrame, cfg: dict) -> dict | None:
    """진입 신호가 난 종목에 대해 매매계획을 만든다."""
    if v.pick is None or v.pick_leverage is None or df is None or not len(df):
        return None

    ex = cfg["rules"]["exit"]
    row = df.iloc[-1]
    L = int(v.pick_leverage)
    long = L > 0

    u_price = float(row["close"])
    ma20 = float(row["ma20"])
    atrp = float(row["atr_pct"]) if not pd.isna(row["atr_pct"]) else 0.02

    # 1) 구조적 손절: MA20 을 ATR 반틈만큼 넘어선 지점.
    #    청산 규칙이 "2일 연속 이탈" 이라 확인 지연분을 버퍼로 둔다.
    u_stop = ma20 * (1 - 0.5 * atrp) if long else ma20 * (1 + 0.5 * atrp)
    u_move = u_stop / u_price - 1.0            # 롱이면 음수, 인버스면 양수
    struct_loss = L * u_move                   # 어느 쪽이든 음수로 나온다

    # 2) 하드 손절
    hard_loss = -abs(float(ex.get("stop_loss", 0.20)))

    # 더 가까운 쪽(덜 손해 보는 쪽)을 채택
    loss = max(struct_loss, hard_loss)
    if loss >= 0:                              # 이미 손절선 아래면 계획 불가
        return None
    used = "기초자산 MA20 이탈 기준" if loss == struct_loss else "상품 -20% 하드 손절"

    R = abs(loss)
    target_r = float(ex.get("target_r", 2.0))
    tp = ex.get("take_profit")

    price = v.pick.get("price")
    plan = {
        "leverage": L,
        "direction": "long" if long else "short",
        "entry": price,
        "entry_note": "다음 거래일 시가 근처. 종가 판정이라 갭이 크면 건너뛰세요.",
        "stop_pct": loss,
        "stop_price": None if price is None else price * (1 + loss),
        "stop_basis": used,
        "u_stop": u_stop,
        "target_pct": R * target_r,
        "target_price": None if price is None else price * (1 + R * target_r),
        "target_r": target_r,
        "target_is_rule": tp is not None,
        "proj_pct": v.pick_net,
        "proj_price": None if (price is None or v.pick_net is None)
                      else price * (1 + float(v.pick_net)),
        "time_stop_days": int(ex.get("time_stop_days", 20)),
        "rr": target_r,
    }

    pf = cfg.get("portfolio") or {}
    risk = float(pf.get("risk_per_trade", 0.02))
    cap_total = float(pf.get("max_position", 0.30))

    # 손절폭만으로 비중을 정하면 손절이 좁을 때 터무니없이 커진다.
    # 레버리지 상품은 갭 하락하면 손절가에 못 팔기 때문에, 배수로 나눈
    # 절대 상한을 같이 건다. 3배면 총자산의 10%, 2배면 15% 가 한도.
    by_risk = risk / R
    cap = cap_total / abs(L)
    size = min(by_risk, cap)

    plan["risk_per_trade"] = risk
    plan["size_by_risk"] = by_risk
    plan["size_cap"] = cap
    plan["position_pct"] = size
    plan["capped"] = bool(by_risk > cap)
    plan["gap_risk"] = abs(L) >= 3
    return plan


def plan_lines(v, plan: dict, market: str) -> list[str]:
    """텔레그램·콘솔용 문장."""
    if not plan:
        return []
    f = lambda x: _fmt(x, market)
    L = abs(plan["leverage"])
    out = [
        f"   매수      {f(plan['entry'])}  ({plan['entry_note'].split('.')[0]})",
        f"   손절      {f(plan['stop_price'])}   {plan['stop_pct'] * 100:+.1f}%"
        f"  ← {plan['stop_basis']}",
    ]
    tag = "익절" if plan["target_is_rule"] else "목표"
    out.append(f"   {tag}      {f(plan['target_price'])}   "
               f"{plan['target_pct'] * 100:+.1f}%  (손절폭의 {plan['target_r']:.0f}배)")
    if not plan["target_is_rule"]:
        out.append("             ※ 자동 청산 아님. 추세가 꺾일 때까지 들고 갑니다.")
    if plan.get("proj_price"):
        out.append(f"   추세 예상  {f(plan['proj_price'])}   {plan['proj_pct'] * 100:+.1f}%"
                   f"  (20거래일 기준)")
    out.append(f"   시간 한도  {plan['time_stop_days']}거래일 — 그때까지 못 벌면 청산")
    if plan.get("capped"):
        out.append(f"   투입 비중  총자산의 {plan['position_pct'] * 100:.0f}% 이하"
                   f"  ({L}배 상한. 손절폭 기준으로는 "
                   f"{plan['size_by_risk'] * 100:.0f}% 까지 나오지만 줄였습니다)")
    else:
        out.append(f"   투입 비중  총자산의 {plan['position_pct'] * 100:.0f}% 이하"
                   f"  (한 번에 {plan['risk_per_trade'] * 100:.0f}% 잃는 기준)")
    if plan.get("gap_risk"):
        out.append("             ※ 3배는 갭 하락 시 손절가에 못 팝니다. "
                   "손절선을 믿고 크게 넣지 마세요.")
    return out
