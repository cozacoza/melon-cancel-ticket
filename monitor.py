"""
멜론티켓 모니터링 스크립트
- GitHub Actions: 5분마다 1회 실행 후 종료
- Railway: CHECK_INTERVAL 환경변수 설정 시 무한루프 실행
"""

import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ───────────────────────────────────────────
# 환경변수 (GitHub Secrets에서 설정)
# ───────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
CONCERT_URL         = os.environ.get("CONCERT_URL", "")
PRICE_THRESHOLD     = int(os.environ.get("PRICE_THRESHOLD", "100000"))
CHECK_INTERVAL      = int(os.environ.get("CHECK_INTERVAL", "0"))

# 날짜/시간으로 회차 지정
# 예: "2025-08-15 19:30"  또는  "8월 15일 19:30"  또는  "08/15 19:30"
# 비워두면 전체 회차 모니터링
TARGET_DATETIME = os.environ.get("TARGET_DATETIME", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://ticket.melon.com/",
}


def normalize(text: str) -> str:
    """비교를 위해 공백·특수문자 제거 후 소문자화"""
    return re.sub(r"[\s\-\.:/]", "", text).lower()


def datetime_matches(cell_text: str) -> bool:
    """
    페이지의 날짜/시간 셀 텍스트가 TARGET_DATETIME과 일치하는지 확인.
    '2025-08-15 19:30', '8월15일', '19:30' 등 다양한 형태를 유연하게 비교.
    """
    if not TARGET_DATETIME:
        return True  # 비어있으면 전체 허용

    # 날짜/시간을 숫자만 추출해서 비교 (예: "20250815" "1930")
    target_norm = normalize(TARGET_DATETIME)

    # 월·일·시·분 숫자만 추출
    target_digits = re.sub(r"[^\d]", "", target_norm)
    cell_digits   = re.sub(r"[^\d]", "", cell_text)

    # 타겟 숫자가 셀 텍스트 숫자 안에 포함되면 매칭
    # 예: "0815" "1930" → 셀에 "20250815193000" 있으면 True
    parts = TARGET_DATETIME.replace("-", " ").replace("/", " ").replace(":", " ").split()
    return all(p.zfill(2) in cell_digits or p in cell_text for p in parts if p.isdigit())


def fetch_ticket_info(url: str) -> dict:
    """멜론티켓 공연 페이지에서 티켓 정보 파싱"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] 페이지 요청 실패: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    info = {
        "title":        "",
        "date":         "",
        "venue":        "",
        "target_round": "",   # 매칭된 회차 텍스트
        "prices":       [],   # 전체 가격
        "target_prices":[],   # 회차 필터링된 가격
        "status":       "",
        "url":          url,
    }

    # ── 공연 제목 ──────────────────────────────
    for sel in [".tit", "h2.tit_concert", ".tit_perf", "h1.tit", ".perf_tit"]:
        for el in soup.select(sel):
            text = el.get_text(strip=True)
            if text and len(text) > 3 and "안내" not in text and "출연" not in text:
                info["title"] = text
                break
        if info["title"]:
            break

    # ── 날짜 / 장소 ────────────────────────────
    for row in soup.select(".info_detail li, .perf_info li, .tbl_info tr"):
        text = row.get_text(" ", strip=True)
        if any(k in text for k in ["날짜", "일시", "기간"]):
            info["date"] = text
        if any(k in text for k in ["장소", "공연장"]):
            info["venue"] = text

    # ── 전체 가격 추출 ─────────────────────────
    price_candidates = []
    for el in soup.select(".price, .ticket_price, .tbl_price td, .price_info, .area_price"):
        txt = el.get_text(" ", strip=True)
        for n in re.findall(r"[\d,]+(?=\s*원)", txt):
            val = int(n.replace(",", ""))
            if 1_000 < val < 10_000_000:
                price_candidates.append(val)

    if not price_candidates:
        for n in re.findall(r"([\d,]{4,})\s*원", soup.get_text()):
            val = int(n.replace(",", ""))
            if 1_000 < val < 10_000_000:
                price_candidates.append(val)

    info["prices"] = sorted(set(price_candidates))

    # ── 날짜/시간 기반 회차 필터링 ────────────
    if TARGET_DATETIME:
        target_prices = []
        matched_row_text = ""

        # 스케줄 테이블 행 순회
        schedule_rows = soup.select(
            ".tbl_schedule tr, .schedule_list li, "
            ".perf_schedule tr, .list_schedule li, "
            "table tr"
        )

        for row in schedule_rows:
            row_text = row.get_text(" ", strip=True)
            if datetime_matches(row_text):
                matched_row_text = row_text
                for n in re.findall(r"[\d,]+(?=\s*원)", row_text):
                    val = int(n.replace(",", ""))
                    if 1_000 < val < 10_000_000:
                        target_prices.append(val)

        info["target_round"]  = matched_row_text or TARGET_DATETIME
        # 회차별 가격을 못 찾으면 전체 가격으로 폴백
        info["target_prices"] = sorted(set(target_prices)) if target_prices else info["prices"]
    else:
        info["target_round"]  = "전체 회차"
        info["target_prices"] = info["prices"]

    # ── 예매 상태 ──────────────────────────────
    for sel in [".btn_reservation", ".btn_booking", ".state_label", ".btn_ticket"]:
        el = soup.select_one(sel)
        if el:
            info["status"] = el.get_text(strip=True)
            break

    return info


def send_discord_alert(info: dict, matched_prices: list):
    """Discord 웹훅으로 알림 전송"""
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")
        return

    title       = info.get("title") or "공연명 확인 필요"
    now         = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    matched_str = ", ".join(f"{p:,}원" for p in matched_prices)
    all_str     = ", ".join(f"{p:,}원" for p in info["prices"]) or "확인 필요"
    round_label = f" [{TARGET_DATETIME}]" if TARGET_DATETIME else ""

    embed = {
        "title": f"🎟️ {title}{round_label} 티켓 감지!",
        "description": (
            f"**{PRICE_THRESHOLD:,}원 이상** 티켓이 발견됐어요!\n\n"
            f"🔥 해당 티켓: **{matched_str}**\n"
            f"지금 바로 확인하세요 👇"
        ),
        "color": 0xFF4500,
        "fields": [
            {"name": "🎭 공연명",    "value": title,                                      "inline": False},
            {"name": "🗓 일정",      "value": info.get("date")        or "확인 필요",     "inline": True},
            {"name": "📍 장소",      "value": info.get("venue")       or "확인 필요",     "inline": True},
            {"name": "🕐 지정 회차", "value": TARGET_DATETIME         or "전체 회차",     "inline": True},
            {"name": "💰 전체 가격", "value": all_str,                                    "inline": False},
            {"name": "🔖 예매 상태", "value": info.get("status")      or "확인 필요",     "inline": True},
        ],
        "url": info["url"],
        "footer": {"text": f"멜론티켓 모니터 | {now}"},
    }

    payload = {
        "content": f"@everyone 🔔 **{title}**{round_label} — {PRICE_THRESHOLD:,}원 이상 티켓 발견!",
        "embeds": [embed],
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        print(f"[OK] Discord 알림 전송 완료: {matched_str}")
    except requests.RequestException as e:
        print(f"[ERROR] Discord 전송 실패: {e}")


def check_once():
    """1회 체크 실행"""
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] 체크 중... ({CONCERT_URL})")

    info = fetch_ticket_info(CONCERT_URL)
    if not info:
        print("[ERROR] 페이지 로드 실패")
        return

    prices  = info.get("target_prices", [])
    matched = [p for p in prices if p >= PRICE_THRESHOLD]

    if matched:
        print(f"🎯 조건 충족! {[f'{p:,}원' for p in matched]}")
        send_discord_alert(info, matched)
    else:
        round_info = f" ({TARGET_DATETIME})" if TARGET_DATETIME else ""
        print(f"조건 미충족{round_info} (발견된 가격: {[f'{p:,}원' for p in prices] or '없음'})")


def main():
    if not CONCERT_URL:
        print("[ERROR] CONCERT_URL 환경변수를 설정해 주세요.")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[ERROR] DISCORD_WEBHOOK_URL 환경변수를 설정해 주세요.")
        return

    print(f"[INFO] 대상 URL    : {CONCERT_URL}")
    print(f"[INFO] 알림 조건   : {PRICE_THRESHOLD:,}원 이상")
    print(f"[INFO] 지정 회차   : {TARGET_DATETIME or '전체'}")
    print(f"[INFO] 실행 모드   : {'루프 (Railway)' if CHECK_INTERVAL > 0 else '1회 실행 (GitHub Actions)'}")
    print("─" * 50)

    if CHECK_INTERVAL > 0:
        while True:
            check_once()
            time.sleep(CHECK_INTERVAL)
    else:
        check_once()


if __name__ == "__main__":
    main()
