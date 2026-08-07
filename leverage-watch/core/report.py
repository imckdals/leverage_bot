"""현황을 마크다운으로 써서 저장소에 올린다.

핸드폰에서 비공개 저장소를 볼 때 가장 확실한 방법이다.
GitHub 앱이나 모바일 웹에서 파일을 누르면 표까지 그대로 렌더링된다.
서버도, 공개 전환도, 별도 서비스도 필요 없다.
"""

from __future__ import annotations

import datetime as dt
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "STATUS.md")

LABEL = {"entry": "🟡 매수", "exit": "🔴 매도", "holding": "🔵 보유",
         "watch": "근접", "flat": "관망", "none": "상품없음", "blocked": "보류"}


def _num(x, market: str) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}" if market == "KR" else f"{x:,.2f}"


def write(payload: dict, digest: list[dict], near: list[dict],
          stats: dict, version: str) -> str:
    items = payload.get("items", [])
    now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    L: list[str] = []
    L.append("# 레버리지 관제")
    L.append("")
    L.append(f"`{now}` · {stats.get('total', len(items))}종 점검 · v{version}")
    L.append("")

    # ── 오늘 할 일 ───────────────────────────────────────
    buys = [i for i in items if i["status"] == "entry"]
    sells = [i for i in items if i["status"] == "exit"]
    L.append("## 오늘 할 일")
    L.append("")
    if not buys and not sells:
        L.append("**살 것도 팔 것도 없습니다.**")
    for i in sells:
        pk = i.get("pick") or {}
        L.append(f"### 🔴 파세요 — {pk.get('t') or i['name']}")
        L.append("")
        L.append(f"{i['name']} · {i.get('reason', '')}")
        L.append("")
    for i in buys:
        pk, pl = i.get("pick") or {}, i.get("plan") or {}
        mk = i.get("market", "US")
        L.append(f"### 🟡 사세요 — {pk.get('t', '?')} "
                 f"{abs(int(pk.get('x', 1)))}배")
        L.append("")
        L.append(f"{i['name']} 기준")
        L.append("")
        L.append("| | |")
        L.append("|---|---|")
        L.append(f"| 매수 | {_num(pk.get('price'), mk)} |")
        L.append(f"| 손절 | {_num(pl.get('stop_price'), mk)} "
                 f"({(pl.get('stop_pct') or 0) * 100:+.1f}%) |")
        L.append(f"| 목표 | {_num(pl.get('target_price'), mk)} "
                 f"({(pl.get('target_pct') or 0) * 100:+.1f}%) |")
        L.append(f"| 기한 | {pl.get('time_stop_days', 20)}거래일 |")
        L.append(f"| 비중 | 총자산 {(pl.get('position_pct') or 0) * 100:.0f}% 이하 |")
        L.append("")
        if pk.get("thin"):
            L.append("> 거래대금이 얇습니다. 지정가로 나눠 사세요.")
            L.append("")

    # ── 보유 중 ─────────────────────────────────────────
    if digest:
        L.append("## 보유 중")
        L.append("")
        L.append("실제 매수 여부와 무관하게 신호가 난 종목을 추적합니다.")
        L.append("")
        L.append("| 상품 | 종목 | 손익 | 손절까지 | 경과 | |")
        L.append("|---|---|---|---|---|---|")
        for d in digest:
            r = "—" if d.get("ret") is None else f"{d['ret'] * 100:+.1f}%"
            ts = "—" if d.get("to_stop") is None else f"{d['to_stop'] * 100:.1f}%p"
            L.append(f"| **{d['ticker']}** {abs(int(d['leverage']))}배 | {d['name']} "
                     f"| {r} | {ts} | {d['held']}/{d['time_stop']}일 | {d['action']} |")
        L.append("")

    # ── 조건 근접 ───────────────────────────────────────
    if near:
        L.append("## 조건에 가까운 종목")
        L.append("")
        L.append("| 상품 | 방향 | 조건 | 막고 있는 것 |")
        L.append("|---|---|---|---|")
        for n in near:
            L.append(f"| {n['name']} | {n['dir']} | {n['passed']}/{n['total']} "
                     f"| {n['miss']} |")
        L.append("")

    # ── 전체 ────────────────────────────────────────────
    L.append("## 전체 종목")
    L.append("")
    L.append("| 종목 | 방향 | 상태 | 조건 | 직진성 | 20일 추세 vs 감가 |")
    L.append("|---|---|---|---|---|---|")
    for i in items:
        d = "▲" if i.get("direction") == "long" else "▼"
        er = "—" if i.get("er60") is None else f"{i['er60']:.2f}"
        g, dc = i.get("gain_20d"), i.get("decay_20d")
        edge = "—" if (g is None or dc is None) else f"{g * 100:+.1f}% / −{dc * 100:.1f}%"
        L.append(f"| {i['name']} | {d} | {LABEL.get(i['status'], i['status'])} "
                 f"| {i['passed']}/{i['total']} | {er} | {edge} |")
    L.append("")

    L.append("---")
    L.append("")
    L.append("**직진성**은 60일 순이동 ÷ 실제 이동거리입니다. "
             "높을수록 한 방향으로 곧게 간 것이고, 레버리지는 이때만 제값을 합니다.")
    L.append("")
    L.append("이 파일은 점검할 때마다 자동으로 갱신됩니다. "
             "투자 자문이 아니며 손실 책임은 사용자에게 있습니다.")

    text = "\n".join(L)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    return OUT
