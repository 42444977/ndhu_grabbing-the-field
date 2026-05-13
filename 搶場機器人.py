#前置作業 安裝對應的 WebDriver
#下載selenium

import tkinter as tk
from tkinter import ttk, messagebox
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import ddddocr
import os

class VenueBookingGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("搶場機器人")
        self.root.geometry("430x680")

        # 樣式配置
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.style.configure("TLabel",  foreground="#000000", font=("標楷體", 14))
        self.style.configure("TEntry",  foreground="#000000", font=("標楷體", 14))
        self.style.configure("TCombobox", foreground="#000000", font=("標楷體", 14))
        self.style.configure("TButton", background="#3b82f6", foreground="#ffffff", font=("標楷體", 14, "bold"))
        self.style.map("TButton", background=[("active", "#2563eb")])
        self.style.configure("TCheckbutton",  foreground="#000000", font=("標楷體", 14))
        self.style.layout("Frameless.TCheckbutton",
            [('Checkbutton.label', {'sticky': 'nswe'})]
        )

        # 主框架
        self.main_frame = ttk.Frame(self.root, padding=20)
        self.main_frame.pack(fill="both", expand=True)

        # 帳號
        ttk.Label(self.main_frame, text="帳號").pack(anchor="w", pady=5)
        self.email_var = tk.StringVar(value="")
        email_frame = ttk.Frame(self.main_frame)
        email_frame.pack(fill="x", pady=5)
        email_entry_frame = ttk.Frame(email_frame)
        email_entry_frame.pack(anchor="center")
        self.email_entry = ttk.Entry(email_entry_frame, textvariable=self.email_var, width=30, justify="center")
        self.email_entry.pack(side="left")
        ttk.Label(email_entry_frame, text="@gms.ndhu.edu.tw", font=("標楷體", 12), foreground="#000000").pack(side="left", padx=5)

        # 密碼
        ttk.Label(self.main_frame, text="密碼").pack(anchor="w", pady=5)
        self.password_var = tk.StringVar(value="")
        pw_frame = ttk.Frame(self.main_frame)
        pw_frame.pack(fill="x", pady=5)
        pw_entry_frame = ttk.Frame(pw_frame)
        pw_entry_frame.pack(anchor="center")
        self.password_entry = ttk.Entry(pw_entry_frame, textvariable=self.password_var, show="*", width=45, justify="center")
        self.password_entry.pack(side="left")
        self.show_pw = False
        self.eye_btn = tk.Button(pw_entry_frame, text="👁", width=3, height=1, command=self.toggle_password, font=("標楷體", 12))
        self.eye_btn.pack(side="left", padx=3, pady=1)

        # 運動類型
        ttk.Label(self.main_frame, text="運動類型").pack(anchor="w", pady=5)
        self.sport_var = tk.StringVar(value="排球")
        self.sport_combo = ttk.Combobox(
            self.main_frame,
            textvariable=self.sport_var,
            state="readonly",
            values=["籃球", "排球", "操場", "體育館", "網球場", "戶外大型球場"]
        )
        self.sport_combo.pack(fill="x", pady=5)

        # 定義各運動類型的場地清單
        self.venues_dict = {
            "排球": [
                "VOL0A排球場A-女",
                "VOL0B排球場B-男",
                "VOL0C排球場C-女",
                "VOL0D排球場D-男",
                "VOL0E排球場E-女",
                "VOL0F排球場F-男",
                "VOL0G排球場G-女",
                "VOL0H排球場H-男",
                "VOL0J排球場L-女 (集賢館場地)",
                "VOL0K排球場K-男 (集賢館場地)",
                "VOLR1排球場I-女 (原R1)",
                "VOLR2排球場J-男 (原R2)"
            ],
            "籃球": [
                "BSK0A籃球場A",
                "BSK0B籃球場B",
                "BSK0C籃球場C"
            ],
            "體育館": [
                "XGMB1壽館場B-羽1",
                "XGMB2壽館場B-羽2",
                "XGMB3壽館場B-羽3",
                "XGMB4壽館場B-羽4",
                "XGMC1壽館場C-排1",
                "XGMC2壽館場C-排2",
                "XGMC3壽館場C-排3",
                "XGMC4壽館場C-排4",
                "XGYMA壽館場A-籃球"
            ],
            "操場": [
                "TRACK01操場跑道",
                "TRACK02操場草地"
            ],
            "網球場": [
                "TEN01網球場A",
                "TEN02網球場B"
            ],
            "戶外大型球場": [
                "OUT01大型球場A",
                "OUT02大型球場B"
            ]
        }

        # 場地
        ttk.Label(self.main_frame, text="場地").pack(anchor="w", pady=5)
        self.venue_var = tk.StringVar(value=self.venues_dict["排球"][5])
        self.venue_combo = ttk.Combobox(
            self.main_frame,
            textvariable=self.venue_var,
            state="readonly",
            values=self.venues_dict["排球"]
        )
        self.venue_combo.pack(fill="x", pady=5)

        # 綁定運動類型變更事件
        self.sport_combo.bind("<<ComboboxSelected>>", self.update_venues)

        # 搶場時間
        ttk.Label(self.main_frame, text="搶場時間").pack(anchor="w", pady=5)
        hour_options = [f"{i:02d}" for i in range(24)]
        minute_options = [f"{i:02d}" for i in range(60)]
        second_options = [f"{i:02d}" for i in range(60)]
        self.hour_var = tk.StringVar(value="00")
        self.minute_var = tk.StringVar(value="00")
        self.second_var = tk.StringVar(value="00")
        time_frame = ttk.Frame(self.main_frame)
        time_frame.pack(pady=5)
        ttk.Combobox(time_frame, textvariable=self.hour_var, values=hour_options, width=3, state="readonly").pack(side="left")
        ttk.Label(time_frame, text="點").pack(side="left")
        ttk.Combobox(time_frame, textvariable=self.minute_var, values=minute_options, width=3, state="readonly").pack(side="left")
        ttk.Label(time_frame, text="分").pack(side="left")
        ttk.Combobox(time_frame, textvariable=self.second_var, values=second_options, width=3, state="readonly").pack(side="left")
        ttk.Label(time_frame, text="秒").pack(side="left")

        # 場次
        ttk.Label(self.main_frame, text="場次").pack(anchor="w", pady=5)
        self.session_var = tk.StringVar(value="17:00-19:00")
        self.session_combo = ttk.Combobox(self.main_frame, textvariable=self.session_var, state="readonly",
                                        values=["06:00-08:00",
                                                "07:00-09:00",
                                                "08:00-10:00",            
                                                "09:00-11:00",
                                                "10:00-12:00",
                                                "11:00-13:00",
                                                "12:00-14:00",
                                                "13:00-15:00",
                                                "14:00-16:00",
                                                "15:00-17:00",
                                                "16:00-18:00",
                                                "17:00-19:00",
                                                "18:00-20:00",
                                                "19:00-21:00",
                                                "20:00-22:00",
                                                "21:00-23:00",
                                                "22:00-23:00"])
        self.session_combo.pack(fill="x", pady=5)

        # 借用原因
        ttk.Label(self.main_frame, text="借用原因").pack(anchor="w", pady=5)
        self.reason_var = tk.StringVar(value="我要練球")
        self.reason_entry = ttk.Entry(self.main_frame, textvariable=self.reason_var)
        self.reason_entry.pack(fill="x", pady=5)

        # 是否正式搶票
        self.check_var = tk.BooleanVar(value=True)
        self.check_box = ttk.Checkbutton(
            self.main_frame, 
            text="✔ 正式搶場", 
            variable=self.check_var, 
            command=self.update_check_text,
            style="Frameless.TCheckbutton"
        )
        self.check_box.pack(anchor="w", pady=10)

        # 提交按鈕
        self.submit_button = ttk.Button(self.main_frame, text="開始搶場", command=self.submit)
        self.submit_button.pack(pady=20)
        
        # 增加說明文字
        ttk.Label(
            self.main_frame, 
            text="p.s.這裡默認搶兩周後前一天的場\n    練習則是兩周前兩天且不經時間驗證",
            font=("標楷體", 14),
            foreground="#000000"
        ).pack(anchor="w", pady=5)

    def validate_time(self, time_str):
        """驗證時間格式"""
        try:
            time.strptime(time_str, "%H:%M:%S")
            return True
        except ValueError:
            return False

    def submit(self):
        """提交表單並執行搶場"""
        session_map = {
            "06:00-08:00": 3,
            "07:00-09:00": 4,
            "08:00-10:00": 5,
            "09:00-11:00": 6,
            "10:00-12:00": 7,
            "11:00-13:00": 8,
            "12:00-14:00": 9,
            "13:00-15:00": 10,
            "14:00-16:00": 11,
            "15:00-17:00": 12,
            "16:00-18:00": 13,
            "17:00-19:00": 14,
            "18:00-20:00": 15,
            "19:00-21:00": 16,
            "20:00-22:00": 17,
            "21:00-23:00": 18,
            "22:00-23:00": 19
        }

        time_str = f"{self.hour_var.get()}:{self.minute_var.get()}:{self.second_var.get()}"
        
        data = {
            "email": self.email_var.get(),
            "password": self.password_var.get(),
            "sport_type": self.sport_var.get(),
            "venue": self.venue_var.get(),
            "time": time_str,
            "part": session_map.get(self.session_var.get(), 14),
            "reason": self.reason_var.get(),
            "check": self.check_var.get(),
            "stop": 20
        }

        if not all([data["email"], data["password"], data["sport_type"], data["venue"], data["reason"]]):
            messagebox.showerror("錯誤", "請填寫所有欄位")
            return
        if not self.validate_time(data["time"]):
            messagebox.showerror("錯誤", "時間格式錯誤，請使用 hh:mm:ss")
            return
        
        self.root.destroy()
        self.run_selenium(data)

    def safe_click(self, driver, locator, retries=2, delay=0.2):
        """安全點擊元素，失敗會重試"""
        for i in range(retries):
            try:
                elem = WebDriverWait(driver, 10).until(EC.element_to_be_clickable(locator))
                driver.execute_script("arguments[0].scrollIntoView(true);", elem)
                time.sleep(delay)
                driver.execute_script("arguments[0].click();", elem)
                return True
            except Exception as e:
                print(f"點擊失敗，重試 {i+1}/{retries}: {e}")
                time.sleep(0.5)
        return False

    def solve_captcha_until_success(self, driver, max_retries=10):
        """OCR 辨識驗證碼並自動嘗試，直到確認按鈕消失"""
        ocr = ddddocr.DdddOcr(beta=True)
        for attempt in range(max_retries):
            try:
                captcha_input = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "txtCaptchaValue"))
                )
                captcha_img = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "imgCaptcha"))
                )

                captcha_img.screenshot("captcha.png")
                with open("captcha.png", "rb") as f:
                    image = f.read()
                captcha_text = ocr.classification(image).strip()
                os.remove("captcha.png")

                captcha_input.clear()
                captcha_input.send_keys(captcha_text)
                print(f"✅ 嘗試驗證碼 ({attempt+1}/{max_retries}): {captcha_text}")

                confirm_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "/html/body/div/div/div[5]/button"))
                )
                driver.execute_script("arguments[0].click();", confirm_btn)
                time.sleep(0.2)

                try:
                    WebDriverWait(driver, 0.5).until_not(
                        EC.presence_of_element_located((By.XPATH, "/html/body/div/div/div[5]/button"))
                    )
                    print("🎉 驗證成功！")
                    return True, attempt + 1
                except:
                    print("❌ 驗證失敗，嘗試刷新驗證碼...")
                    refresh_btn = driver.find_element(By.XPATH, "//*[@id='txtCaptchaValue']/following::button[1]")
                    driver.execute_script("arguments[0].click();", refresh_btn)
                    time.sleep(0.3)

            except Exception as e:
                print(f"⚠️ 驗證流程錯誤: {e}")
                time.sleep(0.5)

        print("🚨 超過最大嘗試次數，仍未通過驗證")
        return False, max_retries
    
    def toggle_password(self):
        """切換密碼顯示/隱藏"""
        if self.show_pw:
            self.password_entry.config(show="*")
            self.eye_btn.config(text="👁")
        else:
            self.password_entry.config(show="")
            self.eye_btn.config(text="👁")
        self.show_pw = not self.show_pw
        
    def update_check_text(self):
        """根據勾選狀態切換顯示的文字"""
        if self.check_var.get():
            self.check_box.config(text="✔ 正式搶場")
        else:
            self.check_box.config(text="✘ 練習模式")

    def run_selenium(self, data):
        """執行 Selenium 搶場流程"""
        try:
            driver = webdriver.Chrome()
            driver.get("https://sys.ndhu.edu.tw/gc/sportcenter/SportsFields/Login.aspx")

            # 登入
            driver.find_element(By.ID, "MainContent_TxtUSERNO").send_keys(data["email"])
            driver.find_element(By.ID, "MainContent_TxtPWD").send_keys(data["password"])
            driver.find_element(By.ID, "MainContent_Button1").click()

            # 等待登入完成
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "MainContent_Button2")))

            # 新增申請
            self.safe_click(driver, (By.ID, "MainContent_Button2"))
            print("✅ 進入新增申請")
            time.sleep(0.3)

            # 選擇運動類型
            try:
                sport_dropdown = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "MainContent_drpkind"))
                )
                Select(sport_dropdown).select_by_visible_text(data["sport_type"])
            except Exception as e:
                print(f"❌ 運動類型選擇失敗: {e}")
                driver.quit()
                messagebox.showerror("錯誤", "運動類型選擇失敗")
                return
            time.sleep(0.5)

            # 選擇場地
            try:
                venue_dropdown = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "MainContent_DropDownList1"))
                )
                Select(venue_dropdown).select_by_visible_text(data["venue"])
            except Exception as e:
                print(f"❌ 場地選擇失敗: {e}")
                driver.quit()
                messagebox.showerror("錯誤", "場地選擇失敗")
                return
            time.sleep(0.5)

            # 選日期 (今日 → 後一周 → 後一周 → 前一天)
            for btn_id in ["MainContent_BtnToday2", "MainContent_BtnNextW2", "MainContent_BtnNextW2", "MainContent_BtnPreD2"]:
                self.safe_click(driver, (By.ID, btn_id))
                time.sleep(0.5)

            if not data["check"]:
                driver.find_element(By.ID, "MainContent_BtnPreD2").click()
                time.sleep(0.5)

            if data["check"]:
                # 等待指定時間
                while True:
                    try:
                        time_element = driver.find_element(By.ID, "currTime")
                        current_time = time_element.text.strip()
                        print(current_time)
                        if current_time[-8:] == data["time"]:
                            break
                        time.sleep(0.1)
                    except:
                        print("⚠️ 無法獲取時間，重試中...")
                        time.sleep(0.1)

            # 點擊查詢，開始計時
            start_time = time.time()
            self.safe_click(driver, (By.ID, "MainContent_Button1"))
            print("✅ 已點擊查詢")

            # 點擊申請
            apply_locator = (By.XPATH, f"//tr[{data['part']}]//button[contains(text(), '申請')]")
            if not self.safe_click(driver, apply_locator):
                print("❌ 無法點擊申請按鈕")
                driver.quit()
                messagebox.showerror("錯誤", "無法點擊申請按鈕")
                return

            # 處理驗證碼
            success, attempts = self.solve_captcha_until_success(driver)
            if not success:
                driver.quit()
                messagebox.showerror("錯誤", "驗證碼處理失敗")
                return

            # 驗證成功後，根據次數調整延遲
            if attempts == 1:
                extra_wait = 2
            elif attempts == 2:
                extra_wait = 1
            elif attempts == 3:
                extra_wait = 0.5
            else:
                extra_wait = 0
            print(f"⏳ 驗證成功，用了 {attempts} 次，額外等待 {extra_wait} 秒再送出")

            # 等待確認按鈕消失
            try:
                WebDriverWait(driver, 300).until_not(
                    EC.presence_of_element_located((By.XPATH, "/html/body/div/div/div[5]/button"))
                )
                print("✅ 偵測到確認按鈕消失，開始填寫借用原因")

                time.sleep(0.1)

                # 填入借用原因
                reason_input = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "MainContent_ReasonTextBox1"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", reason_input)
                time.sleep(0.2)
                reason_input.clear()
                reason_input.send_keys(data["reason"])
                print("✅ 已自動輸入借用原因")

            except Exception as e:
                print(f"⚠️ 借用原因輸入失敗: {e}")
                driver.quit()
                messagebox.showerror("錯誤", f"借用原因輸入失敗: {e}")
                return

            # 最後確認，結束計時
            end_time = time.time()
            duration = end_time - start_time

            # 格式化時間顯示為秒，保留兩位小數
            duration_str = f"{duration:.2f} 秒"

            # 準備成功訊息
            success_message = (
                f"成功搶到場！\n"
                f"運動類型: {data['sport_type']}\n"
                f"場地: {data['venue']}\n"
                f"場次: {self.session_var.get()}\n"
                f"搶場耗時: {duration_str}\n"
                f"驗證碼嘗試: {attempts} 次\n"
                f"額外等待: {extra_wait} 秒"
            )

            if data["check"]:
                time.sleep(extra_wait)
                self.safe_click(driver, (By.ID, "MainContent_Button4"))
                print("✅ 已送出申請")
                messagebox.showinfo("成功", success_message)
            else:
                print("✅ 練習模式完成")
                messagebox.showinfo("練習成功", success_message)

            time.sleep(data["stop"])
            print("程式結束")

        except Exception as e:
            print(f"⚠️ 發生錯誤: {e}")
            messagebox.showerror("錯誤", f"發生錯誤: {e}")

        finally:
            if 'driver' in locals():
                driver.quit()
            print("✅ 瀏覽器已關閉")

    def update_venues(self, event=None):
        """更新場地選項"""
        sport = self.sport_var.get()
        venues = self.venues_dict.get(sport, [])
        if venues:
            self.venue_combo["values"] = venues
            self.venue_var.set(venues[0])

if __name__ == "__main__":
    root = tk.Tk()
    app = VenueBookingGUI(root)
    root.mainloop()
