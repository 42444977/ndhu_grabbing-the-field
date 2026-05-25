# -*- coding: utf-8 -*-
from __future__ import annotations
"""
搶場機器人 7.0
更新內容：
  - 新增「定時啟動」：可設定未來時間，到時間才開始整個流程
  - 與東華系統時間同步校正：避免本機時間與網頁 currTime 漂移
  - GUI 即時狀態列 + 滾動日誌區
  - 自動記憶上次設定（含密碼，使用 base64 + XOR 混淆）
  - Selenium 於獨立執行緒執行，GUI 不卡頓
  - 現代深色卡片風 UI
  - 修正 root.destroy 後仍存取 self 的潛在問題
  - 驗證碼暫存圖改寫到系統 temp 目錄
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import time
import datetime
import threading
import queue
import json
import os
import base64
import tempfile

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    import ddddocr
    HAS_DDDDOCR = True
except ImportError:
    HAS_DDDDOCR = False


# ========================================================================
# Config — 設定載入與儲存
# ========================================================================
class Config:
    """負責讀寫 %APPDATA%/NDHU_Booking/config.json"""

    APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "NDHU_Booking")
    CONFIG_FILE = os.path.join(APP_DIR, "config.json")

    DEFAULTS = {
        "email": "",
        "password": "",
        "remember_password": True,
        "sport": "排球",
        "venue": "VOL0F排球場F-男",
        "session": "17:00-19:00",
        "reason": "我要練球",
        "check": True,
        "use_schedule": False,
        "schedule_hour": "11",
        "schedule_minute": "58",
        "schedule_second": "00",
        "snatch_hour": "00",
        "snatch_minute": "00",
        "snatch_second": "00",
        "stay_mode": "不自動關閉",
        "skip_extra_wait": False,
    }

    _XOR_KEY = b"ndhu_booking_7_xor"

    @classmethod
    def _obfuscate(cls, text: str) -> str:
        if not text:
            return ""
        data = text.encode("utf-8")
        result = bytes(b ^ cls._XOR_KEY[i % len(cls._XOR_KEY)] for i, b in enumerate(data))
        return base64.b64encode(result).decode("ascii")

    @classmethod
    def _deobfuscate(cls, text: str) -> str:
        if not text:
            return ""
        try:
            data = base64.b64decode(text.encode("ascii"))
            result = bytes(b ^ cls._XOR_KEY[i % len(cls._XOR_KEY)] for i, b in enumerate(data))
            return result.decode("utf-8")
        except Exception:
            return ""

    @classmethod
    def load(cls) -> dict:
        cfg = cls.DEFAULTS.copy()
        try:
            with open(cls.CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update(saved)
            if cfg.get("password"):
                cfg["password"] = cls._deobfuscate(cfg["password"])
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[Config] 載入失敗，使用預設值: {e}")
        return cfg

    @classmethod
    def save(cls, data: dict) -> None:
        try:
            os.makedirs(cls.APP_DIR, exist_ok=True)
            to_save = data.copy()
            if to_save.get("remember_password") and to_save.get("password"):
                to_save["password"] = cls._obfuscate(to_save["password"])
            else:
                to_save["password"] = ""
            with open(cls.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Config] 儲存失敗: {e}")


# ========================================================================
# TimeSync — 校正本機與東華系統時間差
# ========================================================================
class TimeSync:
    """讀取網頁 currTime，計算本機與系統的時間差，提供校正後時間"""

    def __init__(self):
        self.offset_seconds = 0.0
        self.calibrated = False

    def calibrate(self, driver, timeout=8.0) -> float:
        """讀取一次 currTime（會等到網頁 JS 填好文字才讀），更新 offset_seconds，回傳偏移秒數"""
        deadline = time.time() + timeout
        text = ""
        local_before = local_after = time.time()

        # currTime 是用 JS 動態填入，DOM 出現後文字可能還是空，要 polling
        while time.time() < deadline:
            try:
                local_before = time.time()
                # 兩種讀法都試一下：先 textContent (含隱藏), 再 .text
                text = (driver.execute_script(
                    "var e=document.getElementById('currTime');"
                    "return e ? (e.textContent || e.innerText || '').trim() : '';"
                ) or "").strip()
                local_after = time.time()
                if text and len(text) >= 8:
                    break
            except Exception:
                pass
            time.sleep(0.15)

        if not text or len(text) < 8:
            raise ValueError("無法讀取網頁 currTime 內容")

        # 取末段 8 字元 "HH:MM:SS"
        time_part = text[-8:]
        server_t = datetime.datetime.strptime(time_part, "%H:%M:%S")

        avg_local = datetime.datetime.fromtimestamp((local_before + local_after) / 2)
        server_dt = avg_local.replace(
            hour=server_t.hour, minute=server_t.minute, second=server_t.second, microsecond=0
        )

        diff = (server_dt - avg_local).total_seconds()
        # 處理跨午夜的邊界情況
        if diff > 12 * 3600:
            diff -= 24 * 3600
        elif diff < -12 * 3600:
            diff += 24 * 3600

        self.offset_seconds = diff
        self.calibrated = True
        return diff

    def now(self) -> datetime.datetime:
        """回傳校正後的當前時間"""
        return datetime.datetime.now() + datetime.timedelta(seconds=self.offset_seconds)


# ========================================================================
# BookingWorker — 在獨立執行緒中執行整個 Selenium 流程
# ========================================================================
class BookingWorker:
    """訊息格式（透過 queue 傳回主執行緒）：
        ("status", text, color_tag)
        ("log",    text)
        ("done",   success_bool, message)
    """

    # 驗證碼一次猜中 vs 多次猜中 -> 送出前的額外等待秒數
    CAPTCHA_DELAY_MAP = {1: 2.0, 2: 1.0, 3: 0.5}

    def __init__(self, data: dict, msg_queue: queue.Queue):
        self.data = data
        self.queue = msg_queue
        self.cancel_flag = threading.Event()
        self.driver = None
        self.time_sync = TimeSync()

    # ----- 對外控制 -----
    def cancel(self):
        self.cancel_flag.set()

    # ----- queue 工具 -----
    def _log(self, msg: str):
        self.queue.put(("log", msg))

    def _status(self, text: str, color: str = "accent"):
        self.queue.put(("status", text, color))

    def _done(self, success: bool, message: str):
        self.queue.put(("done", success, message))

    # ----- 主流程 -----
    def run(self):
        try:
            # Stage 1: 等待定時啟動
            if self.data["use_schedule"]:
                self._wait_for_schedule()
                if self.cancel_flag.is_set():
                    self._done(False, "已取消")
                    return

            # Stage 2: 啟動瀏覽器並登入
            self._status("啟動瀏覽器中...", "accent")
            self._log("啟動 Chrome WebDriver")
            self.driver = webdriver.Chrome()
            self.driver.get("https://sys.ndhu.edu.tw/gc/sportcenter/SportsFields/Login.aspx")

            self._status("登入中...", "accent")
            self._log(f"登入帳號 {self.data['email']}@gms.ndhu.edu.tw")
            self.driver.find_element(By.ID, "MainContent_TxtUSERNO").send_keys(self.data["email"])
            self.driver.find_element(By.ID, "MainContent_TxtPWD").send_keys(self.data["password"])
            self.driver.find_element(By.ID, "MainContent_Button1").click()

            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.ID, "MainContent_Button2"))
            )
            self._log("登入成功")

            # Stage 3: 校時 (僅正式模式需要)
            if self.data["check"]:
                self._status("校正系統時間中...", "accent")
                try:
                    offset = self.time_sync.calibrate(self.driver)
                    if offset >= 0:
                        self._log(f"校時完成 - 系統時間比本機快 {offset:.2f} 秒")
                    else:
                        self._log(f"校時完成 - 系統時間比本機慢 {abs(offset):.2f} 秒")
                except Exception as e:
                    self._log(f"校時失敗，將使用本機時間: {e}")
            else:
                self._log("練習模式，跳過校時")

            if self.cancel_flag.is_set():
                self._done(False, "已取消")
                return

            # Stage 4: 進入新增申請
            self._status("進入申請頁面...", "accent")
            if not self._safe_click((By.ID, "MainContent_Button2")):
                self._done(False, "進入申請頁面失敗")
                return
            self._log("進入新增申請")
            time.sleep(0.5)

            # Stage 5: 選擇運動類型與場地
            self._status(f"選擇 {self.data['sport_type']} / {self.data['venue']}", "accent")
            try:
                sport_dropdown = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "MainContent_drpkind"))
                )
                Select(sport_dropdown).select_by_visible_text(self.data["sport_type"])
                self._log(f"選擇運動類型: {self.data['sport_type']}")
            except Exception as e:
                self._done(False, f"運動類型選擇失敗: {e}")
                return
            time.sleep(0.6)
            
            # 選兩次以防網頁沒讀取到
            self._status(f"選擇 {self.data['sport_type']} / {self.data['venue']}", "accent")
            try:
                sport_dropdown = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "MainContent_drpkind"))
                )
                Select(sport_dropdown).select_by_visible_text(self.data["sport_type"])
                self._log(f"選擇運動類型: {self.data['sport_type']}")
            except Exception as e:
                self._done(False, f"運動類型選擇失敗: {e}")
                return
            time.sleep(0.6)

            try:
                venue_dropdown = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "MainContent_DropDownList1"))
                )
                Select(venue_dropdown).select_by_visible_text(self.data["venue"])
                self._log(f"選擇場地: {self.data['venue']}")
            except Exception as e:
                self._done(False, f"場地選擇失敗: {e}")
                return
            time.sleep(0.6)
            
            # 選兩次以防網頁沒讀取到
            try:
                venue_dropdown = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "MainContent_DropDownList1"))
                )
                Select(venue_dropdown).select_by_visible_text(self.data["venue"])
                self._log(f"選擇場地: {self.data['venue']}")
            except Exception as e:
                self._done(False, f"場地選擇失敗: {e}")
                return
            time.sleep(0.6)

            # Stage 6: 切換到目標日期 (今日 + 下週x2 + 前一天)
            self._status("切換到目標日期...", "accent")
            for btn_id in [
                "MainContent_BtnToday2",
                "MainContent_BtnNextW2",
                "MainContent_BtnNextW2",
                "MainContent_BtnPreD2",
            ]:
                self._safe_click((By.ID, btn_id))
                time.sleep(0.3)

            if not self.data["check"]:
                try:
                    self.driver.find_element(By.ID, "MainContent_BtnPreD2").click()
                    self._log("練習模式：再回退一天")
                    time.sleep(0.3)
                except Exception:
                    pass

            # Stage 7: 等待搶場時間 (僅正式模式)
            if self.data["check"]:
                self._status("等待搶場時間中...", "warning")
                self._wait_for_snatch_time()
                if self.cancel_flag.is_set():
                    self._done(False, "已取消")
                    return

            # Stage 8: 查詢
            self._status("查詢場地中...", "accent")
            start_time = time.time()
            self._safe_click((By.ID, "MainContent_Button1"))
            self._log("已點擊查詢")

            # Stage 9: 點申請
            apply_locator = (
                By.XPATH,
                f"//tr[{self.data['part']}]//button[contains(text(), '申請')]",
            )
            if not self._safe_click(apply_locator):
                self._done(False, "無法點擊申請按鈕")
                return
            self._log("已點擊申請按鈕")

            # Stage 10: 處理驗證碼
            self._status("辨識驗證碼中...", "accent")
            success, attempts = self._solve_captcha()
            if not success:
                self._done(False, "驗證碼處理失敗")
                return

            extra_wait = self.CAPTCHA_DELAY_MAP.get(attempts, 0)
            self._log(f"驗證成功（嘗試 {attempts} 次），額外等待 {extra_wait} 秒")

            # Stage 11: 等待驗證對話框消失並填寫借用原因
            self._status("填寫借用原因...", "accent")
            try:
                WebDriverWait(self.driver, 300).until_not(
                    EC.presence_of_element_located(
                        (By.XPATH, "/html/body/div/div/div[5]/button")
                    )
                )
                time.sleep(0.1)
                reason_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "MainContent_ReasonTextBox1"))
                )
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", reason_input
                )
                time.sleep(0.2)
                reason_input.clear()
                reason_input.send_keys(self.data["reason"])
                self._log(f"已輸入借用原因: {self.data['reason']}")
            except Exception as e:
                self._done(False, f"借用原因輸入失敗: {e}")
                return

            duration = time.time() - start_time

            # Stage 12: 最終送出
            if self.data["check"]:
                if self.data.get("skip_extra_wait", False):
                    self._log("已啟用「不演了」模式，跳過額外等待直接送出")
                else:
                    time.sleep(extra_wait)
                self._status("送出申請...", "accent")
                self._safe_click((By.ID, "MainContent_Button4"))
                self._log("已送出申請")
                msg = (
                    f"運動類型: {self.data['sport_type']}\n"
                    f"場地: {self.data['venue']}\n"
                    f"場次: {self.data['session_label']}\n"
                    f"搶場耗時: {duration:.2f} 秒\n"
                    f"驗證碼嘗試: {attempts} 次"
                )
                self._status("搶場成功！", "success")
                self._done(True, msg)
            else:
                msg = (
                    f"練習模式完成\n"
                    f"場地: {self.data['venue']}\n"
                    f"場次: {self.data['session_label']}\n"
                    f"耗時: {duration:.2f} 秒"
                )
                self._status("練習完成", "success")
                self._done(True, msg)

        except Exception as e:
            self._log(f"發生錯誤: {e}")
            self._status("執行失敗", "error")
            self._done(False, str(e))

        finally:
            # 結束後依「停留時間」設定處理瀏覽器
            if self.driver is not None:
                stop_secs = int(self.data.get("stop", -1))
                if stop_secs < 0:
                    self._log("頁面保持開啟，瀏覽器不會自動關閉，請完成後手動關閉")
                else:
                    if stop_secs > 0:
                        self._log(f"{stop_secs} 秒後自動關閉瀏覽器")
                    try:
                        # 用 cancel_flag 中斷，使用者取消可立即關
                        self.cancel_flag.wait(timeout=stop_secs)
                    except Exception:
                        pass
                    try:
                        self.driver.quit()
                    except Exception:
                        pass
                    self._log("瀏覽器已關閉")

    # ----- 時間等待 -----
    def _parse_target_time(self, h, m, s) -> datetime.datetime:
        now = datetime.datetime.now()
        target = now.replace(hour=int(h), minute=int(m), second=int(s), microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return target

    def _wait_for_schedule(self):
        """使用本機時間倒數到定時啟動時間"""
        target = self._parse_target_time(
            self.data["schedule_hour"],
            self.data["schedule_minute"],
            self.data["schedule_second"],
        )
        self._log(f"等待定時啟動 - 目標時間 {target.strftime('%H:%M:%S')}")
        last_shown = -1
        while not self.cancel_flag.is_set():
            now = datetime.datetime.now()
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                self._log("到達定時啟動時間")
                break
            sec_int = int(remaining)
            if sec_int != last_shown:
                last_shown = sec_int
                hrs, rem = divmod(sec_int, 3600)
                mins, secs = divmod(rem, 60)
                self._status(
                    f"等待定時啟動  {hrs:02d}:{mins:02d}:{secs:02d}", "warning"
                )
            # 用 Event.wait 讓取消可即時生效
            self.cancel_flag.wait(timeout=min(remaining, 0.1))

    def _wait_for_snatch_time(self):
        """使用校正後時間倒數到搶場時間（更精準）"""
        target = self._parse_target_time(
            self.data["snatch_hour"],
            self.data["snatch_minute"],
            self.data["snatch_second"],
        )
        self._log(
            f"等待搶場時間 - 目標 {target.strftime('%H:%M:%S')} "
            f"(時間偏移 {self.time_sync.offset_seconds:+.2f}s)"
        )
        while not self.cancel_flag.is_set():
            now = self.time_sync.now()
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                break
            if remaining > 2:
                self._status(f"倒數搶場  {remaining:.1f} 秒", "warning")
                self.cancel_flag.wait(timeout=min(remaining - 1, 0.1))
            else:
                self._status(f"倒數搶場  {remaining:.2f} 秒", "warning")
                self.cancel_flag.wait(timeout=0.005)

    # ----- Selenium 工具 -----
    def _safe_click(self, locator, retries=3, delay=0.2) -> bool:
        last_err = None
        for i in range(retries):
            if self.cancel_flag.is_set():
                return False
            try:
                elem = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable(locator)
                )
                self.driver.execute_script("arguments[0].scrollIntoView(true);", elem)
                time.sleep(delay)
                self.driver.execute_script("arguments[0].click();", elem)
                return True
            except Exception as e:
                last_err = e
                # 只在最後一次失敗時印詳細訊息；前面的重試屬正常自我修復
                if i == retries - 1:
                    self._log(f"點擊失敗（已重試 {retries} 次）: {e}")
                else:
                    short = str(e).split("\n")[0][:80]
                    self._log(f"重新嘗試點擊 ({i+1}/{retries}): {short}")
                time.sleep(0.4)
        return False

    def _solve_captcha(self, max_retries=10):
        if not HAS_DDDDOCR:
            self._log("未安裝 ddddocr，無法辨識驗證碼")
            return False, 0
        try:
            ocr = ddddocr.DdddOcr(beta=True, show_ad=False)
        except Exception as e:
            self._log(f"初始化 OCR 失敗: {e}")
            return False, 0

        tmp_path = os.path.join(tempfile.gettempdir(), "ndhu_captcha.png")

        for attempt in range(max_retries):
            if self.cancel_flag.is_set():
                return False, attempt
            try:
                captcha_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "txtCaptchaValue"))
                )
                captcha_img = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "imgCaptcha"))
                )
                captcha_img.screenshot(tmp_path)
                with open(tmp_path, "rb") as f:
                    image_data = f.read()
                captcha_text = ocr.classification(image_data).strip()
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

                captcha_input.clear()
                captcha_input.send_keys(captcha_text)
                self._log(f"嘗試驗證碼 ({attempt+1}/{max_retries}): {captcha_text}")

                confirm_btn = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "/html/body/div/div/div[5]/button")
                    )
                )
                self.driver.execute_script("arguments[0].click();", confirm_btn)
                time.sleep(0.2)

                try:
                    WebDriverWait(self.driver, 0.5).until_not(
                        EC.presence_of_element_located(
                            (By.XPATH, "/html/body/div/div/div[5]/button")
                        )
                    )
                    self._log("驗證碼正確")
                    return True, attempt + 1
                except Exception:
                    self._log("驗證失敗，刷新驗證碼")
                    try:
                        refresh_btn = self.driver.find_element(
                            By.XPATH,
                            "//*[@id='txtCaptchaValue']/following::button[1]",
                        )
                        self.driver.execute_script("arguments[0].click();", refresh_btn)
                    except Exception:
                        pass
                    time.sleep(0.3)
            except Exception as e:
                self._log(f"驗證流程錯誤: {e}")
                time.sleep(0.4)
        return False, max_retries


# ========================================================================
# VenueBookingGUI — 主介面
# ========================================================================
class VenueBookingGUI:
    # ----- 配色 (明亮主題) -----
    BG = "#f3f4f6"           # 視窗背景 - 淺灰
    CARD = "#ffffff"         # 卡片背景 - 白
    CARD_BORDER = "#d1d5db"  # 卡片邊框 - 中淺灰
    ACCENT = "#6d28d9"       # 主色 - 紫
    ACCENT_HOVER = "#5b21b6" # 主色 hover - 深紫
    SUCCESS = "#059669"      # 成功 - 綠
    ERROR = "#dc2626"        # 錯誤 - 紅
    WARNING = "#d97706"      # 警告 - 橘
    TEXT = "#111827"         # 主文字 - 深灰黑
    TEXT_DIM = "#6b7280"     # 次要文字 - 中灰
    INPUT_BG = "#ffffff"     # 輸入框背景 - 白
    LOG_BG = "#f9fafb"       # 日誌背景 - 極淺灰
    BUTTON_TEXT = "#ffffff"  # 按鈕文字 - 白

    FONT = "標楷體"

    STAY_OPTIONS = ["不自動關閉", "10 秒", "20 秒", "30 秒", "60 秒", "120 秒"]

    SESSIONS = [
        ("06:00-08:00", 3), ("07:00-09:00", 4), ("08:00-10:00", 5),
        ("09:00-11:00", 6), ("10:00-12:00", 7), ("11:00-13:00", 8),
        ("12:00-14:00", 9), ("13:00-15:00", 10), ("14:00-16:00", 11),
        ("15:00-17:00", 12), ("16:00-18:00", 13), ("17:00-19:00", 14),
        ("18:00-20:00", 15), ("19:00-21:00", 16), ("20:00-22:00", 17),
        ("21:00-23:00", 18), ("22:00-23:00", 19),
    ]

    VENUES_DICT = {
        "排球": [
            "VOL0A排球場A-女", "VOL0B排球場B-男", "VOL0C排球場C-女", "VOL0D排球場D-男",
            "VOL0E排球場E-女", "VOL0F排球場F-男", "VOL0G排球場G-女", "VOL0H排球場H-男",
            "VOL0J排球場L-女 (集賢館場地)", "VOL0K排球場K-男 (集賢館場地)",
            "VOLR1排球場I-女 (原R1)", "VOLR2排球場J-男 (原R2)",
        ],
        "籃球": ["BSK0A籃球場A", "BSK0B籃球場B", "BSK0C籃球場C"],
        "體育館": [
            "XGMB1壽館場B-羽1", "XGMB2壽館場B-羽2", "XGMB3壽館場B-羽3", "XGMB4壽館場B-羽4",
            "XGMC1壽館場C-排1", "XGMC2壽館場C-排2", "XGMC3壽館場C-排3", "XGMC4壽館場C-排4",
            "XGYMA壽館場A-籃球",
        ],
        "操場": ["TRACK01操場跑道", "TRACK02操場草地"],
        "網球場": ["TEN01網球場A", "TEN02網球場B"],
        "戶外大型球場": ["OUT01大型球場A", "OUT02大型球場B"],
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("搶場機器人 7.0")
        self.root.geometry("600x800")
        self.root.minsize(560, 480)
        self.root.configure(bg=self.BG)

        self.cfg = Config.load()

        self.queue: queue.Queue = queue.Queue()
        self.worker: BookingWorker | None = None
        self.worker_thread: threading.Thread | None = None

        self._init_styles()
        self._build_vars()
        self._build_form_view()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- 樣式 -----
    def _init_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "App.TCombobox",
            fieldbackground=self.INPUT_BG,
            background=self.INPUT_BG,
            foreground=self.TEXT,
            arrowcolor=self.ACCENT,
            bordercolor=self.CARD_BORDER,
            lightcolor=self.CARD_BORDER,
            darkcolor=self.CARD_BORDER,
            # 選中後的文字反白背景設成跟輸入框一樣 (=隱形)
            selectbackground=self.INPUT_BG,
            selectforeground=self.TEXT,
            padding=4,
        )
        style.map(
            "App.TCombobox",
            fieldbackground=[("readonly", self.INPUT_BG), ("disabled", "#f3f4f6")],
            foreground=[("disabled", self.TEXT_DIM)],
            bordercolor=[("focus", self.ACCENT)],
            # 任何狀態下都讓選取反白保持隱形
            selectbackground=[("readonly", self.INPUT_BG), ("focus", self.INPUT_BG), ("!focus", self.INPUT_BG)],
            selectforeground=[("readonly", self.TEXT), ("focus", self.TEXT), ("!focus", self.TEXT)],
        )

        self.root.option_add("*TCombobox*Listbox.background", self.INPUT_BG)
        self.root.option_add("*TCombobox*Listbox.foreground", self.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", self.ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", self.BUTTON_TEXT)
        self.root.option_add("*TCombobox*Listbox.font", (self.FONT, 13))

    # ----- 共用建構元 -----
    def _label(self, parent, text, size=13, color=None, weight="normal"):
        return tk.Label(
            parent, text=text, bg=parent.cget("bg"),
            fg=color or self.TEXT,
            font=(self.FONT, size, weight),
        )

    def _entry(self, parent, textvariable, show=None, width=None, justify="left"):
        e = tk.Entry(
            parent, textvariable=textvariable, show=show or "",
            bg=self.INPUT_BG, fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat", bd=0,
            font=(self.FONT, 14), justify=justify,
            highlightthickness=1,
            highlightbackground=self.CARD_BORDER,
            highlightcolor=self.ACCENT,
        )
        if width:
            e.config(width=width)
        return e

    def _combo(self, parent, textvariable, values, width=None):
        c = ttk.Combobox(
            parent, textvariable=textvariable, values=values,
            state="readonly", style="App.TCombobox",
            font=(self.FONT, 13),
        )
        if width:
            c.config(width=width)

        # 禁用滑鼠滾輪改變下拉選單的值（避免誤觸）
        # 改為將滾輪事件轉發給整個表單捲動
        def _on_wheel(event, _self=self):
            canvas = getattr(_self, "_scroll_canvas", None)
            if canvas is not None:
                try:
                    canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                except tk.TclError:
                    pass
            return "break"
        c.bind("<MouseWheel>", _on_wheel)
        # Linux 上的滾輪事件
        c.bind("<Button-4>", lambda e, _s=self: (_s._scroll_canvas.yview_scroll(-1, "units") if getattr(_s, "_scroll_canvas", None) else None) or "break")
        c.bind("<Button-5>", lambda e, _s=self: (_s._scroll_canvas.yview_scroll(1, "units") if getattr(_s, "_scroll_canvas", None) else None) or "break")

        # 選完後馬上把焦點丟給主視窗，徹底消除選中文字的反白
        def _clear_focus(event, _self=self):
            try:
                _self.root.focus_set()
            except tk.TclError:
                pass
        c.bind("<<ComboboxSelected>>", _clear_focus, add="+")
        return c

    def _card(self, parent, title):
        wrap = tk.Frame(parent, bg=self.BG)
        wrap.pack(fill="x", padx=16, pady=8)

        title_lbl = tk.Label(
            wrap, text=title, bg=self.BG,
            fg=self.ACCENT, font=(self.FONT, 13, "bold"),
            anchor="w",
        )
        title_lbl.pack(fill="x", padx=2, pady=(0, 4))

        card = tk.Frame(
            wrap, bg=self.CARD,
            highlightbackground=self.CARD_BORDER,
            highlightthickness=1,
        )
        card.pack(fill="x")
        inner = tk.Frame(card, bg=self.CARD)
        inner.pack(fill="x", padx=16, pady=14)
        return inner

    def _primary_button(self, parent, text, command):
        btn = tk.Button(
            parent, text=text, command=command,
            bg=self.ACCENT, fg=self.BUTTON_TEXT,
            activebackground=self.ACCENT_HOVER, activeforeground=self.BUTTON_TEXT,
            relief="flat", bd=0, cursor="hand2",
            font=(self.FONT, 16, "bold"), padx=20, pady=12,
        )
        btn.bind("<Enter>", lambda e: btn.config(bg=self.ACCENT_HOVER))
        btn.bind("<Leave>", lambda e: btn.config(bg=self.ACCENT))
        return btn

    def _secondary_button(self, parent, text, command):
        btn = tk.Button(
            parent, text=text, command=command,
            bg=self.CARD, fg=self.TEXT,
            activebackground=self.CARD_BORDER, activeforeground=self.TEXT,
            relief="flat", bd=1, cursor="hand2",
            highlightbackground=self.CARD_BORDER, highlightthickness=1,
            font=(self.FONT, 13), padx=14, pady=6,
        )
        return btn

    def _time_picker(self, parent, h_var, m_var, s_var, on_change=None):
        """產生「HH:MM:SS」三個下拉的組合元件"""
        f = tk.Frame(parent, bg=parent.cget("bg"))
        hours = [f"{i:02d}" for i in range(24)]
        mins_secs = [f"{i:02d}" for i in range(60)]

        def add_box(var, values):
            cb = self._combo(f, var, values, width=4)
            cb.pack(side="left", padx=3)
            if on_change:
                cb.bind("<<ComboboxSelected>>", lambda e: on_change(), add="+")
            return cb

        add_box(h_var, hours)
        self._label(f, " : ", size=16, weight="bold").pack(side="left")
        add_box(m_var, mins_secs)
        self._label(f, " : ", size=16, weight="bold").pack(side="left")
        add_box(s_var, mins_secs)
        return f

    # ----- 變數 -----
    def _build_vars(self):
        c = self.cfg
        self.email_var = tk.StringVar(value=c["email"])
        self.password_var = tk.StringVar(value=c["password"])
        self.remember_pw_var = tk.BooleanVar(value=c["remember_password"])
        self.sport_var = tk.StringVar(value=c["sport"])
        self.venue_var = tk.StringVar(value=c["venue"])
        self.session_var = tk.StringVar(value=c["session"])
        self.reason_var = tk.StringVar(value=c["reason"])
        self.check_var = tk.BooleanVar(value=c["check"])
        self.use_schedule_var = tk.BooleanVar(value=c["use_schedule"])
        self.sched_h = tk.StringVar(value=c["schedule_hour"])
        self.sched_m = tk.StringVar(value=c["schedule_minute"])
        self.sched_s = tk.StringVar(value=c["schedule_second"])
        self.snatch_h = tk.StringVar(value=c["snatch_hour"])
        self.snatch_m = tk.StringVar(value=c["snatch_minute"])
        self.snatch_s = tk.StringVar(value=c["snatch_second"])
        self.stay_var = tk.StringVar(value=c["stay_mode"])
        self.skip_wait_var = tk.BooleanVar(value=c.get("skip_extra_wait", False))
        self.show_pw = False

    # ----- 表單畫面 -----
    def _build_form_view(self):
        if hasattr(self, "main") and self.main is not None:
            self.main.destroy()

        # 清掉執行畫面的元件引用，避免背景 worker 訊息打到舊元件
        self.status_label = None
        self.log_text = None
        self.cancel_btn = None

        self.main = tk.Frame(self.root, bg=self.BG)
        self.main.pack(fill="both", expand=True)

        # 可捲動容器
        self._scroll_canvas = tk.Canvas(self.main, bg=self.BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.main, orient="vertical", command=self._scroll_canvas.yview)
        self._scroll_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._scroll_canvas.pack(side="left", fill="both", expand=True)

        form = tk.Frame(self._scroll_canvas, bg=self.BG)
        form_window = self._scroll_canvas.create_window((0, 0), window=form, anchor="nw")

        def _on_form_configure(event):
            self._scroll_canvas.configure(scrollregion=self._scroll_canvas.bbox("all"))

        def _on_canvas_configure(event):
            self._scroll_canvas.itemconfig(form_window, width=event.width)

        form.bind("<Configure>", _on_form_configure)
        self._scroll_canvas.bind("<Configure>", _on_canvas_configure)

        # 滾輪滾動 - 滑鼠進入時才綁定，離開解除，避免影響到日誌區的捲動
        def _on_mousewheel(event):
            self._scroll_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_wheel(event):
            self._scroll_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_wheel(event):
            self._scroll_canvas.unbind_all("<MouseWheel>")

        self._scroll_canvas.bind("<Enter>", _bind_wheel)
        self._scroll_canvas.bind("<Leave>", _unbind_wheel)

        # 標題
        header = tk.Frame(form, bg=self.BG)
        header.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(
            header, text="搶場機器人", bg=self.BG,
            fg=self.TEXT, font=(self.FONT, 22, "bold"),
        ).pack(side="left")
        tk.Label(
            header, text="v7.0", bg=self.BG, fg=self.ACCENT,
            font=(self.FONT, 13, "bold"),
        ).pack(side="left", padx=(10, 0), pady=(6, 0))

        # === 帳號卡 ===
        c1 = self._card(form, "帳號 / 密碼")
        self._label(c1, "學號").grid(row=0, column=0, sticky="w", pady=(0, 4))
        emailf = tk.Frame(c1, bg=self.CARD)
        emailf.grid(row=1, column=0, sticky="ew", pady=(0, 10), columnspan=2)
        self._entry(emailf, self.email_var, width=18).pack(side="left")
        self._label(emailf, " @gms.ndhu.edu.tw", size=12, color=self.TEXT_DIM).pack(side="left")

        self._label(c1, "密碼").grid(row=2, column=0, sticky="w", pady=(0, 4))
        pwf = tk.Frame(c1, bg=self.CARD)
        pwf.grid(row=3, column=0, sticky="ew")
        self.password_entry = self._entry(pwf, self.password_var, show="*", width=24)
        self.password_entry.pack(side="left")
        self.eye_btn = tk.Button(
            pwf, text="👁", command=self._toggle_password,
            bg=self.INPUT_BG, fg=self.TEXT,
            activebackground=self.CARD_BORDER, activeforeground=self.TEXT,
            relief="flat", bd=0, cursor="hand2",
            highlightbackground=self.CARD_BORDER, highlightthickness=1,
            font=(self.FONT, 13),
        )
        self.eye_btn.pack(side="left", padx=(8, 0))

        rem_chk = tk.Checkbutton(
            c1, text="記住密碼", variable=self.remember_pw_var,
            bg=self.CARD, fg=self.TEXT,
            activebackground=self.CARD, activeforeground=self.TEXT,
            selectcolor=self.INPUT_BG,
            font=(self.FONT, 12), bd=0,
        )
        rem_chk.grid(row=3, column=1, sticky="w", padx=(14, 0))
        c1.columnconfigure(0, weight=1)

        # === 場地卡 ===
        c2 = self._card(form, "運動類型 / 場地 / 場次")
        self._label(c2, "運動類型").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.sport_combo = self._combo(c2, self.sport_var, list(self.VENUES_DICT.keys()))
        self.sport_combo.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.sport_combo.bind("<<ComboboxSelected>>", self._on_sport_change, add="+")

        self._label(c2, "場地").grid(row=2, column=0, sticky="w", pady=(0, 4))
        venues = self.VENUES_DICT.get(self.sport_var.get(), [])
        self.venue_combo = self._combo(c2, self.venue_var, venues)
        self.venue_combo.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        if self.venue_var.get() not in venues and venues:
            self.venue_var.set(venues[0])

        self._label(c2, "場次").grid(row=4, column=0, sticky="w", pady=(0, 4))
        self.session_combo = self._combo(
            c2, self.session_var, [label for label, _ in self.SESSIONS]
        )
        self.session_combo.grid(row=5, column=0, sticky="ew")
        c2.columnconfigure(0, weight=1)

        # === 時間卡 ===
        c3 = self._card(form, "定時啟動 / 搶場時間")

        sched_chk = tk.Checkbutton(
            c3, text="啟用定時啟動 (到時間才執行整個流程)",
            variable=self.use_schedule_var,
            bg=self.CARD, fg=self.TEXT,
            activebackground=self.CARD, activeforeground=self.TEXT,
            selectcolor=self.INPUT_BG,
            font=(self.FONT, 13, "bold"), bd=0,
            command=self._update_sched_state,
        )
        sched_chk.pack(anchor="w", pady=(0, 8))

        sched_row = tk.Frame(c3, bg=self.CARD)
        sched_row.pack(anchor="w", pady=(0, 16))
        self._label(sched_row, "啟動時間  ", size=13, color=self.TEXT_DIM).pack(side="left")
        self.sched_picker = self._time_picker(sched_row, self.sched_h, self.sched_m, self.sched_s)
        self.sched_picker.pack(side="left")

        snatch_lbl = tk.Frame(c3, bg=self.CARD)
        snatch_lbl.pack(anchor="w", pady=(0, 8))
        tk.Label(
            snatch_lbl, text="搶場時間 (網頁上按查詢的時間)",
            bg=self.CARD, fg=self.TEXT, font=(self.FONT, 13, "bold"),
        ).pack(side="left")

        snatch_row = tk.Frame(c3, bg=self.CARD)
        snatch_row.pack(anchor="w")
        self._label(snatch_row, "搶場時間  ", size=13, color=self.TEXT_DIM).pack(side="left")
        self._time_picker(snatch_row, self.snatch_h, self.snatch_m, self.snatch_s).pack(side="left")

        # === 其他卡 ===
        c4 = self._card(form, "其他設定")
        self._label(c4, "借用原因").pack(anchor="w", pady=(0, 4))
        self._entry(c4, self.reason_var).pack(fill="x", pady=(0, 10), ipady=2)

        self._label(c4, "完成後頁面停留時間").pack(anchor="w", pady=(0, 4))
        self._combo(c4, self.stay_var, self.STAY_OPTIONS).pack(fill="x", pady=(0, 10))

        self.check_btn = tk.Checkbutton(
            c4,
            variable=self.check_var,
            bg=self.CARD, fg=self.TEXT,
            activebackground=self.CARD, activeforeground=self.TEXT,
            selectcolor=self.INPUT_BG,
            font=(self.FONT, 13, "bold"), bd=0,
            command=self._update_check_text,
        )
        self.check_btn.pack(anchor="w", pady=(4, 0))
        self._update_check_text()

        # 「不演了」按鈕 - 跳過驗證碼後的額外等待時間，搶到後直接送出
        self.skip_wait_btn = tk.Checkbutton(
            c4,
            variable=self.skip_wait_var,
            bg=self.CARD, fg=self.ERROR,
            activebackground=self.CARD, activeforeground=self.ERROR,
            selectcolor=self.INPUT_BG,
            font=(self.FONT, 13, "bold"), bd=0,
            command=self._update_skip_wait_text,
        )
        self.skip_wait_btn.pack(anchor="w", pady=(8, 0))
        skip_tip = tk.Label(
            c4,
            text="開啟後將捨棄驗證碼後的等待，搶到場立刻按下送出",
            bg=self.CARD, fg=self.TEXT_DIM,
            font=(self.FONT, 10),
        )
        skip_tip.pack(anchor="w", pady=(0, 4))
        self._update_skip_wait_text()

        # 提示文字
        tip = tk.Label(
            form,
            text="預設搶兩週後前一天的場 (練習模式則往前再多一天)",
            bg=self.BG, fg=self.TEXT_DIM, font=(self.FONT, 10),
        )
        tip.pack(pady=(8, 4))

        # 開始按鈕
        self._primary_button(form, "開始搶場", self._on_submit).pack(
            pady=(6, 16), padx=16, fill="x", ipady=4
        )

        self._update_sched_state()

    # ----- 互動處理 -----
    def _toggle_password(self):
        self.show_pw = not self.show_pw
        self.password_entry.config(show="" if self.show_pw else "*")

    def _on_sport_change(self, event=None):
        sport = self.sport_var.get()
        venues = self.VENUES_DICT.get(sport, [])
        self.venue_combo["values"] = venues
        if venues:
            self.venue_var.set(venues[0])

    def _update_check_text(self):
        self.check_btn.config(
            text="✔ 正式搶場" if self.check_var.get()
            else "✘ 練習模式 (兩週前兩天的場, 不送出)"
        )

    def _update_skip_wait_text(self):
        self.skip_wait_btn.config(
            text="🔥 不演了 (啟動)" if self.skip_wait_var.get()
            else "💤 不演了"
        )

    def _update_sched_state(self):
        # 啟動定時時，自然狀態 (其實 readonly Combobox 不能 disable，這裡僅用 hint)
        enabled = self.use_schedule_var.get()
        for child in self.sched_picker.winfo_children():
            if isinstance(child, ttk.Combobox):
                child.configure(state="readonly" if enabled else "disabled")

    # ----- 提交 -----
    def _validate_inputs(self) -> str | None:
        if not self.email_var.get().strip():
            return "請填寫學號"
        if not self.password_var.get():
            return "請填寫密碼"
        if not self.reason_var.get().strip():
            return "請填寫借用原因"
        return None

    def _on_submit(self):
        err = self._validate_inputs()
        if err:
            messagebox.showerror("錯誤", err)
            return

        session_label = self.session_var.get()
        session_map = dict(self.SESSIONS)
        part = session_map.get(session_label, 14)

        # 把「停留時間」字串轉成秒數 (-1 代表不自動關閉)
        stay_mode = self.stay_var.get()
        if stay_mode == "不自動關閉":
            stop_seconds = -1
        else:
            try:
                stop_seconds = int(stay_mode.replace(" 秒", "").strip())
            except Exception:
                stop_seconds = -1

        # 儲存設定
        Config.save({
            "email": self.email_var.get().strip(),
            "password": self.password_var.get(),
            "remember_password": self.remember_pw_var.get(),
            "sport": self.sport_var.get(),
            "venue": self.venue_var.get(),
            "session": session_label,
            "reason": self.reason_var.get(),
            "check": self.check_var.get(),
            "use_schedule": self.use_schedule_var.get(),
            "schedule_hour": self.sched_h.get(),
            "schedule_minute": self.sched_m.get(),
            "schedule_second": self.sched_s.get(),
            "snatch_hour": self.snatch_h.get(),
            "snatch_minute": self.snatch_m.get(),
            "snatch_second": self.snatch_s.get(),
            "stay_mode": stay_mode,
            "skip_extra_wait": self.skip_wait_var.get(),
        })

        data = {
            "email": self.email_var.get().strip(),
            "password": self.password_var.get(),
            "sport_type": self.sport_var.get(),
            "venue": self.venue_var.get(),
            "session_label": session_label,
            "part": part,
            "reason": self.reason_var.get(),
            "check": self.check_var.get(),
            "use_schedule": self.use_schedule_var.get(),
            "schedule_hour": self.sched_h.get(),
            "schedule_minute": self.sched_m.get(),
            "schedule_second": self.sched_s.get(),
            "snatch_hour": self.snatch_h.get(),
            "snatch_minute": self.snatch_m.get(),
            "snatch_second": self.snatch_s.get(),
            "stay_mode": stay_mode,
            "stop": stop_seconds,
            "skip_extra_wait": self.skip_wait_var.get(),
        }

        self._switch_to_running_view(data)

    # ----- 執行畫面 -----
    def _switch_to_running_view(self, data: dict):
        self.main.destroy()
        self.main = tk.Frame(self.root, bg=self.BG)
        self.main.pack(fill="both", expand=True)

        # 標題
        header = tk.Frame(self.main, bg=self.BG)
        header.pack(fill="x", padx=16, pady=(18, 6))
        tk.Label(
            header, text="搶場進行中", bg=self.BG,
            fg=self.TEXT, font=(self.FONT, 22, "bold"),
        ).pack(side="left")

        # 狀態卡
        sw = tk.Frame(self.main, bg=self.BG)
        sw.pack(fill="x", padx=16, pady=(8, 10))
        scard = tk.Frame(
            sw, bg=self.CARD,
            highlightbackground=self.CARD_BORDER, highlightthickness=1,
        )
        scard.pack(fill="x")
        self.status_label = tk.Label(
            scard, text="準備中...",
            bg=self.CARD, fg=self.ACCENT,
            font=(self.FONT, 18, "bold"),
            pady=28,
        )
        self.status_label.pack(fill="x", padx=16)

        # 摘要
        sub = tk.Frame(self.main, bg=self.BG)
        sub.pack(fill="x", padx=16, pady=(0, 10))
        summary = (
            f"{data['sport_type']}  ·  {data['venue']}\n"
            f"場次 {data['session_label']}    模式 "
            f"{'正式' if data['check'] else '練習'}"
        )
        if data["use_schedule"]:
            summary += (
                f"\n定時啟動  {data['schedule_hour']}:{data['schedule_minute']}:{data['schedule_second']}"
            )
        if data["check"]:
            summary += (
                f"    搶場  {data['snatch_hour']}:{data['snatch_minute']}:{data['snatch_second']}"
            )
        summary += f"\n完成後停留: {data.get('stay_mode', '不自動關閉')}"
        tk.Label(
            sub, text=summary, bg=self.BG, fg=self.TEXT_DIM,
            font=(self.FONT, 12), justify="left",
        ).pack(anchor="w")

        # 日誌卡
        lw = tk.Frame(self.main, bg=self.BG)
        lw.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        tk.Label(
            lw, text="執行日誌", bg=self.BG,
            fg=self.ACCENT, font=(self.FONT, 13, "bold"), anchor="w",
        ).pack(fill="x")
        log_card = tk.Frame(
            lw, bg=self.CARD,
            highlightbackground=self.CARD_BORDER, highlightthickness=1,
        )
        log_card.pack(fill="both", expand=True, pady=(4, 0))
        self.log_text = scrolledtext.ScrolledText(
            log_card, bg=self.LOG_BG, fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat", bd=0,
            font=("Consolas", 11), wrap="word",
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)

        # 取消 / 返回
        btnf = tk.Frame(self.main, bg=self.BG)
        btnf.pack(fill="x", padx=16, pady=(0, 18))
        self.cancel_btn = self._primary_button(btnf, "取消", self._on_cancel)
        cancel_red = "#b91c1c"
        self.cancel_btn.config(bg=self.ERROR, activebackground=cancel_red)
        self.cancel_btn.bind("<Enter>", lambda e: self.cancel_btn.config(bg=cancel_red))
        self.cancel_btn.bind("<Leave>", lambda e: self.cancel_btn.config(bg=self.ERROR))
        self.cancel_btn.pack(fill="x", ipady=6)

        # 啟動 worker
        self.worker = BookingWorker(data, self.queue)
        self.worker_thread = threading.Thread(target=self.worker.run, daemon=True)
        self.worker_thread.start()

        # 開始輪詢 queue
        self.root.after(50, self._poll_queue)

    # ----- queue 輪詢 -----
    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass

        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.root.after(80, self._poll_queue)
        else:
            # 把 queue 殘留訊息消化完
            try:
                while True:
                    self._handle_message(self.queue.get_nowait())
            except queue.Empty:
                pass

    def _widget_alive(self, w) -> bool:
        try:
            return bool(w) and bool(w.winfo_exists())
        except Exception:
            return False

    def _handle_message(self, msg):
        kind = msg[0]
        try:
            if kind == "status":
                _, text, color = msg
                color_map = {
                    "accent": self.ACCENT,
                    "warning": self.WARNING,
                    "success": self.SUCCESS,
                    "error": self.ERROR,
                }
                if self._widget_alive(getattr(self, "status_label", None)):
                    self.status_label.config(text=text, fg=color_map.get(color, self.ACCENT))
            elif kind == "log":
                _, text = msg
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                if self._widget_alive(getattr(self, "log_text", None)):
                    self.log_text.config(state="normal")
                    self.log_text.insert("end", f"[{ts}] {text}\n")
                    self.log_text.see("end")
                    self.log_text.config(state="disabled")
            elif kind == "done":
                _, success, message = msg
                if success:
                    messagebox.showinfo("成功", f"完成！\n\n{message}")
                else:
                    messagebox.showerror("失敗", f"未能完成搶場：\n\n{message}")
                if self._widget_alive(getattr(self, "cancel_btn", None)):
                    self.cancel_btn.config(text="返回主畫面", command=self._build_form_view,
                                           bg=self.ACCENT, activebackground=self.ACCENT_HOVER)
                    self.cancel_btn.bind("<Enter>", lambda e: self.cancel_btn.config(bg=self.ACCENT_HOVER))
                    self.cancel_btn.bind("<Leave>", lambda e: self.cancel_btn.config(bg=self.ACCENT))
        except tk.TclError:
            # 元件已被銷毀就忽略，避免崩潰
            pass

    # ----- 取消 -----
    def _on_cancel(self):
        if self.worker is not None:
            if messagebox.askyesno("確認", "確定要取消執行嗎？"):
                self.worker.cancel()

    # ----- 關閉視窗 -----
    def _on_close(self):
        if self.worker_thread is not None and self.worker_thread.is_alive():
            if not messagebox.askyesno("確認", "搶場仍在進行中，確定關閉？"):
                return
            if self.worker is not None:
                self.worker.cancel()
        self.root.destroy()


if __name__ == "__main__":
    if not HAS_DDDDOCR:
        print("[警告] 未安裝 ddddocr，將無法辨識驗證碼。請執行: pip install ddddocr")
    root = tk.Tk()
    app = VenueBookingGUI(root)
    root.mainloop()
