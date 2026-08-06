"""알림 전송. 텔레그램 / 디스코드 / 콘솔."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

ARROW = {"long": "▲ 롱", "short": "▼ 인버스"}


def _products(e: dict, limit: int = 4) -> str:
    ps = e.get("products") or []
    if not ps:
        return ""
    head = ps[:limit]
    s = " · ".join(f"{p['t']} {abs(p['x'])}x" for p in head)
    return s + (f" 외 {len(ps) - limit}종" if len(ps) > limit else "")


def format_event(e: dict) -> str:
    kind = e["kind"]
    if kind == "entry":
        pick = e.get("pick")
        head = f"[진입] {e['name']}"
        if pick:
            lines = [head,
                     f"→ {pick['t']}  {abs(int(pick['x']))}배 "
                     f"{'롱' if int(pick['x']) > 0 else '인버스'}"]
            if pick.get("price") is not None:
                lines.append(f"   현재가 {pick['price']:,.2f}")
            lines.append(f"   {pick.get('why', '')}")
            if pick.get("thin"):
                lines.append("   ※ 거래대금이 얇습니다. 지정가로 나눠 사세요.")
            if pick.get("no_price"):
                lines.append("   ※ 시세를 받지 못해 대표 종목으로 표시했습니다. HTS 확인 필요.")
            if e.get("plan_lines"):
                lines.append("")
                lines += e["plan_lines"]
                lines.append("")
            lines.append(f"   조건 {e['passed']}/{e['total']} 충족")
            alt = [p for p in (e.get("products") or []) if p["t"] != pick["t"]]
            if alt:
                lines.append("   대체: " + ", ".join(
                    f"{p['t']} {abs(int(p['x']))}배" for p in alt[:4]))
            return "\n".join(lines)
        return (f"{head}\n조건 {e['passed']}/{e['total']} 충족\n"
                + "상품: " + ", ".join(f"{p['t']} {abs(int(p['x']))}배"
                                      for p in (e.get("products") or [])[:4]))
    if kind == "exit":
        return (f"[청산] {e['name']}"
                + (f"  {e['pick']['t']}" if e.get("pick") else "")
                + f"\n사유: {e['reason']}\n보유 {e.get('held_days', 0)}일차")
    return (f"[관심] {e['name']}\n조건 {e['passed']}/{e['total']} · {e.get('reason', '')}")


def _post(url: str, payload: dict, form: bool = False) -> bool:
    try:
        if form:
            req = urllib.request.Request(url, data=urllib.parse.urlencode(payload).encode())
        else:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as exc:
        print(f"  알림 전송 실패: {exc}")
        return False


def send(cfg: dict, events: list[dict]) -> None:
    if not events:
        return
    _deliver(cfg, "\n\n".join(format_event(e) for e in events))


def _creds(cfg: dict) -> tuple[str, dict]:
    """알림 설정. 환경변수가 있으면 config.yaml 보다 우선한다.

    붙여넣기로 들어온 값에는 줄바꿈이나 공백이 섞이는 일이 흔하다.
    그대로 URL 에 넣으면 요청이 통째로 실패하므로 반드시 털어낸다.
    """
    import os

    def clean(x) -> str:
        return str(x).strip().strip('"').strip("'") if x else ""

    ch = clean(os.environ.get("LEV_CHANNEL")) or cfg["alerts"].get("channel", "console")
    tg = {k: clean(v) for k, v in (cfg["alerts"].get("telegram") or {}).items()}
    if os.environ.get("LEV_TG_TOKEN"):
        tg["bot_token"] = clean(os.environ["LEV_TG_TOKEN"])
    if os.environ.get("LEV_TG_CHAT"):
        tg["chat_id"] = clean(os.environ["LEV_TG_CHAT"])

    dc = {k: clean(v) for k, v in (cfg["alerts"].get("discord") or {}).items()}
    if os.environ.get("LEV_DISCORD_WEBHOOK"):
        dc["webhook_url"] = clean(os.environ["LEV_DISCORD_WEBHOOK"])

    if ch == "console" and tg.get("bot_token") and tg.get("chat_id"):
        ch = "telegram"
    return ch, {"telegram": tg, "discord": dc}


def _deliver(cfg: dict, text: str) -> None:
    ch, creds = _creds(cfg)

    if ch == "telegram":
        tg = creds["telegram"]
        token, chat = tg.get("bot_token"), tg.get("chat_id")
        if not token or not chat:
            print("텔레그램 설정이 비어 있어 콘솔로 출력합니다.\n" + text)
            return
        # 텔레그램 메시지 상한(4096자)을 넘으면 나눠 보낸다
        for chunk in _split(text, 3800):
            _post(f"https://api.telegram.org/bot{token}/sendMessage",
                  {"chat_id": chat, "text": chunk}, form=True)
    elif ch == "discord":
        url = creds["discord"].get("webhook_url")
        if not url:
            print("디스코드 웹훅이 비어 있어 콘솔로 출력합니다.\n" + text)
            return
        for chunk in _split(text, 1900):
            _post(url, {"content": chunk})
    else:
        print(text)


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for block in text.split("\n\n"):
        if len(buf) + len(block) + 2 > limit and buf:
            out.append(buf)
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        out.append(buf)
    return out


def send_test(cfg: dict) -> None:
    send(cfg, [{"kind": "watch", "name": "연결 테스트", "group": "설정 확인",
                "direction": "long", "passed": 0, "total": 9,
                "reason": "이 메시지가 보이면 알림 설정 완료"}])


def format_digest(items: list[dict], asof: str) -> str:
    """보유 중인 종목의 매일 상태. 신호가 없어도 이건 나간다."""
    if not items:
        return f"[{asof}] 보유 없음. 조건 맞는 종목이 없습니다."

    out = [f"[{asof}] 보유 {len(items)}건"]
    for it in items:
        r = it.get("ret")
        out.append("")
        out.append(f"■ {it['name']}  {it['ticker']} {abs(int(it['leverage']))}배")
        out.append(f"   손익      {r * 100:+.1f}%" if r is not None else "   손익      —")
        if it.get("to_stop") is not None:
            out.append(f"   손절까지  {it['to_stop'] * 100:.1f}%p 남음")
        if it.get("to_target") is not None:
            out.append(f"   목표까지  {it['to_target'] * 100:.1f}%p 남음")
        out.append(f"   경과      {it['held']}거래일 / 한도 {it['time_stop']}일")
        out.append(f"   지금은    {it['action']}")
    return "\n".join(out)


def send_digest(cfg: dict, items: list[dict], asof: str) -> None:
    mode = cfg["alerts"].get("daily_digest", "when_holding")
    if mode == "off" or (mode == "when_holding" and not items):
        return
    _deliver(cfg, format_digest(items, asof))
