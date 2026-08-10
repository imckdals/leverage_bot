"""설정 파일 자동 검사.

설정을 손댈 때마다 중복·배수 부호·필드 누락 같은 실수가 났다.
사람이 눈으로 훑는 대신 기계가 매번 확인하게 한다.
selftest.py 가 이 검사를 부르고, 그게 GitHub Actions 의 '규칙 검사'
단계에서 돌기 때문에 깨진 설정은 배포 전에 걸린다.
"""

from __future__ import annotations

from collections import Counter

REQUIRED = ("u", "name", "group", "market", "vol_max")
MARKETS = ("US", "KR")


def validate(cfg: dict) -> list[str]:
    """문제 목록을 반환한다. 비어 있으면 정상."""
    bad: list[str] = []
    U = cfg.get("universe") or []
    if not U:
        return ["universe 가 비어 있습니다"]

    # 기초자산 중복 — 상태 파일이 종목을 u 로 구분하므로 겹치면 안 된다
    for k, n in Counter(x.get("u") for x in U).items():
        if n > 1:
            bad.append(f"기초자산 중복: {k} ({n}번)")

    # 같은 상품 티커가 여러 종목에 붙어 있으면 추천이 꼬인다
    owners: dict[str, list[str]] = {}
    for it in U:
        for side in ("long", "short"):
            for pair in (it.get(side) or []):
                owners.setdefault(pair[0], []).append(f"{it.get('u')}/{side}")
    for t, who in owners.items():
        if len(who) > 1:
            bad.append(f"상품 티커 중복: {t} → {', '.join(who)}")

    for it in U:
        u = it.get("u", "?")
        for f in REQUIRED:
            if f not in it:
                bad.append(f"{u}: 필수 항목 {f} 없음")
        if it.get("market") not in MARKETS:
            bad.append(f"{u}: market 은 US 또는 KR 이어야 함 (지금 {it.get('market')})")
        try:
            v = float(it.get("vol_max"))
            if not 0.005 <= v <= 0.15:
                bad.append(f"{u}: vol_max 가 상식 범위 밖 ({v})")
        except (TypeError, ValueError):
            bad.append(f"{u}: vol_max 가 숫자가 아님")

        sf = it.get("signal_from")
        if sf is not None and (not isinstance(sf, str) or not sf):
            bad.append(f"{u}: signal_from 이 티커 문자열이 아님 → {sf}")

        if not (it.get("long") or it.get("short")):
            bad.append(f"{u}: 살 수 있는 상품이 하나도 없음")

        for side, want in (("long", 1), ("short", -1)):
            for pair in (it.get(side) or []):
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    bad.append(f"{u}/{side}: [티커, 배수] 형식이 아님 → {pair}")
                    continue
                t, x = pair
                if not isinstance(t, str) or not t:
                    bad.append(f"{u}/{side}: 티커가 문자열이 아님 → {t}")
                try:
                    x = int(x)
                except (TypeError, ValueError):
                    bad.append(f"{u}/{side}/{t}: 배수가 정수가 아님 → {x}")
                    continue
                # 롱에 음수, 인버스에 양수가 들어가면 감가 계산이 통째로 뒤집힌다
                if x * want <= 0:
                    bad.append(f"{u}/{side}/{t}: 배수 부호가 방향과 반대 ({x:+d})")
                if abs(x) > 3:
                    bad.append(f"{u}/{side}/{t}: 배수가 비정상 ({x:+d})")

    groups = {x.get("group") for x in U}
    for key in ("watch_groups", "regime_exempt_groups"):
        for g in (cfg.get(key) or []):
            if g not in groups:
                bad.append(f"{key} 에 존재하지 않는 그룹: {g}")

    if cfg.get("watch_groups"):
        live = [x for x in U if x.get("group") in cfg["watch_groups"]]
        if not live:
            bad.append("watch_groups 에 해당하는 종목이 하나도 없음")

    r = cfg.get("rules") or {}
    for side in ("long", "short"):
        s = r.get(side) or {}
        if s.get("min_gates", 0) < s.get("watch_gates", 0):
            bad.append(f"rules.{side}: min_gates 가 watch_gates 보다 작음")
    st = r.get("straight") or {}
    if st.get("er_min", 0) > st.get("er_strong", 1):
        bad.append("rules.straight: er_min 이 er_strong 보다 큼")
    if st.get("r2_min", 0) > st.get("r2_strong", 1):
        bad.append("rules.straight: r2_min 이 r2_strong 보다 큼")

    return bad


def report(cfg: dict) -> bool:
    """검사 결과를 출력하고 정상 여부를 반환한다."""
    bad = validate(cfg)
    U = cfg.get("universe") or []
    n_prod = sum(len(it.get("long") or []) + len(it.get("short") or []) for it in U)
    if bad:
        print(f"설정 문제 {len(bad)}건:")
        for b in bad[:20]:
            print(f"   - {b}")
        if len(bad) > 20:
            print(f"   … 외 {len(bad) - 20}건")
        return False
    print(f"설정 검사 통과 — 기초자산 {len(U)}종 · 상품 {n_prod}종")
    return True
