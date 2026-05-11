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
TARGET_ROUND        = os.environ.get("TARGET_ROUND", "").strip()   # 예: "2회차", "3회", "" = 전체
CHECK_INTERVAL      = int(os.environ.get("CHECK_INTERVAL", "0"))   # 0 = 1회 실행, 양수 = 루프

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://ticket.melon.com/",
}


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
        "title": "",
        "date": "",
        "venue": "",
        "round": "",
        "prices": [],
        "round_prices": [],   # 회차 필터링된 가격
        "status": "",
        "url": url,
    }

    # ── 공연 제목 ──────────────────────────────
    for sel in ["h2.tit_concert", ".tit_perf", "h1.tit", ".perf_tit", "h2.tit"]:
        el = soup.select_one(sel)
        if el:
            info["title"] = el.get_text(strip=True)
            break

    # ── 날짜 / 장소 ────────────────────────────
    for row in soup.select(".info_detail li, .perf_info li, .tbl_info tr"):
        text = row.get_text(" ", strip=True)
        if any(k in text for k in ["날짜", "일시", "기간"]):
            info["date"] = text
        if any(k in text for k in ["장소", "공연장"]):
            info["venue"] = text

    # ── 회차 정보 추출 ─────────────────────────
    # 멜론티켓은 회차를 "1회", "2회차", "N회차" 형태로 표기
    round_els = soup.select(".tbl_schedule tr, .schedule_list li, .round_list li, .perf_schedule tr")
    rounds_found = []
    for el in round_els:
        txt = el.get_text(" ", strip=True)
        if re.search(r"\d+\s*회", txt):
            rounds_found.append(txt)
    if rounds_found:
        info["round"] = " / ".join(rounds_found[:5])  # 최대 5개까지만

    # ── 가격 추출 (전체) ───────────────────────
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

    # ── 회차 필터링 가격 ───────────────────────
    if TARGET_ROUND:
        # TARGET_ROUND가 포함된 블록에서만 가격 추출
        round_prices = []
        for el in soup.select(".tbl_schedule tr, .schedule_list li, .round_list li, .perf_schedule tr"):
            txt = el.get_text(" ", strip=True)
            if TARGET_ROUND in txt:
                for n in re.findall(r"[\d,]+(?=\s*원)", txt):
                    val = int(n.replace(",", ""))
                    if 1_000 < val < 10_000_000:
                        round_prices.append(val)
        info["round_prices"] = sorted(set(round_prices)) if round_prices else info["prices"]
    else:
        info["round_prices"] = info["prices"]

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
    round_label = f" [{TARGET_ROUND}]" if TARGET_ROUND else ""

    embed = {
        "title": f"🎟️ [{title}]{round_label} 티켓 감지!",
        "description": (
            f"**{PRICE_THRESHOLD:,}원 이상** 티켓이 발견됐어요!\n\n"
            f"🔥 해당 티켓: **{matched_str}**\n"
            f"지금 바로 확인하세요 👇"
        ),
        "color": 0xFF4500,
        "fields": [
            {"name": "🎭 공연명",    "value": title,                             "inline": False},
            {"name": "🗓 일정",      "value": info.get("date")   or "확인 필요", "inline": True},
            {"name": "📍 장소",      "value": info.get("venue")  or "확인 필요", "inline": True},
            {"name": "🔢 모니터링 회차", "value": TARGET_ROUND or "전체 회차",   "inline": True},
            {"name": "💰 전체 가격", "value": all_str,                           "inline": False},
            {"name": "🔖 예매 상태", "value": info.get("status") or "확인 필요", "inline": True},
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

    prices  = info.get("round_prices", [])
    matched = [p for p in prices if p >= PRICE_THRESHOLD]

    if matched:
        print(f"🎯 조건 충족! {[f'{p:,}원' for p in matched]}")
        send_discord_alert(info, matched)
    else:
        round_info = f" ({TARGET_ROUND})" if TARGET_ROUND else ""
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
    print(f"[INFO] 모니터링 회차: {TARGET_ROUND or '전체'}")
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
