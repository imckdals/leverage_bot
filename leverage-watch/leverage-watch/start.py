#!/usr/bin/env python3
"""처음 시작할 때 이 파일 하나만 실행하세요.

    python start.py

설치 → 검사 → 첫 점검 → 대시보드까지 알아서 진행합니다.
중간에 막히면 무엇이 문제인지 한국어로 알려줍니다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PKGS = ["pandas", "numpy", "yaml", "yfinance", "fastapi", "uvicorn", "apscheduler"]


def line(ch="─"):
    print(ch * 62)


def step(n, total, title):
    print()
    line()
    print(f"  {n}/{total}  {title}")
    line()


def die(msg, hint=""):
    print()
    print("  ✕ " + msg)
    if hint:
        for h in hint.split("\n"):
            print("    " + h)
    print()
    if os.name == "nt":
        input("  엔터를 누르면 창이 닫힙니다. ")
    sys.exit(1)


def main():
    os.chdir(HERE)
    print()
    print("  레버리지 관제 — 처음 실행")
    print("  기초자산 59개 · 레버리지 상품 152종을 감시합니다.")

    # ── 1. 파이썬 버전 ────────────────────────────────────
    step(1, 5, "파이썬 확인")
    v = sys.version_info
    print(f"  현재 버전 {v.major}.{v.minor}.{v.micro}")
    if (v.major, v.minor) < (3, 10):
        die(f"파이썬 3.10 이상이 필요합니다 (지금 {v.major}.{v.minor}).",
            "python.org/downloads 에서 최신 버전을 설치하세요.\n"
            "Windows 라면 설치 첫 화면의 'Add python.exe to PATH' 를 꼭 체크하세요.")
    print("  통과")

    # ── 2. 패키지 설치 ────────────────────────────────────
    step(2, 5, "필요한 패키지 설치")
    missing = []
    for p in PKGS:
        try:
            __import__(p)
        except ImportError:
            missing.append(p)

    if not missing:
        print("  이미 다 설치돼 있습니다.")
    else:
        print(f"  {len(missing)}개가 없어서 설치합니다. 1~2분 걸립니다 …")
        print()
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                            "-r", "requirements.txt"])
        if r.returncode != 0:
            die("설치에 실패했습니다.",
                "아래 명령을 직접 실행해 보고 나오는 메시지를 확인하세요.\n"
                f"  {sys.executable} -m pip install -r requirements.txt")
        print("  설치 완료")

    # ── 3. 규칙 검사 ──────────────────────────────────────
    step(3, 5, "규칙이 제대로 도는지 검사 (인터넷 불필요)")
    r = subprocess.run([sys.executable, "selftest.py"],
                       capture_output=True, text=True)
    tail = [l for l in r.stdout.strip().split("\n") if l.strip()][-1:]
    print("  " + (tail[0] if tail else "출력 없음"))
    if r.returncode != 0:
        print()
        print(r.stdout[-1500:])
        die("규칙 검사에 실패했습니다.",
            "이 상태로는 신호를 믿을 수 없습니다. 위 출력을 알려주세요.")
    print("  통과")

    # ── 4. 첫 점검 ────────────────────────────────────────
    step(4, 5, "첫 점검 — 시세를 받아옵니다")
    print("  티커 200개를 처음 받으므로 3~6분 걸립니다.")
    print("  (다음부터는 캐시를 써서 훨씬 빠릅니다)")
    print()
    t0 = time.time()
    r = subprocess.run([sys.executable, "run.py"], capture_output=True, text=True)
    out = r.stdout.strip()
    for l in out.split("\n"):
        print("  " + l)

    if "받지 못함" in out and "대부분 실패" in out:
        die("시세를 거의 받지 못했습니다.",
            "인터넷 연결이나 방화벽 문제일 가능성이 큽니다.\n"
            "회사 네트워크라면 개인 네트워크에서 다시 시도해 보세요.")
    print()
    print(f"  {time.time() - t0:.0f}초 걸렸습니다.")
    print("  '진입 0' 이 정상입니다. 조건이 다 맞는 드문 날에만 숫자가 올라갑니다.")

    # ── 5. 대시보드 ───────────────────────────────────────
    step(5, 5, "대시보드 실행")
    url = "http://127.0.0.1:8777"
    print(f"  주소: {url}")
    print("  브라우저가 자동으로 열립니다. 안 열리면 위 주소를 직접 입력하세요.")
    print()
    print("  이 창을 켜둔 동안만 동작합니다. 끄려면 Ctrl+C 를 누르세요.")
    print("  한국장 15:45, 미국장 06:10 에 자동으로 다시 점검합니다.")
    print()
    print("  다음에 할 만한 것:")
    print("    python run.py --calibrate    직진성 기준값이 실제 시장과 맞는지 확인")
    print("    python backtest.py           이 규칙이 과거에 돈을 벌었는지 검증")
    print("    실행방법.md 7단계             휴대폰 텔레그램 알림 연결")
    line()
    print()

    try:
        import threading
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    try:
        subprocess.run([sys.executable, "server.py"])
    except KeyboardInterrupt:
        print("\n  종료했습니다. 다시 켜려면 python server.py 를 실행하세요.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  중단했습니다.\n")
