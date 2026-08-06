"""점검 1회: 데이터 → 판정 → 상태 갱신 → 알림 → 대시보드 파일."""

from __future__ import annotations

import datetime as dt
import json
import os

import pandas as pd
import yaml

from . import data, engine, notify, plan as plan_mod, state as state_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "config.yaml")
LATEST_FILE = os.path.join(ROOT, "state", "latest.json")

ORDER = {"exit": 0, "entry": 1, "holding": 2, "watch": 3,
         "flat": 4, "none": 5, "blocked": 6}
ACTIVE = ("exit", "entry", "holding", "watch")


def load_config(path: str = CONFIG_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_scan(cfg: dict | None = None, quiet: bool = False,
             send_digest: bool = False) -> dict:
    cfg = cfg or load_config()
    universe = cfg["universe"]

    tickers = [cfg["regime"]["us"]["index"], cfg["regime"]["us"]["vix"],
               cfg["regime"]["kr"]["index"]] + [it["u"] for it in universe]

    want = sorted(set(tickers))
    if not quiet:
        print(f"기초자산 {len(want)}건 수집 …")
    frames = data.load_many(want)

    missing = [t for t in want if t not in frames]
    if missing and not quiet:
        print(f"  받지 못함 {len(missing)}건: {', '.join(missing[:8])}"
              + (f" 외 {len(missing) - 8}건" if len(missing) > 8 else ""))
        if len(missing) > len(want) * 0.5:
            print("  대부분 실패했습니다. 인터넷 연결이나 방화벽을 확인하세요.")

    regimes = engine.evaluate_regime(cfg, frames)
    verdicts = [engine.evaluate(it, frames.get(it["u"]), regimes, cfg) for it in universe]

    # 알림보다 먼저 상품 시세를 받는다. 알림에 "이거 사라"고 티커를 하나
    # 찍어 보내려면 가격과 거래대금이 있어야 한다.
    live = [v for v in verdicts if v.status in ACTIVE]
    if live:
        pw = {p["t"] for v in live for p in v.products}
        if not quiet and pw:
            print(f"상품 시세 {len(pw)}건 추가 수집 …")
        pf = data.load_many(sorted(pw), years=1)
        for v in live:
            for p in v.products:
                d = pf.get(p["t"])
                if d is not None and len(d):
                    p["price"] = float(d["close"].iloc[-1])
                    if len(d) > 1:
                        p["change_pct"] = float(d["close"].iloc[-1] / d["close"].iloc[-2] - 1) * 100
                    # 20일 평균 거래대금. 같은 배수 상품 중 뭘 고를지의 기준.
                    try:
                        dv = (d["close"] * d["volume"]).tail(20).mean()
                        p["turnover"] = None if pd.isna(dv) else float(dv)
                    except Exception:
                        p["turnover"] = None
                else:
                    p["missing"] = True
            _pick_product(v)
            if v.status == "entry" and v.pick:
                v.plan = plan_mod.make_plan(v, frames.get(v.id), cfg)

    st = state_mod.load_state()
    events = state_mod.apply(verdicts, frames, cfg, st)
    state_mod.save_state(st)
    notify.send(cfg, events)

    digest = build_digest(verdicts, frames, cfg, st)
    if send_digest:
        near = [v.pick["t"] if v.pick else v.name
                for v in verdicts if v.status == "watch"][:6]
        notify.send_digest(cfg, digest, dt.date.today().isoformat(), near)

    verdicts.sort(key=lambda v: (ORDER.get(v.status, 9), -v.passed, v.group, v.name))
    groups: list[str] = []
    for it in universe:
        if it["group"] not in groups:
            groups.append(it["group"])

    payload = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "regime": regimes,
        "groups": groups,
        "items": [v.to_dict() for v in verdicts],
        "events": events,
        "history": st.get("history", [])[-40:][::-1],
        "digest": digest,
        "counts": {k: sum(1 for v in verdicts if v.status == k) for k in ORDER},
        "product_count": sum(len(it.get("long") or []) + len(it.get("short") or [])
                             for it in universe),
    }

    os.makedirs(os.path.dirname(LATEST_FILE), exist_ok=True)
    tmp = LATEST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LATEST_FILE)

    if not quiet:
        c = payload["counts"]
        print(f"진입 {c['entry']} · 이탈 {c['exit']} · 보유 {c['holding']} · "
              f"관심 {c['watch']} · 관망 {c['flat']} · 상품없음 {c['none']} · 보류 {c['blocked']}")
        if not events:
            print("새 알림 없음.")
    return payload


def audit(cfg: dict | None = None) -> dict:
    """설정에 적힌 티커가 실제로 데이터를 주는지 전수 확인한다."""
    cfg = cfg or load_config()
    under = [it["u"] for it in cfg["universe"]]
    prods: list[tuple[str, str]] = []
    for it in cfg["universe"]:
        for side in ("long", "short"):
            for t, x in (it.get(side) or []):
                prods.append((t, it["name"]))

    print(f"기초자산 {len(under)}건 확인 …")
    ok_u, bad_u = [], []
    for t in under:
        df = data.load(t, max_age_hours=24)
        (ok_u if df is not None and len(df) >= 210 else bad_u).append(t)

    print(f"레버리지 상품 {len(prods)}건 확인 …")
    ok_p, bad_p, thin_p = [], [], []
    seen = set()
    for t, owner in prods:
        if t in seen:
            continue
        seen.add(t)
        df = data.load(t, years=1, max_age_hours=24)
        if df is None or not len(df):
            bad_p.append((t, owner))
        elif len(df) < 60:
            thin_p.append((t, owner, len(df)))
        else:
            ok_p.append(t)

    print()
    print(f"기초자산  정상 {len(ok_u)} · 실패 {len(bad_u)}")
    if bad_u:
        print("  실패: " + ", ".join(bad_u))
    print(f"상품      정상 {len(ok_p)} · 실패 {len(bad_p)} · 이력부족 {len(thin_p)}")
    if bad_p:
        print("  데이터 없음 (상장폐지·티커변경 의심):")
        for t, owner in bad_p:
            print(f"    {t:8s} ← {owner}")
    if thin_p:
        print("  이력 부족 (신규상장 의심):")
        for t, owner, n in thin_p:
            print(f"    {t:8s} ← {owner}  ({n}일)")
    print("\n실패한 티커는 config.yaml 에서 지우거나 현재 티커로 바꾸세요.")
    return {"bad_underlyings": bad_u, "bad_products": bad_p, "thin_products": thin_p}


def calibrate(cfg: dict | None = None) -> None:
    """실제 데이터에서 직진성 지표가 어떻게 분포하는지 보여준다.

    합성 데이터로 정한 기본 임계값(er_min 0.15 / r2_min 0.45)이
    실제 시장에서 너무 빡빡하거나 헐거우면 여기 숫자를 보고 고친다.
    """
    import numpy as np

    cfg = cfg or load_config()
    tickers = [it["u"] for it in cfg["universe"]]
    print(f"기초자산 {len(tickers)}건의 최근 지표 수집 …")
    frames = data.load_many(tickers)

    rows = []
    for it in cfg["universe"]:
        df = frames.get(it["u"])
        if df is None or len(df) < 210:
            continue
        r = df.iloc[-1]
        if pd.isna(r["er60"]) or pd.isna(r["r2_60"]):
            continue
        rows.append((it["name"], float(r["er60"]), float(r["r2_60"]),
                     float(r["rvol20"]), float(r["close"] > r["ma200"])))

    if not rows:
        print("데이터를 받지 못했습니다.")
        return

    er = np.array([x[1] for x in rows])
    r2 = np.array([x[2] for x in rows])
    s = cfg["rules"]["straight"]

    print()
    print(f"{'지표':<10}{'최소':>7}{'25%':>7}{'중앙':>7}{'75%':>7}{'90%':>7}{'최대':>7}")
    print("─" * 52)
    for nm, arr in (("직진성 ER", er), ("경로 R²", r2)):
        q = np.percentile(arr, [0, 25, 50, 75, 90, 100])
        print(f"{nm:<10}" + "".join(f"{v:>7.2f}" for v in q))

    pass_er = int((er >= s["er_min"]).sum())
    pass_r2 = int((r2 >= s["r2_min"]).sum())
    pass_both = int(((er >= s["er_min"]) & (r2 >= s["r2_min"])).sum())
    n = len(rows)
    print()
    print(f"현재 임계값 er_min={s['er_min']} · r2_min={s['r2_min']}")
    print(f"  직진성 통과 {pass_er}/{n} · 경로 통과 {pass_r2}/{n} · 둘 다 {pass_both}/{n}")
    print()
    print("직진성 상위 10건 (지금 가장 '쭉' 가고 있는 기초자산):")
    for nm, e, r, v, up in sorted(rows, key=lambda x: -x[1])[:10]:
        print(f"  {nm:<22} ER {e:.2f}  R² {r:.2f}  변동성 {v * 100:>3.0f}%  "
              f"{'상승' if up else '하락'}")
    print()
    print("둘 다 통과가 0건이면 임계값이 너무 높습니다. 전체의 10~20%가")
    print("통과하는 수준으로 config.yaml 의 er_min / r2_min 을 맞추세요.")


# 거래대금 하한. 통화가 다르므로 시장별로 따로 둔다.
#   미국: 하루 500만 달러 미만이면 호가가 벌어져 레버리지 상품은 손해가 크다
#   한국: 하루 10억 원 미만이면 같은 이유
THIN = {"US": 5e6, "KR": 1e9}


def _pick_product(v) -> None:
    """살 종목을 하나로 좁힌다.

    1. 감가를 감당할 수 있는 배수만 후보로 둔다 (engine 이 계산해 둔 값)
    2. 그 배수의 상품 중 거래대금이 가장 큰 것을 고른다.
       같은 기초자산에 운용사가 여럿 붙는데, 호가가 얇은 쪽은
       사고팔 때 스프레드로 손해를 본다.
    3. 시세를 못 받은 경우(신규 상장 등) 설정 파일 순서상 첫 번째를 쓴다.
       config 는 규모가 큰 운용사 순으로 적어뒀다.
    """
    if v.pick_leverage is None:
        return
    same = [p for p in v.products if int(p["x"]) == int(v.pick_leverage)]
    if not same:
        same = list(v.products)
    if not same:
        return

    priced = [p for p in same if not p.get("missing") and p.get("turnover")]
    if priced:
        priced.sort(key=lambda p: -(p.get("turnover") or 0))
        top, no_price = priced[0], False
    else:
        top, no_price = same[0], True

    floor = THIN.get(v.market, 5e6)
    turn = top.get("turnover")
    thin = bool(turn is not None and turn < floor)

    why = f"{abs(int(v.pick_leverage))}배 · 감가 감당 가능"
    if len(same) > 1:
        why += (f" · 같은 배수 {len(same)}종 중 "
                + ("거래대금 1위" if not no_price else "대표 종목"))
    if v.pick_net is not None:
        why += f" · 20일 기대 순이익 {v.pick_net * 100:+.1f}%"

    v.pick = {"t": top["t"], "x": int(top["x"]), "price": top.get("price"),
              "change_pct": top.get("change_pct"), "turnover": turn,
              "thin": thin, "no_price": no_price, "why": why}


def build_digest(verdicts, frames, cfg, st) -> list[dict]:
    """보유 중인 종목의 오늘 상태."""
    items = []
    for v in verdicts:
        rec = (st.get("tickers") or {}).get(v.id) or {}
        pos = rec.get("position")
        if not pos:
            continue
        df = frames.get(v.id)
        if df is None:
            continue
        ret = engine.position_return(df, pos)
        plan = pos.get("plan") or {}
        held = 0
        try:
            held = len(df.loc[str(pos["opened"]):]) - 1
        except Exception:
            pass

        stop_pct = plan.get("stop_pct")
        tgt_pct = plan.get("target_pct")
        to_stop = None if (ret is None or stop_pct is None) else ret - stop_pct
        to_tgt = None if (ret is None or tgt_pct is None) else tgt_pct - ret

        if v.status == "exit":
            action = "지금 파세요"
        elif to_stop is not None and to_stop < 0.05:
            action = "손절선 근접 — 깨지면 파세요"
        elif to_tgt is not None and to_tgt <= 0:
            action = "목표 도달 — 추세 꺾이면 정리"
        else:
            action = "아직 파는 조건 없음"

        items.append({
            "name": v.name, "ticker": (pos.get("pick") or {}).get("t", "—"),
            "leverage": pos.get("leverage") or 1, "ret": ret,
            "to_stop": to_stop, "to_target": to_tgt, "held": held,
            "time_stop": int(cfg["rules"]["exit"].get("time_stop_days", 20)),
            "action": action,
        })
    return items


def read_latest() -> dict | None:
    if not os.path.exists(LATEST_FILE):
        return None
    try:
        with open(LATEST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
