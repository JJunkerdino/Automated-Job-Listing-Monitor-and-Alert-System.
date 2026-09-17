"""
WAT Job Search Monitor
======================
Playwright automation ที่เข้าเว็บ New Step Exchange Program, กรอกฟอร์ม,
เข้าหน้าเลือกงาน, ค้นหาคำว่า "Thai" แล้ววน check ซ้ำจนกว่าจะเจองานที่ตรงกับ
MATCH_KEYWORDS (เช่น "Lahn Pad Thai") จากนั้นแจ้งเตือน (toast + beep + ntfy.sh push notification)
และเปิด browser ค้างไว้ให้ผู้ใช้เข้าไปเลือกงานเอง

สำคัญ: สคริปต์นี้ไม่คลิกปุ่ม "เลือกงานนี้"/"เลือกเป็นตัวสำรอง" ใดๆ ทั้งสิ้น
เป็นระบบแจ้งเตือนอย่างเดียว (notify-only) ตามที่ผู้ใช้ยืนยัน — ห้ามเพิ่ม
โค้ด auto-click ปุ่มเลือกงานกลับเข้ามาโดยไม่ได้รับอนุญาตใหม่
"""

import os
import re
import sys
import json
import time
import logging
import winsound
import urllib.request
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from winotify import Notification

# ============================================================
# CONFIG — แก้ค่าตรงนี้ได้ตามต้องการ
# ============================================================

# --- ข้อมูลฟอร์ม (step 1) ---
FIRST_NAME = "KARN"
LAST_NAME = "KARAKET"
PHONE = "0933430263"
CONTACT_EMAIL = "jj.karnkaraket@gmail.com"
SEASON_VALUE = "Summer"  # ค่าจริงใน <option value="..."> ของ #seasonSelect (ไม่ใช่ label ที่มี emoji)

# --- URL ของฟอร์ม (ตรวจสอบตัวอักษรให้แม่นยำ พิมพ์ผิดง่ายมาก เช่น l กับ I) ---
FORM_URL = "https://script.google.com/macros/s/AKfycbwUZIwNELDWZAKHtMkmpmx89ILMaBB8ah99ZIegMYAXFuReXyN-gbgq7wG_OIiae0Zj/exec"

# --- คำค้นหาในหน้าเลือกงาน (step 6) ---
SEARCH_TERM = "Thai"

# --- keyword ที่ใช้ตัดสินว่าใช่งานเป้าหมายหรือไม่ (lowercase, ตรวจแบบ substring) ---
# แก้ list นี้ได้เลยถ้าต้องการหางานอื่นในอนาคต
MATCH_KEYWORDS = ["lahn", "pad thai"]

# --- จังหวะเวลา ---
CHECK_INTERVAL_SECONDS = 60          # soft-check ทุกกี่วินาที (อ่าน job card ที่โหลดอยู่แล้วซ้ำ ไม่ reload หน้า)
HARD_RELOAD_INTERVAL_SECONDS = 120   # reload หน้าใหม่ทั้งหมด + กรอกฟอร์มใหม่ + search ใหม่ ทุกกี่วินาที
MAX_RUNTIME_HOURS = 12               # safety limit หยุดอัตโนมัติถ้ายังไม่เจอ

# --- Heartbeat + error alert ---
HEARTBEAT_INTERVAL_SECONDS = 45 * 60  # แจ้งเตือนว่า "ยังรันอยู่ปกติ" ทุกกี่วินาที
ERROR_ALERT_THRESHOLD = 3             # เช็คพลาดติดกันกี่ครั้งถึงจะแจ้งเตือนว่า "มีปัญหา"

# --- Screenshot ---
SCREENSHOT_ON_MATCH = True
SCREENSHOT_DIR = "screenshots"

# --- Log ---
LOG_FILE = "log.txt"

# --- Push notification (ผ่าน ntfy.sh) ---
# HTTP POST ธรรมดา ไม่ต้องสมัคร/ไม่มี API key เลย ลงแอพ ntfy บนมือถือ แล้ว subscribe
# topic ชื่อเดียวกันกับด้านล่าง จะเห็น push notification ทันทีที่สคริปต์ส่ง
NTFY_ENABLED = True
NTFY_TOPIC = "wat-job-karn-4e980b0ba6ef"  # เปลี่ยนเป็นชื่ออื่นได้ (ยิ่งสุ่ม/ยาวยิ่งปลอดภัย เพราะ topic เดาโดยสุ่มจะเห็นข้อความได้)

# ============================================================
# LOGGING — print + เขียนไฟล์ log.txt พร้อม timestamp ในตัว
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("wat_monitor")


# ============================================================
# NOTIFICATION HELPERS
# ============================================================
def play_alert_sound():
    """เสียง beep เตือน (winsound เป็น built-in module บน Windows ไม่ต้องติดตั้งเพิ่ม)"""
    try:
        for _ in range(3):
            winsound.Beep(1000, 400)
            time.sleep(0.15)
    except Exception:
        log.exception("เล่นเสียง beep ไม่สำเร็จ")


def show_toast(title: str, message: str):
    """Windows native toast notification ผ่าน winotify"""
    try:
        Notification(app_id="WAT Job Monitor", title=title, msg=message, duration="long").show()
    except Exception:
        log.exception("แสดง toast notification ไม่สำเร็จ")


def send_ntfy(subject: str, body: str):
    """ส่ง push notification ผ่าน ntfy.sh (HTTP POST ธรรมดา ไม่มี account/secret)"""
    if not NTFY_ENABLED:
        return
    try:
        payload = json.dumps({
            "topic": NTFY_TOPIC,
            "title": subject,
            "message": body,
            "priority": 5,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://ntfy.sh/",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info("ส่ง push notification ผ่าน ntfy.sh สำเร็จ (topic=%s, HTTP %s)", NTFY_TOPIC, resp.status)
    except Exception:
        log.exception("ส่ง ntfy notification ไม่สำเร็จ (toast/beep ที่แจ้งไปแล้วยังใช้ได้ปกติ)")


def notify_match(job_title: str, city: str, state: str, salary: str, screenshot_path: str | None):
    title = "เจองาน Thai แล้ว!"
    message = f"{job_title}\nCity: {city} | State: {state}\nเงินเดือน: {salary}"
    if screenshot_path:
        message += f"\n\nScreenshot: {os.path.abspath(screenshot_path)}"
    log.info("=== พบงานที่ตรงกับเงื่อนไข ===\n%s", message)
    play_alert_sound()
    show_toast(title, message)
    send_ntfy(subject=f"[WAT Monitor] {title}", body=message)


def notify_timeout():
    title = "หมดเวลา - ยังไม่เจองาน"
    message = f"รันครบ {MAX_RUNTIME_HOURS} ชั่วโมงแล้ว ยังไม่พบงานที่ตรงเงื่อนไข ({', '.join(MATCH_KEYWORDS)})"
    log.info(message)
    play_alert_sound()
    show_toast(title, message)
    send_ntfy(subject=f"[WAT Monitor] {title}", body=message)


def notify_heartbeat(check_count: int, elapsed_str: str):
    """แจ้งเตือนเป็นระยะว่า script ยังทำงานอยู่ปกติ (ส่งแค่ ntfy พอ ไม่ต้อง toast/beep กวนใจ)"""
    message = f"ยังทำงานอยู่ปกติ - เช็คไปแล้ว {check_count} ครั้ง (รันมาแล้ว {elapsed_str}) ยังไม่เจองานที่ตรงเงื่อนไข"
    log.info("[Heartbeat] %s", message)
    send_ntfy(subject="[WAT Monitor] ✅ ยังรันอยู่ปกติ", body=message)


def notify_error(consecutive_errors: int):
    """แจ้งเตือนเมื่อเช็คพลาดติดต่อกันหลายครั้ง (อาจเว็บล่ม/เน็ตหลุด/element เปลี่ยน) - ส่งครั้งเดียวต่อรอบปัญหา ไม่สแปม"""
    message = f"เช็คล้มเหลวติดต่อกัน {consecutive_errors} ครั้งแล้ว อาจมีปัญหา (ดูรายละเอียดใน log.txt)"
    log.warning("[Error Alert] %s", message)
    send_ntfy(subject="[WAT Monitor] ⚠️ มีปัญหาระหว่างรัน", body=message)


# ============================================================
# PLAYWRIGHT HELPERS
# ============================================================
def get_frame(page):
    """เนื้อหาจริงของหน้าเว็บซ้อนอยู่ใน iframe 2 ชั้น (ยืนยันจาก DOM จริงแล้ว)"""
    return page.frame_locator("iframe").frame_locator("iframe")


def fill_form_and_go_to_job_search(page):
    """Step 1-5: เปิดหน้าแรก, กรอกฟอร์ม 4 ช่อง, เลือกช่วงเวลา, กดปุ่มเลือกงาน"""
    log.info("เปิดหน้าฟอร์ม: %s", FORM_URL)
    page.goto(FORM_URL, wait_until="domcontentloaded", timeout=30000)

    frame = get_frame(page)
    frame.locator("#firstName").wait_for(state="visible", timeout=20000)

    frame.locator("#firstName").fill(FIRST_NAME)
    frame.locator("#lastName").fill(LAST_NAME)
    frame.locator("#phone").fill(PHONE)
    frame.locator("#email").fill(CONTACT_EMAIL)
    frame.locator("#seasonSelect").select_option(value=SEASON_VALUE)

    frame.get_by_role("button", name="เลือกงาน").click()

    # รอหน้าเลือกงาน Summer โหลดเสร็จ (สังเกตหัวข้อ "2 เลือกงาน Summer")
    frame.get_by_role("heading", name=re.compile("เลือกงาน")).wait_for(state="visible", timeout=20000)
    log.info("เข้าสู่หน้าเลือกงานสำเร็จ")


def search_jobs(page):
    """Step 6-7: พิมพ์คำค้นหาในหน้าเลือกงาน แล้วรอผลกรองจริง

    สำคัญ: เว็บนี้กรอง list ด้วย keyup event ไม่ใช่แค่ set ค่า input เฉยๆ
    (ยืนยันจากการทดสอบจริง — fill() เฉยๆ list ไม่กรอง) ต้องพิมพ์แบบ
    keystroke จริง แล้วรอ debounce ก่อนอ่านผลเสมอ
    """
    frame = get_frame(page)
    search_box = frame.locator("#searchInput")
    search_box.click()
    search_box.fill("")
    search_box.press_sequentially(SEARCH_TERM, delay=80)
    page.wait_for_timeout(1500)  # รอ debounce ของ filter บนเว็บ


def parse_card_details(card_text: str):
    """ดึง city/state/salary จากข้อความ card โดย bound ด้วย emoji marker ที่รู้โครงสร้างแน่นอน"""
    city, state, salary = "N/A", "N/A", "N/A"
    m = re.search(r"City:\s*([^|]+?)\s*\|\s*State:\s*([^💵📅🏠]+)", card_text)
    if m:
        city = m.group(1).strip()
        state = m.group(2).strip()
    m2 = re.search(r"💵\s*([^📅🏠]+)", card_text)
    if m2:
        salary = m2.group(1).strip()
    return city, state, salary


def read_job_cards(page):
    """อ่านรายชื่องานที่ขึ้นอยู่ตอนนี้ (หลัง filter) พร้อม city/state/salary"""
    frame = get_frame(page)
    cards = []
    headings = frame.locator("h3")
    for i in range(headings.count()):
        heading = headings.nth(i)
        title = (heading.text_content() or "").strip()
        if not title:
            continue
        card_text = (heading.locator("xpath=..").text_content() or "")
        if "📍" not in card_text:
            continue  # heading นี้ไม่ใช่ job card จริง (เช่นหัวข้อ step อื่นในหน้าเดียวกัน)
        city, state, salary = parse_card_details(card_text)
        cards.append({"title": title, "city": city, "state": state, "salary": salary})
    return cards


def find_matching_card(cards):
    for card in cards:
        title_lower = card["title"].lower()
        if any(kw.lower() in title_lower for kw in MATCH_KEYWORDS):
            return card
    return None


def launch_browser(p):
    """เปิด browser+page ใหม่ — เรียกใช้ทั้งตอนเริ่มต้นและตอน relaunch หลัง browser ปิดเองกลางคัน"""
    browser = p.chromium.launch(headless=False)
    page = browser.new_context().new_page()
    return browser, page


# ============================================================
# MAIN LOOP
# ============================================================
def run():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    start_time = datetime.now()
    deadline = start_time + timedelta(hours=MAX_RUNTIME_HOURS)
    last_hard_reload = None
    last_heartbeat = start_time
    check_count = 0
    consecutive_errors = 0
    error_alerted = False

    log.info("เริ่มมอนิเตอร์ - จะรันสูงสุดถึง %s", deadline.strftime("%Y-%m-%d %H:%M:%S"))

    with sync_playwright() as p:
        browser, page = launch_browser(p)

        while datetime.now() < deadline:
            try:
                if page.is_closed() or not browser.is_connected():
                    log.warning("Browser/หน้าเว็บถูกปิดไป (เช่น เผลอปิดหน้าต่าง) - เปิด browser ใหม่ทันที ไม่รอครบ 12 ชม.")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser, page = launch_browser(p)
                    last_hard_reload = None  # บังคับให้ hard reload ทันทีหลัง relaunch

                now = datetime.now()
                need_hard_reload = (
                    last_hard_reload is None
                    or (now - last_hard_reload).total_seconds() >= HARD_RELOAD_INTERVAL_SECONDS
                )

                if need_hard_reload:
                    log.info("--- Hard reload: กรอกฟอร์มใหม่ตั้งแต่ step 1 ---")
                    fill_form_and_go_to_job_search(page)
                    search_jobs(page)
                    last_hard_reload = datetime.now()
                else:
                    log.info("--- Soft check: อ่าน job card ปัจจุบันซ้ำ (ไม่ reload หน้า) ---")

                cards = read_job_cards(page)
                log.info("เจอ job card ทั้งหมด %d รายการ (ค้นหาคำว่า '%s')", len(cards), SEARCH_TERM)
                check_count += 1
                consecutive_errors = 0
                error_alerted = False

                match = find_matching_card(cards)
                if match:
                    screenshot_path = None
                    if SCREENSHOT_ON_MATCH:
                        screenshot_path = os.path.join(
                            SCREENSHOT_DIR, f"match_{datetime.now():%Y%m%d_%H%M%S}.png"
                        )
                        page.screenshot(path=screenshot_path, full_page=True)

                    notify_match(match["title"], match["city"], match["state"], match["salary"], screenshot_path)
                    log.info("หยุด loop แล้ว - เปิด browser ค้างไว้ให้ผู้ใช้เข้ามาเลือกงานเอง (ไม่ auto-click)")

                    # ค้าง process ไว้ตรงนี้เพื่อไม่ให้ `with sync_playwright()` ปิด browser ตอนออกจากฟังก์ชัน
                    while True:
                        time.sleep(3600)

            except PlaywrightTimeoutError:
                log.exception("Timeout ระหว่างเช็ครอบนี้ (เว็บโหลดไม่ทัน/หา element ไม่เจอ) - ข้ามไปรอบถัดไป")
                consecutive_errors += 1
            except Exception:
                log.exception("เกิด error ไม่คาดคิดระหว่างเช็ครอบนี้ - ข้ามไปรอบถัดไป")
                consecutive_errors += 1

            # แจ้งเตือนถ้าเช็คพลาดติดกันหลายครั้ง (แจ้งครั้งเดียวต่อรอบปัญหา ไม่สแปมทุก loop)
            if consecutive_errors >= ERROR_ALERT_THRESHOLD and not error_alerted:
                notify_error(consecutive_errors)
                error_alerted = True

            # แจ้งเตือนเป็นระยะว่ายังทำงานอยู่ปกติ
            if (datetime.now() - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL_SECONDS:
                elapsed = datetime.now() - start_time
                hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
                minutes = remainder // 60
                notify_heartbeat(check_count, f"{hours} ชม. {minutes} นาที")
                last_heartbeat = datetime.now()

            time.sleep(CHECK_INTERVAL_SECONDS)

        notify_timeout()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("ผู้ใช้หยุด script เอง (Ctrl+C)")
