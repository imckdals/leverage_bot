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


def _simple_entry(e: dict) -> str:
    """짧은 매수 알림. 살 종목 하나와 팔 조건만 담는다."""
    pick = e.get("pick") or {}
    p = e.get("plan") or {}
    mk = e.get("market", "US")
    f = lambda x: "—" if x is None else (f"{x:,.0f}" if mk == "KR" else f"{x:,.2f}")

    lev = abs(int(pick.get("x", 1)))
    dirn = "롱" if int(pick.get("x", 1)) > 0 else "인버스"
    out = [f"[매수] {pick.get('t', '?')}  {lev}배 {dirn}",
           f"{e['name']}", ""]

    if pick.get("price") is not None:
        out.append(f"매수   {f(pick['price'])}")
    if p.get("stop_price") is not None:
        out.append(f"손절   {f(p['stop_price'])}  ({p['stop_pct'] * 100:+.1f}%)")
    if p.get("target_price") is not None:
        out.append(f"목표   {f(p['target_price'])}  ({p['target_pct'] * 100:+.1f}%)")
    if p.get("time_stop_days"):
        out.append(f"기한   {p['time_stop_days']}거래일")
    if p.get("position_pct") is not None:
        out.append(f"비중   총자산 {p['position_pct'] * 100:.0f}% 이하")

    out.append("")
    out.append("팔 때가 되면 따로 알립니다.")
    if pick.get("thin"):
        out.append("※ 거래대금이 얇으니 지정가로 나눠 사세요.")
    if pick.get("no_price"):
        out.append("※ 시세 미수신. HTS 에서 종목 확인 필요.")
    return "\n".join(out)


def format_event(e: dict, style: str = "simple") -> str:
    kind = e["kind"]

    if kind == "entry":
        if style == "simple" and e.get("pick"):
            return _simple_entry(e)
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
                lines.append("   ※ 시세를 받지 못해 대표 종목으로 표시했습니다.")
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
        return f"{head}\n조건 {e['passed']}/{e['total']} 충족\n" + _products(e)

    if kind == "exit":
        t = (e.get("pick") or {}).get("t")
        return (f"[매도] {t or e['name']}"
                + (f"  ({e['name']})" if t else "")
                + f"\n\n{e['reason']}\n지금 파세요. 보유 {e.get('held_days', 0)}일차.")

    return f"[관심] {e['name']}\n조건 {e['passed']}/{e['total']} · {e.get('reason', '')}"


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
    style = cfg["alerts"].get("style", "simple")
    _deliver(cfg, "\n\n".join(format_event(e, style) for e in events))


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
            import os
            miss = [k for k, v in (("bot_token", token), ("chat_id", chat)) if not v]
            print("=" * 58)
            print("텔레그램으로 못 보냈습니다. 비어 있는 값: " + ", ".join(miss))
            if os.environ.get("GITHUB_ACTIONS"):
                print("GitHub Actions 에서 실행 중입니다. Secrets 를 확인하세요:")
                print("  저장소 → Settings → Secrets and variables → Actions")
                print("  이름이 정확히 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 인지,")
                print("  대소문자와 밑줄까지 같은지 보세요. 오타면 조용히 빈 값이 됩니다.")
            else:
                print("config.yaml 의 alerts.telegram 을 채우거나,")
                print("환경변수 LEV_TG_TOKEN / LEV_TG_CHAT 를 설정하세요.")
            print("=" * 58)
            print(text)
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


def _minimal_digest(events: list[dict], asof: str) -> str:
    """살 것과 팔 것만. 없으면 없다고 한 줄.

    매일 오는 알림이라 짧아야 한다. 보유 현황이나 근접 종목 같은 건
    대시보드에서 보면 되고, 알림은 행동만 담는다.
    """
    buys = [e for e in events if e["kind"] == "entry"]
    sells = [e for e in events if e["kind"] == "exit"]

    if not buys and not sells:
        return f"[{asof}] 오늘 살 것도 팔 것도 없습니다."

    out = [f"[{asof}]"]
    for e in sells:
        pick = e.get("pick") or {}
        out.append("")
        out.append(f"■ 파세요   {pick.get('t') or e['name']}  ({e['name']})")
        out.append(f"   {e['reason']}")
    for e in buys:
        pick = e.get("pick") or {}
        p = e.get("plan") or {}
        mk = e.get("market", "US")
        f = lambda x: "—" if x is None else (f"{x:,.0f}" if mk == "KR" else f"{x:,.2f}")
        out.append("")
        out.append(f"■ 사세요   {pick.get('t', '?')}  "
                   f"{abs(int(pick.get('x', 1)))}배  ({e['name']})")
        out.append(f"   매수 {f(pick.get('price'))}  ·  손절 {f(p.get('stop_price'))}"
                   f"  ·  목표 {f(p.get('target_price'))}")
        out.append(f"   {p.get('time_stop_days', 20)}거래일 안에 정리 · "
                   f"총자산 {(p.get('position_pct') or 0) * 100:.0f}% 이하")
        if pick.get("thin"):
            out.append("   ※ 거래대금이 얇으니 지정가로 나눠 사세요.")
    return "\n".join(out)


def format_digest(items: list[dict], asof: str,
                  near: list[dict] | None = None,
                  stats: dict | None = None) -> str:
    """상세 요약. digest_style: full 일 때만 쓴다."""
    out: list[str] = []

    if items:
        out.append(f"[{asof}] 신호 기준 {len(items)}건")
        out.append("※ 실제 매수 여부와 무관하게 진입 신호가 난 종목을 추적합니다.")
        for it in items:
            r = it.get("ret")
            out.append("")
            out.append(f"■ {it['ticker']} {abs(int(it['leverage']))}배 · {it['name']}")
            if it["held"] == 0:
                out.append("   오늘 뜬 신호 (손익은 내일부터)")
            else:
                out.append(f"   신호 후   {r * 100:+.1f}%" if r is not None else "   신호 후   —")
                if it.get("to_stop") is not None:
                    out.append(f"   손절까지  {it['to_stop'] * 100:.1f}%p")
            out.append(f"   경과      {it['held']}/{it['time_stop']}거래일")
            out.append(f"   → {it['action']}")
    else:
        out.append(f"[{asof}] 잡고 있는 신호 없음")

    if near:
        out.append("")
        out.append("─ 조건에 가까운 종목 ─")
        for x in near:
            out.append(f"{x['name']} {x['dir']} {x['passed']}/{x['total']}"
                       f"  └ {x['miss']}")
    if stats:
        out.append("")
        out.append(f"점검 {stats['total']}종 · 진입 {stats['entry']} · 관심 {stats['watch']}")
    return "\n".join(out)


def send_digest(cfg: dict, items: list[dict], asof: str,
                near: list[dict] | None = None,
                stats: dict | None = None,
                events: list[dict] | None = None) -> None:
    mode = cfg["alerts"].get("daily_digest", "when_holding")
    if mode == "off":
        return
    if cfg["alerts"].get("digest_style", "minimal") == "minimal":
        _deliver(cfg, _minimal_digest(events or [], asof))
        return
    if mode == "when_holding" and not items and not near:
        return
    _deliver(cfg, format_digest(items, asof, near, stats))


def find_chat_id(cfg: dict) -> None:
    """봇 토큰만 있으면 chat_id 를 찾아준다.

    봇에게 먼저 말을 걸어야 한다. 텔레그램은 스팸 방지 때문에
    봇이 먼저 말을 못 걸게 막아놨고, 그래서 사용자가 한 번
    보내기 전까지는 대화방 자체가 존재하지 않는다.
    """
    import json as _json
    import os

    token = (os.environ.get("LEV_TG_TOKEN")
             or (cfg["alerts"].get("telegram") or {}).get("bot_token"))
    if not token:
        print("먼저 config.yaml 의 alerts.telegram.bot_token 을 채우세요.")
        print("@BotFather 에게 /newbot 이라고 보내면 토큰을 줍니다.")
        return

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = _json.loads(r.read().decode())
    except Exception as exc:
        print(f"텔레그램에 연결하지 못했습니다: {exc}")
        print("토큰이 맞는지, 인터넷이 되는지 확인하세요.")
        return

    if not data.get("ok"):
        print("토큰이 거부됐습니다:", data.get("description", "사유 불명"))
        return

    seen: dict[str, str] = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is None:
            continue
        name = (chat.get("title")
                or " ".join(x for x in (chat.get("first_name"),
                                        chat.get("last_name")) if x)
                or chat.get("username") or "이름 없음")
        seen[str(chat["id"])] = f"{name} ({chat.get('type', '?')})"

    if not seen:
        print("아직 대화 기록이 없습니다.")
        print()
        print("  1. 텔레그램에서 방금 만든 봇을 검색하세요")
        print("  2. 봇에게 아무 메시지나 하나 보내세요 (예: 안녕)")
        print("  3. 이 명령을 다시 실행하세요")
        print()
        print("봇이 먼저 말을 걸 수 없어서, 내가 한 번 보내야 대화방이 생깁니다.")
        return

    print("찾았습니다. config.yaml 의 chat_id 에 아래 숫자를 넣으세요.")
    print()
    for cid, who in seen.items():
        print(f"  {cid:<18} {who}")
    print()
    if len(seen) > 1:
        print("여러 개면 본인 이름으로 된 private 방을 쓰세요.")
    print("GitHub Actions 를 쓰신다면 Secret 이름은 TELEGRAM_CHAT_ID 입니다.")
