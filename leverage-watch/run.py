#!/usr/bin/env python3
"""명령줄 실행.

  python run.py            점검 1회 실행
  python run.py --digest   보유 종목 요약을 지금 보내보기
  python run.py --chatid   텔레그램 chat_id 찾아주기
  python run.py --secrets  저장소를 공개해도 되는지 점검
  python run.py --test     알림 채널 연결 확인
  python run.py --audit    설정에 적힌 티커가 살아 있는지 전수 확인
  python run.py --calibrate 실제 데이터로 직진성 임계값 점검
  python run.py --reset    보유/쿨다운 기록 초기화
"""
import sys

from core import notify
from core.scan import VERSION, load_config, run_scan, audit, calibrate
from core.state import STATE_FILE, save_state


def main() -> int:
    print(f"레버리지 관제 버전 {VERSION}")
    cfg = load_config()
    if "--secrets" in sys.argv:
        bad = []
        tg = cfg["alerts"].get("telegram") or {}
        if (tg.get("bot_token") or "").strip():
            bad.append("alerts.telegram.bot_token")
        if (tg.get("chat_id") or "").strip():
            bad.append("alerts.telegram.chat_id")
        if ((cfg["alerts"].get("discord") or {}).get("webhook_url") or "").strip():
            bad.append("alerts.discord.webhook_url")
        if ((cfg.get("server") or {}).get("password") or "").strip():
            bad.append("server.password")
        if bad:
            print("공개하면 안 됩니다. config.yaml 에 비밀값이 들어 있습니다:")
            for b in bad:
                print(f"  · {b}")
            print()
            print('해당 값을 "" 로 비우고 GitHub Secrets 를 쓰세요.')
            return 1
        print("config.yaml 에 비밀값이 없습니다. 저장소를 공개해도 토큰은 새지 않습니다.")
        print()
        print("다만 공개하면 아래가 보입니다:")
        print("  · 감시 종목과 기준값 (config.yaml)")
        print("  · 언제 어떤 신호가 났는지 (state/state.json, STATUS.md)")
        print("  · 대시보드 (docs/index.html)")
        return 0
    if "--chatid" in sys.argv:
        notify.find_chat_id(cfg)
        return 0
    if "--test" in sys.argv:
        notify.send_test(cfg)
        return 0
    if "--audit" in sys.argv:
        audit(cfg)
        return 0
    if "--calibrate" in sys.argv:
        calibrate(cfg)
        return 0
    if "--reset" in sys.argv:
        save_state({"last_run": None, "tickers": {}, "history": []})
        print(f"초기화 완료: {STATE_FILE}")
        return 0
    run_scan(cfg, send_digest="--digest" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
