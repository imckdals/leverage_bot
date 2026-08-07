#!/usr/bin/env python3
"""대시보드 서버.

  python server.py           →  http://127.0.0.1:8777
  python server.py --port 9000 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import os
import threading

import base64
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from core.scan import load_config, run_scan, read_latest

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "web", "index.html")

app = FastAPI(title="레버리지 관제")


@app.middleware("http")
async def _auth(request: Request, call_next):
    """config.yaml 의 server.password 가 채워져 있을 때만 인증을 건다.

    자기 컴퓨터에서만 볼 거면 비워두면 되고, 서버에 올려서 밖에서
    볼 거면 반드시 채우세요. 대시보드 자체에는 로그인이 없습니다.
    """
    pw = (load_config().get("server") or {}).get("password") or ""
    if not pw:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            if secrets.compare_digest(given, pw):
                return await call_next(request)
        except Exception:
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="leverage-watch"'})
_lock = threading.Lock()
_scanning = False


def _scan_job(digest: bool = False) -> None:
    global _scanning
    if not _lock.acquire(blocking=False):
        return
    try:
        _scanning = True
        run_scan(load_config(), quiet=True, send_digest=digest)
    except Exception as exc:  # 스케줄러가 죽지 않도록
        print(f"점검 실패: {exc}")
    finally:
        _scanning = False
        _lock.release()


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/latest")
def latest():
    data = read_latest()
    if data is None:
        return JSONResponse({"empty": True, "scanning": _scanning}, status_code=200)
    data["scanning"] = _scanning
    return JSONResponse(data)


@app.post("/api/scan")
def scan_now():
    if _scanning:
        return {"started": False, "message": "이미 점검 중입니다."}
    threading.Thread(target=_scan_job, daemon=True).start()
    return {"started": True}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-schedule", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    pw = (cfg.get("server") or {}).get("password") or ""
    if args.host != "127.0.0.1" and not pw:
        print("경고: 외부 접속을 열었는데 config.yaml 의 server.password 가 비어 있습니다.")
        print("      아무나 대시보드를 볼 수 있습니다. 비밀번호를 설정하세요.")

    if not args.no_schedule:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        tz = cfg["schedule"].get("timezone", "Asia/Seoul")
        sched = BackgroundScheduler(timezone=tz)
        dt_ = cfg["alerts"].get("digest_time")
        if dt_ and cfg["alerts"].get("daily_digest", "when_holding") != "off":
            h, m = dt_.split(":")
            sched.add_job(lambda: _scan_job(digest=True),
                          CronTrigger(day_of_week="mon-fri", hour=int(h),
                                      minute=int(m), timezone=tz))
            print(f"보유 요약 {dt_} ({tz})")

        for hhmm in cfg["schedule"].get("runs", []):
            h, m = hhmm.split(":")
            sched.add_job(_scan_job, CronTrigger(day_of_week="mon-sat",
                                                 hour=int(h), minute=int(m), timezone=tz))
            print(f"점검 예약 {hhmm} ({tz})")
        sched.start()

    if read_latest() is None:
        threading.Thread(target=_scan_job, daemon=True).start()
        print("첫 점검을 시작합니다. 데이터 수집에 1~2분 걸립니다.")

    import uvicorn
    print(f"대시보드 → http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
