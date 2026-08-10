"""상태 저장소.

기초자산 하나당 기록 하나. 보유 중인 방향은 후보 방향과 별개로 기억한다.
롱을 들고 있는데 기초자산이 MA200 아래로 내려가면 후보 방향은 인버스가 되지만,
청산 판정은 여전히 들고 있는 롱 기준으로 해야 한다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from . import plan as plan_mod
from .engine import Verdict, check_exit

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

EMPTY: dict[str, Any] = {"last_run": None, "tickers": {}, "history": []}


def load_state() -> dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return json.loads(json.dumps(EMPTY))
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        for k, v in EMPTY.items():
            s.setdefault(k, json.loads(json.dumps(v)))
        return s
    except Exception:
        return json.loads(json.dumps(EMPTY))


def save_state(state: dict[str, Any]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _days_since(iso: str | None) -> int:
    if not iso:
        return 10_000
    try:
        return (dt.date.today() - dt.date.fromisoformat(iso[:10])).days
    except Exception:
        return 10_000


def apply(verdicts: list[Verdict], frames: dict, cfg: dict,
          state: dict[str, Any]) -> list[dict]:
    """상태를 갱신하고 이번 실행에서 새로 발생한 이벤트만 반환한다."""
    events: list[dict] = []
    today = dt.date.today().isoformat()
    notify = cfg["alerts"].get("notify_on", {})

    for v in verdicts:
        rec = state["tickers"].setdefault(
            v.id, {"status": "flat", "last_alert_date": None, "position": None})
        pos = rec.get("position")

        if pos:
            held = pos.get("direction", "long")
            df = frames.get(v.signal_u or v.id)
            reason = check_exit(df, held, pos, cfg) if df is not None else None
            if reason:
                v.status, v.direction, v.reason = "exit", held, reason
                v.products = pos.get("products", v.products)
                rec["position"] = None
                rec["last_alert_date"] = today
                if notify.get("exit", True):
                    events.append({"kind": "exit", "id": v.id, "name": v.name, "pick": v.pick,
                                   "group": v.group, "direction": held, "reason": reason,
                                   "price": v.price, "products": v.products,
                                   "held_days": _days_since(pos.get("opened"))})
            else:
                v.status, v.direction = "holding", held
                v.products = pos.get("products", v.products)
                v.reason = f"보유 중 · {_days_since(pos.get('opened'))}일차 · 이탈 조건 없음"
            rec["status"] = v.status
            continue

        if v.status == "entry":
            cd = int(cfg["rules"][v.direction]["cooldown_days"])
            if _days_since(rec.get("last_alert_date")) < cd:
                v.status = "watch"
                v.reason = f"조건은 충족했지만 쿨다운 {cd}일 이내라 신호 보류"
            else:
                rec["position"] = {"opened": today, "direction": v.direction,
                                   "price": v.price, "products": v.products,
                                   "pick": v.pick, "leverage": v.pick_leverage,
                                   "plan": v.plan,
                                   "atr_pct": _atr_of(frames, v.signal_u or v.id)}
                rec["last_alert_date"] = today
                if notify.get("entry", True):
                    events.append({"kind": "entry", "id": v.id, "name": v.name,
                                   "group": v.group, "direction": v.direction,
                                   "price": v.price, "products": v.products,
                                   "pick": v.pick, "plan": v.plan, "event": v.event,
                                   "plan_lines": (plan_mod.plan_lines(v, v.plan, v.market)
                                                  if v.plan else []),
                                   "passed": v.passed, "total": v.total})
        elif v.status == "watch" and rec.get("status") != "watch" and notify.get("watch", False):
            events.append({"kind": "watch", "id": v.id, "name": v.name,
                           "group": v.group, "direction": v.direction,
                           "products": v.products, "passed": v.passed,
                           "total": v.total, "reason": v.reason})

        rec["status"] = v.status

    state["last_run"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    state["history"] = (state.get("history", []) +
                        [{"ts": state["last_run"], **e} for e in events])[-300:]
    return events


def _atr_of(frames: dict, key: str) -> float | None:
    df = frames.get(key)
    if df is None or "atr_pct" not in df:
        return None
    try:
        return float(df["atr_pct"].iloc[-1])
    except Exception:
        return None
