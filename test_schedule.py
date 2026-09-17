"""สคริปต์ทดสอบชั่วคราว - ส่ง ntfy test ตามเวลาที่กำหนด แล้วจบตัวเอง (ลบทิ้งได้หลังใช้เสร็จ)"""
import time
from datetime import datetime

import main

TARGETS = ["20:40", "20:50", "21:00"]

today = datetime.now().date()
for time_str in TARGETS:
    hh, mm = map(int, time_str.split(":"))
    target = datetime.combine(today, datetime.min.time()).replace(hour=hh, minute=mm)
    wait_seconds = (target - datetime.now()).total_seconds()
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    main.send_ntfy(f"[TEST] {time_str} น.", f"ทดสอบ noti เวลา {time_str} น. - ถ้าเห็นแปลว่า noti ยังทำงานปกติ")

print("ส่งครบทุกรอบแล้ว")
