"""
멜론티켓 모니터링 스크립트 (공식 API 방식)
- HTML 파싱 대신 멜론티켓 내부 API를 직접 호출
- 회차별 날짜/시간 정확히 필터링 가능
- GitHub Actions: 5분마다 1회 실행
"""

import os
import re
import time
import requests
from datetime import datetime

# ───────────────────────────────────────────
# 환경변수 (GitHub Secrets에서 설정)
# ───────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
CONCERT_URL         = os.environ.get("CONCERT_URL", "")
PRICE_THRESHOLD     = int(os.environ.get("PRICE_THRESHOLD", "100000"))
CHECK_INTERVAL      = int(os.environ.get("CHECK_INTERVAL", "0"))

# 날짜+시간으로 회차 지정
# 날짜: "20260610" 또는 "2026-06-10" 또는 "06/10"
# 시간: "19:30" 또는 "1930" (선택, 없으면 해당 날짜 전체)
# 예시: "2026-06-10 19:30"  /  "20260610"  /  "06-10 19:30"
TARGET_DATETIME = os.environ.get("TARGET_DATETIME", "").strip()

BASE_API = "https://tktapi.melon.com/api/product/schedule"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://ticket.melon.com/",
    "X-Requested-With": "XMLHttpRequest",
}

COMMON_PARAMS = {
    "pocCode": "SC0002",
    "perfTypeCode": "GN0002",
    "sellTypeCode": "ST0001",
    "seatCntDisplayYn": "N",
    "interlockTypeCode": "IL0002",
    "corpCodeNo": "",
    "reflashYn": "N",
    "requestservicetype": "P",
}


# ─── 유틸 ────────────────────────────────────────────────────

def extract_prod_id(url: str) -> str | None:
    """멜론티켓 URL에서 prodId 추출"""
    m = re.search(r"prodId=(\d+)", url)
    return m.group(1) if m else None


def normalize_date(raw: str) -> str:
    """날짜 문자열을 YYYYMMDD 형식으로 정규화"""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:   # 20260610
        return digits
    if len(digits) == 6:   # 260610 → 20260610
        return "20" + digits
    if len(digits) == 4:   # 0610 → 2026****  (연도 추론)
        year = str(datetime.now().year)
        return year + digits
    return digits


def normalize_time(raw: str) -> str:
    """시간 문자열을 HHMM 형식으로 정규화"""
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(4)[:4]  # "19:30" → "1930"


def parse_target() -> tuple[str, str]:
    """TARGET_DATETIME을 (날짜YYYYMMDD, 시간HHMM) 으로 분리"""
    if not TARGET_DATETIME:
        return "", ""

    # 날짜와 시간을 공백 또는 'T'로 분리
    parts = TARGET_DATETIME.strip().split()

    date_part = parts[0] if parts else ""
    time_part = parts[1] if len(parts) > 1 else ""

    return normalize_date(date_part), normalize_time(time_part) if time_part else ""


# ─── 멜론 API 호출 ───────────────────────────────────────────

def get_daylist(prod_id: str) -> list:
    """공연 날짜 목록 조회"""
    try:
        res = requests.get(
            f"{BASE_API}/daylist.json",
            params={"prodId": prod_id, **COMMON_PARAMS},
            headers=HEADERS, timeout=10
        )
        res.raise_for_status()
        data = res.json()
        return data.get("data", {}).get("perfDaylist", []) or []
    except Exception as e:
        print(f"  [ERROR] daylist 조회 실패: {e}")
        return []


def get_timelist(prod_id: str, perf_day: str) -> list:
    """특정 날짜의 회차(시간) 목록 조회"""
    try:
        res = requests.get(
            f"{BASE_API}/timelist.json",
            params={"prodId": prod_id, "perfDay": perf_day, **COMMON_PARAMS},
            headers=HEADERS, timeout=10
        )
        res.raise_for_status()
        data = res.json()
        return data.get("data", {}).get("perfTimelist", []) or []
    except Exception as e:
        print(f"  [ERROR] timelist 조회 실패: {e}")
        return []


def get_gradelist(prod_id: str, perf_day: str, schedule_no: str) -> list:
    """특정 회차의 좌석 등급/가격 조회"""
    try:
        res = requests.get(
            f"{BASE_API}/gradelist.json",
            params={
                "prodId": prod_id,
                "perfDay": perf_day,
                "scheduleNoArray": schedule_no,
                "seatPoc": "1",
                "cancelCloseDt": "",
                **COMMON_PARAMS,
            },
            headers=HEADERS, timeout=10
        )
        res.raise_for_status()
        data = res.json()
        return data.get("data", {}).get("seatGradelist", []) or []
    except Exception as e:
        print(f"  [ERROR] gradelist 조회 실패: {e}")
        return []


def get_concert_title(prod_id: str) -> str:
    """공연 제목 조회 (기본 페이지에서 og:title 파싱)"""
    try:
        from bs4 import BeautifulSoup
        res = requests.get(
            f"https://ticket.melon.com/performance/index.htm?prodId={prod_id}",
            headers={**HEADERS, "Accept": "text/html"},
            timeout=10
        )
        soup = BeautifulSoup(res.text, "html.parser")
        for sel in [".tit", "h2.tit_concert", ".tit_perf"]:
            for el in soup.select(sel):
                text = el.get_text(strip=True)
                if text and len(text) > 3 and "안내" not in text and "출연" not in text:
                    return text
        og = soup.find("meta", property="og:title")
        if og:
            return og.get("content", "")
    except Exception:
        pass
    return ""


# ─── Discord 알림 ────────────────────────────────────────────

def send_discord_alert(title: str, perf_day: str, perf_time: str,
                       grades: list, matched: list, threshold: int):
    if not DISCORD_WEBHOOK_URL:
        return

    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = f"{perf_day[:4]}-{perf_day[4:6]}-{perf_day[6:]}"
    time_str = f"{perf_time[:2]}:{perf_time[2:]}" if len(perf_time) == 4 else perf_time
    round_str = f"{date_str} {time_str}".strip()

    matched_str = ", ".join(
        f"{g['seatGradeName']} {int(g['basePrice']):,}원"
        for g in matched
    )
    all_str = ", ".join(
        f"{g['seatGradeName']} {int(g['basePrice']):,}원"
        for g in grades
    )

    embed = {
        "title": f"🎟️ {title} [{round_str}] 티켓 감지!",
        "description": (
            f"**{threshold:,}원 이상** 티켓이 발견됐어요!\n\n"
            f"🔥 해당 티켓: **{matched_str}**\n"
            f"지금 바로 확인하세요 👇"
        ),
        "color": 0xFF4500,
        "fields": [
            {"name": "🎭 공연명",    "value": title,    "inline": False},
            {"name": "🗓 날짜",      "value": date_str, "inline": True},
            {"name": "🕐 시간",      "value": time_str, "inline": True},
            {"name": "💰 전체 가격", "value": all_str,  "inline": False},
        ],
        "url": CONCERT_URL,
        "footer": {"text": f"멜론티켓 모니터 | {now}"},
    }

    payload = {
        "content": f"@everyone 🔔 **{title}** [{round_str}] — {threshold:,}원 이상 티켓 발견!",
        "embeds": [embed],
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        print(f"  [OK] Discord 알림 전송: {matched_str}")
    except Exception as e:
        print(f"  [ERROR] Discord 전송 실패: {e}")


# ─── 메인 체크 로직 ──────────────────────────────────────────

def check_once(prod_id: str, title: str):
    now = datetime.now().strftime("%H:%M:%S")
    target_date, target_time = parse_target()

    print(f"[{now}] 체크 중... (prodId={prod_id})")
    if target_date:
        print(f"  → 지정 날짜: {target_date}" + (f" {target_time}" if target_time else " (시간 무관)"))

    # 1. 날짜 목록 조회
    daylist = get_daylist(prod_id)
    if not daylist:
        print("  날짜 목록 없음 (아직 오픈 전이거나 매진)")
        return

    # 2. 대상 날짜 필터링
    if target_date:
        daylist = [d for d in daylist if d.get("perfDay", "") == target_date]
        if not daylist:
            print(f"  지정 날짜 {target_date}의 회차 없음")
            return

    # 3. 각 날짜 → 시간 → 가격 순으로 탐색
    for day in daylist:
        perf_day = day["perfDay"]
        timelist = get_timelist(prod_id, perf_day)

        if not timelist:
            continue

        # 시간 필터링
        if target_time:
            timelist = [t for t in timelist if t.get("perfTime", "") == target_time]
            if not timelist:
                continue

        for slot in timelist:
            schedule_no = slot["scheduleNo"]
            perf_time   = slot.get("perfTime", "")
            casting     = slot.get("casting", "")

            grades = get_gradelist(prod_id, perf_day, schedule_no)
            if not grades:
                continue

            # 가격 조건 체크
            matched = [g for g in grades if int(g["basePrice"]) >= PRICE_THRESHOLD]

            date_str = f"{perf_day[:4]}-{perf_day[4:6]}-{perf_day[6:]}"
            time_str = f"{perf_time[:2]}:{perf_time[2:]}" if len(perf_time) == 4 else ""
            price_list = [f"{g['seatGradeName']} {int(g['basePrice']):,}원" for g in grades]

            print(f"  [{date_str} {time_str}] {', '.join(price_list)}")
            if casting:
                print(f"    캐스팅: {casting}")

            if matched:
                print(f"  🎯 조건 충족! {[g['seatGradeName'] for g in matched]}")
                send_discord_alert(title, perf_day, perf_time, grades, matched, PRICE_THRESHOLD)
            else:
                print(f"  조건 미충족 ({PRICE_THRESHOLD:,}원 이상 없음)")

            time.sleep(0.5)  # API 과부하 방지


def main():
    if not CONCERT_URL:
        print("[ERROR] CONCERT_URL 환경변수를 설정해 주세요.")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[ERROR] DISCORD_WEBHOOK_URL 환경변수를 설정해 주세요.")
        return

    prod_id = extract_prod_id(CONCERT_URL)
    if not prod_id:
        print("[ERROR] URL에서 prodId를 찾을 수 없어요. 멜론티켓 URL을 확인해주세요.")
        return

    title = get_concert_title(prod_id) or f"공연 {prod_id}"

    print(f"[INFO] 공연명    : {title}")
    print(f"[INFO] prodId    : {prod_id}")
    print(f"[INFO] 알림 조건 : {PRICE_THRESHOLD:,}원 이상")
    print(f"[INFO] 지정 회차 : {TARGET_DATETIME or '전체'}")
    print(f"[INFO] 실행 모드 : {'루프' if CHECK_INTERVAL > 0 else '1회 실행 (GitHub Actions)'}")
    print("─" * 50)

    if CHECK_INTERVAL > 0:
        while True:
            check_once(prod_id, title)
            time.sleep(CHECK_INTERVAL)
    else:
        check_once(prod_id, title)


if __name__ == "__main__":
    main()
