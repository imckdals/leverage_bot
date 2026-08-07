"""점검 결과를 박아 넣은 단독 HTML 을 만든다.

서버 없이 브라우저만으로 열리는 파일이다. docs/index.html 로 쓰면
GitHub Pages 가 그대로 웹페이지로 띄워준다.

주의: Pages 는 무료 플랜에서 공개 저장소만 지원한다. 이 파일에는
알림 토큰이 들어가지 않지만, 어떤 신호가 언제 났는지는 공개된다.
"""

from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "web", "index.html")
OUT_DIR = os.path.join(ROOT, "docs")
OUT = os.path.join(OUT_DIR, "index.html")


def build(payload: dict, version: str) -> str | None:
    if not os.path.exists(TEMPLATE):
        return None

    html = open(TEMPLATE, encoding="utf-8").read()

    data = dict(payload)
    data.pop("events", None)          # 알림 원문은 굳이 싣지 않는다

    inject = (
        "const BAKED = " + json.dumps(data, ensure_ascii=False) + ";\n"
        "render(BAKED);\n"
        "document.getElementById('stamp').textContent =\n"
        "  new Date(BAKED.generated_at).toLocaleString('ko-KR',"
        "{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})"
        f" + ' 점검 · v{version}';\n"
        "const b = document.getElementById('scanBtn');\n"
        "b.textContent = '자동 갱신'; b.disabled = true;\n"
    )

    # 서버에 붙는 부분(refresh/setInterval)을 박아 넣은 데이터로 대체
    m = re.search(r"refresh\(\);\s*\nsetInterval\(refresh,\s*\d+\);", html)
    if not m:
        return None
    html = html[:m.start()] + inject + html[m.end():]

    # 수동 점검 버튼은 서버가 없으니 동작하지 않는다
    html = html.replace(
        'document.getElementById("scanBtn").addEventListener("click"',
        'if (false) document.getElementById("scanBtn").addEventListener("click"')

    html = html.replace("<title>레버리지 관제</title>",
                        "<title>레버리지 관제</title>\n"
                        '<meta name="robots" content="noindex,nofollow">')

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    return OUT
