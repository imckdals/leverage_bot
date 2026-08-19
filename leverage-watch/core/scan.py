"""점검 1회: 데이터 → 판정 → 상태 갱신 → 알림 → 대시보드 파일."""

from __future__ import annotations

import datetime as dt
import json
import os

import pandas as pd
import yaml

from . import events as events_mod
from . import (data, engine, notify, plan as plan_mod, publish,
               report, state as state_mod)

VERSION = "2026.08.07"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "config.yaml")
LATEST_FILE = os.path.join(ROOT, "state", "latest.json")

ORDER = {"exit": 0, "entry": 1, "holding": 2, "watch": 3,
         "flat": 4, "none": 5, "blocked": 6}
ACTIVE = ("exit", "entry", "holding", "watch")


def load_config(path: str = CONFIG_FILE) -> dict:
    """설정을 읽는다. 문법이 깨졌으면 어디가 문제인지 짚어준다."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        print("=" * 60)
        print("config.yaml 문법이 깨졌습니다.")
        if mark is not None:
            n = mark.line + 1
            print(f"위치: {n}번째 줄")
            print()
            lines = text.splitlines()
            for i in range(max(0, n - 3), min(len(lines), n + 2)):
                mk = " ←── 여기" if i == n - 1 else ""
                print(f"  {i + 1:>4} | {lines[i]}{mk}")
            print()
        if getattr(exc, "problem", None):
            print(f"사유: {exc.problem}")
        print()
        print("가장 흔한 원인:")
        print("  · 대괄호를 열고 안 닫음   → short: [[\"AMDD\", -1]]  (끝에 ]] 두 개)")
        print("  · 줄 끝에 쉼표만 남음     → [\"A\", 2],  뒤에 아무것도 없음")
        print("  · 들여쓰기가 어긋남       → 같은 항목끼리 칸 수를 맞추세요")
        print("=" * 60)
        raise SystemExit(1)


def run_scan(cfg: dict | None = None, quiet: bool = False,
             send_digest: bool = False) -> dict:
    cfg = cfg or load_config()

    # 감시 대상 그룹 좁히기. 비어 있으면 전부 본다.
    groups = cfg.get("watch_groups") or []
    universe = [u for u in cfg["universe"] if not groups or u["group"] in groups]
    if not universe:
        print("watch_groups 에 맞는 종목이 없습니다. config.yaml 을 확인하세요.")
        universe = cfg["universe"]
    cfg = dict(cfg, universe=universe)
    CFG_CACHE.clear(); CFG_CACHE.update(cfg)
    universe = cfg["universe"]

    tickers = [cfg["regime"]["us"]["index"], cfg["regime"]["us"]["vix"],
               cfg["regime"]["kr"]["index"]] + [engine.signal_ticker(it) for it in universe]

    want = sorted(set(tickers))
    if not quiet:
        print(f"[버전 {VERSION}] 감시 그룹 {groups or '전체'} · 기초자산 {len(universe)}종")
        print(f"기초자산 {len(want)}건 수집 …")
    frames = data.load_many(want)

    missing = [t for t in want if t not in frames]
    if missing and not quiet:
        print(f"  받지 못함 {len(missing)}건: {', '.join(missing[:8])}"
              + (f" 외 {len(missing) - 8}건" if len(missing) > 8 else ""))
        if len(missing) > len(want) * 0.5:
            print("  대부분 실패했습니다. 인터넷 연결이나 방화벽을 확인하세요.")

    regimes = engine.evaluate_regime(cfg, frames)
    verdicts = []
    for it in universe:
        sig = engine.signal_ticker(it)
        v = engine.evaluate(it, frames.get(sig), regimes, cfg)
        v.signal_u = sig
        verdicts.append(v)

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
                v.plan = plan_mod.make_plan(v, frames.get(v.signal_u or v.id), cfg)

    # 일정 회피: 종목 조건이 다 맞아도 실적이 코앞이면 진입시키지 않는다.
    # 실적은 갭으로 움직여서 손절가에 팔 수가 없다.
    for v in verdicts:
        if v.status not in ("entry", "holding"):
            continue
        try:
            v.event = events_mod.check(v, cfg)
        except Exception:
            continue
        if v.status == "entry" and v.event.get("block"):
            v.status = "watch"
            v.reason = v.event["reason"]
            v.pick = v.plan = None

    st = state_mod.load_state()
    events = state_mod.apply(verdicts, frames, cfg, st)
    state_mod.save_state(st)

    # 최소 형식 요약을 보낼 실행에서는 개별 알림을 생략한다.
    # 안 그러면 같은 내용이 두 번 간다.
    minimal_digest = (send_digest
                      and cfg["alerts"].get("digest_style", "minimal") == "minimal")
    if not minimal_digest:
        notify.send(cfg, events)

    # 보유 중 실적이 다가오면 알린다. 팔지 말지는 사용자가 정한다.
    warn_d = int((cfg.get("events") or {}).get("warn_days_before_earnings", 3))
    for v in verdicts:
        if v.status != "holding" or not v.event:
            continue
        d = v.event.get("d_earnings")
        if d is None or d > warn_d:
            continue
        pos = (st.get("tickers") or {}).get(v.id) or {}
        if (pos.get("position") or {}).get("earn_warned") == v.event.get("earnings"):
            continue
        events.append({"kind": "earnings_warn", "id": v.id, "name": v.name,
                       "ticker": (v.pick or {}).get("t") or v.name, "days": d})
        if pos.get("position"):
            pos["position"]["earn_warned"] = v.event.get("earnings")

    if events:
        state_mod.save_state(st)
        if not (send_digest and cfg["alerts"].get("digest_style", "minimal") == "minimal"):
            notify.send(cfg, [e for e in events if e["kind"] == "earnings_warn"])

    digest = build_digest(verdicts, frames, cfg, st)
    if send_digest:
        notify.send_digest(cfg, digest, dt.date.today().isoformat(),
                           _near_misses(verdicts), _coverage(verdicts), events)

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

    # 핸드폰에서 보려고 마크다운 현황도 같이 쓴다
    try:
        report.write(payload, digest, _near_misses(verdicts),
                     _coverage(verdicts), VERSION)
    except Exception as exc:
        print(f"현황 파일 생성 실패: {exc}")

    # GitHub Pages 용 단독 대시보드
    try:
        publish.build(payload, VERSION)
    except Exception as exc:
        print(f"대시보드 생성 실패: {exc}")

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
    under = [engine.signal_ticker(it) for it in cfg["universe"]]
    prods: list[tuple[str, str]] = []
    for it in cfg["universe"]:
        for side in ("long", "short"):
            for t, x in (it.get(side) or []):
                prods.append((t, it["name"]))

    names = {engine.signal_ticker(it): it["name"] for it in cfg["universe"]}
    print(f"기초자산 {len(under)}건 확인 …")
    ok_u, bad_u, thin_u = [], [], []
    for t in under:
        df = data.load(t, max_age_hours=24)
        if df is None or not len(df):
            bad_u.append(t)              # 티커가 틀렸거나 상장폐지
        elif len(df) < 210:
            thin_u.append((t, len(df)))  # 신규 상장. 시간이 지나면 해결된다
        else:
            ok_u.append(t)

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
        elif len(df) < 40:
            thin_p.append((t, owner, len(df)))
        else:
            ok_p.append(t)

    print()
    print(f"기초자산  정상 {len(ok_u)} · 실패 {len(bad_u)} · 이력부족 {len(thin_u)}")
    if bad_u:
        print("  데이터 없음 (티커 오류·상장폐지 의심) — 지우거나 고치세요:")
        for t in bad_u:
            print(f"    {t:10s} ← {names.get(t, '')}")
    if thin_u:
        print("  이력 부족 — 지우지 마세요. 210일이 쌓이면 자동으로 판정에 들어옵니다:")
        for t, n in thin_u:
            left = 210 - n
            print(f"    {t:10s} ← {names.get(t, '')}  ({n}일, {left}일 더 필요)")
    print(f"상품      정상 {len(ok_p)} · 실패 {len(bad_p)} · 이력부족 {len(thin_p)}")
    if bad_p:
        print("  데이터 없음 (상장폐지·티커변경 의심):")
        for t, owner in bad_p:
            print(f"    {t:8s} ← {owner}")
    if thin_p:
        print("  이력 부족 (신규상장 의심):")
        for t, owner, n in thin_p:
            print(f"    {t:8s} ← {owner}  ({n}일)")
    print()
    print("'데이터 없음' 만 config.yaml 에서 지우면 됩니다.")
    print("'이력 부족' 은 신규 상장이라 그런 것이니 그대로 두세요.")
    return {"bad_underlyings": bad_u, "bad_products": bad_p, "thin_products": thin_p}


def calibrate(cfg: dict | None = None) -> None:
    """실제 데이터에서 직진성 지표가 어떻게 분포하는지 보여준다.

    합성 데이터로 정한 기본 임계값(er_min 0.15 / r2_min 0.45)이
    실제 시장에서 너무 빡빡하거나 헐거우면 여기 숫자를 보고 고친다.
    """
    import numpy as np

    cfg = cfg or load_config()
    tickers = [engine.signal_ticker(it) for it in cfg["universe"]]
    print(f"기초자산 {len(tickers)}건의 최근 지표 수집 …")
    frames = data.load_many(tickers)

    rows = []
    for it in cfg["universe"]:
        df = frames.get(engine.signal_ticker(it))
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


CFG_CACHE: dict = {}


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

    # 가격 상한. 소액으로 나눠 담으려면 1주 단가가 낮아야 한다.
    # 단, 판정에는 쓰지 않는다. 레버리지 ETF 가격이 낮다는 건
    # 대개 '싸다'가 아니라 '이미 많이 녹았다'는 뜻이기 때문이다.
    cap = (CFG_CACHE.get("portfolio") or {}).get("max_share_price")
    if cap and priced:
        cheap = [p for p in priced if (p.get("price") or 0) <= float(cap)]
        if cheap:
            priced = cheap        # 조건 맞는 게 있으면 그 안에서만 고른다

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


SINGLE = ("미국 개별주", "한국 단일종목")


def _near_misses(verdicts, limit: int = 6) -> list[dict]:
    """조건에 가까운 종목. 개별주를 먼저 보여준다.

    "왜 개별주는 신호가 안 오지" 를 화면 없이 알 수 있게 하려는 것이다.
    막고 있는 게 무엇인지까지 같이 담는다.
    """
    cand = [v for v in verdicts if v.status == "watch"]
    cand.sort(key=lambda v: (v.group not in SINGLE, -(v.passed / max(v.total, 1))))
    out = []
    for v in cand[:limit]:
        miss = [g.label for g in v.gates if not g.passed]
        out.append({
            "name": (v.pick or {}).get("t") or v.name,
            "dir": "롱" if v.direction == "long" else "인버스",
            "passed": v.passed, "total": v.total,
            "miss": ", ".join(miss[:2]) + (f" 외 {len(miss) - 2}" if len(miss) > 2 else ""),
        })
    return out


def _coverage(verdicts) -> dict:
    """무엇을 몇 종 점검했고 몇 개가 통과했는지."""
    single = [v for v in verdicts if v.group in SINGLE]
    return {
        "total": len(verdicts),
        "entry": sum(1 for v in verdicts if v.status == "entry"),
        "watch": sum(1 for v in verdicts if v.status == "watch"),
        "single_total": len(single),
        "single_entry": sum(1 for v in single if v.status == "entry"),
        "single_watch": sum(1 for v in single if v.status == "watch"),
        "holding": sum(1 for v in verdicts if v.status == "holding"),
    }


def build_digest(verdicts, frames, cfg, st) -> list[dict]:
    """보유 중인 종목의 오늘 상태."""
    items = []
    for v in verdicts:
        rec = (st.get("tickers") or {}).get(v.id) or {}
        pos = rec.get("position")
        if not pos:
            continue
        df = frames.get(v.signal_u or v.id)
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
