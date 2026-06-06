import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports
import json
import csv
import os
import threading
from datetime import datetime
import time
import requests  # 需安裝: pip3 install requests

# --- 設定 ---
DESKTOP_PATH = os.path.expanduser("~/Desktop")
CSV_FILE = os.path.join(DESKTOP_PATH, "algae_monitor_data.csv")
BAUD_RATE = 9600
BUFFER_SIZE = 20  # 每累積 20 筆數據寫入一次 SD 卡
FLUSH_INTERVAL = 60  # 即使數據不夠，每 60 秒也強制寫入一次
# 上次成功上傳到 Sheet 的時間戳(用來做斷點補傳)
LAST_UPLOADED_FILE = os.path.join(DESKTOP_PATH, ".last_uploaded_ts")

# Google Apps Script Web app URL — 從 cloud_url.txt 讀(該檔在 .gitignore,git 不追蹤)
# 在每台機器(Windows 開發機 / Pi)各自建立 cloud_url.txt,內容就一行完整 URL
_CLOUD_URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_url.txt")
try:
    with open(_CLOUD_URL_FILE) as f:
        CLOUD_URL = f.read().strip()
except FileNotFoundError:
    CLOUD_URL = ""  # 沒設 → 雲端同步功能會自動跳過,本地 CSV 仍正常寫入

class AlgaeMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("藻類比賽 - 數據監測儀表板 (SD卡保護版)")
        self.root.geometry("800x600")
        
        # 感測器狀態、標籤與單位
        self.sensor_keys = ["t", "ph", "tds", "tdse", "ec", "turb", "lux", "c2b", "c2c"]
        self.labels = {
            "t": "溫度", "ph": "酸鹼", "tds": "溶解", "tdse": "TDS(EC)", "ec": "導電",
            "turb": "濁度", "lux": "光照", "c2b": "CO2_B", "c2c": "CO2_C"
        }
        self.units = {
            "t": "°C", "ph": "pH", "tds": "ppm", "tdse": "ppm", "ec": "mS/cm",
            "turb": "NTU", "lux": "lx", "c2b": "ppm", "c2c": "ppm"
        }
        self.status = {key: tk.BooleanVar(value=True) for key in self.sensor_keys}
        self.data_vars = {key: tk.StringVar(value="---") for key in self.sensor_keys}
        self.cloud_sync = tk.BooleanVar(value=False) # 預設關閉雲端同步
        
        # 緩衝區與鎖
        self.data_buffer = []
        self.buffer_lock = threading.Lock()
        self.last_flush_time = time.time()

        # 雲端斷點補傳機制
        self._backfill_running = False
        self._ts_lock = threading.Lock()
        # 監聽 cloud_sync checkbox 變化:OFF→ON 觸發補傳
        self.cloud_sync.trace_add("write", self._on_cloud_sync_changed)

        self.setup_ui()
        self.start_serial_threads()
        self.start_timer_thread()
        
        # 初始化 CSV
        if not os.path.exists(CSV_FILE):
            with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
                header = ["時間", "裝置"]
                for key in self.sensor_keys:
                    header.append(f"{self.labels[key]}({self.units[key]})")
                csv.writer(f).writerow(header)

    def setup_ui(self):
        header = tk.Label(self.root, text="藻類監測系統", font=("Arial", 24, "bold"))
        header.pack(pady=10)

        main_frame = tk.Frame(self.root)
        main_frame.pack(fill="both", expand=True, padx=20)

        display_frame = tk.LabelFrame(main_frame, text="即時數據", font=("Arial", 14))
        display_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        for key in self.sensor_keys:
            frame = tk.Frame(display_frame)
            frame.pack(fill="x", pady=5, padx=10)
            tk.Label(frame, text=f"{self.labels[key]} ({self.units[key]})", font=("Arial", 12), width=15, anchor="w").pack(side="left")
            tk.Label(frame, textvariable=self.data_vars[key], font=("Arial", 14, "bold"), fg="blue").pack(side="left")

        control_frame = tk.LabelFrame(main_frame, text="監控開關 (勾選以記錄)", font=("Arial", 14))
        control_frame.pack(side="right", fill="y", padx=10, pady=10)

        for key in self.sensor_keys:
            tk.Checkbutton(control_frame, text=self.labels[key], variable=self.status[key], font=("Arial", 11)).pack(anchor="w", pady=2, padx=10)

        tk.Label(control_frame, text="--- 雲端設定 ---", font=("Arial", 12, "bold")).pack(pady=10)
        tk.Checkbutton(control_frame, text="同步至 Google Sheets", variable=self.cloud_sync, font=("Arial", 11), fg="green").pack(anchor="w", padx=10)

        self.status_bar = tk.Label(self.root, text="正在搜尋裝置...", bd=1, relief="sunken", anchor="w")
        self.status_bar.pack(side="bottom", fill="x")

    def save_to_buffer(self, timestamp, device_id, values_dict):
        """將整行數據存入緩衝區。checkbox 與斷線都已在 handle_serial 統一轉為 -1"""
        with self.buffer_lock:
            row = [timestamp, device_id]
            for key in self.sensor_keys:
                row.append(values_dict.get(key, -1))
            self.data_buffer.append(row)
            if len(self.data_buffer) >= BUFFER_SIZE:
                self.flush_buffer()

    def flush_buffer(self):
        """強制將緩衝區數據寫入 SD 卡"""
        if not self.data_buffer:
            return
        try:
            with open(CSV_FILE, mode='a', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerows(self.data_buffer)
            self.data_buffer = []
            self.last_flush_time = time.time()
            self.status_bar.config(text=f"數據已同步至 SD 卡: {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            self.status_bar.config(text=f"寫入失敗: {e}")

    def start_timer_thread(self):
        """定時器：確保即使數據不夠，也會定期存檔"""
        def timer_loop():
            while True:
                time.sleep(10) # 每 10 秒檢查一次
                if time.time() - self.last_flush_time >= FLUSH_INTERVAL:
                    with self.buffer_lock:
                        self.flush_buffer()
        threading.Thread(target=timer_loop, daemon=True).start()

    def sync_to_cloud(self, device_id, values_dict, ts):
        """背景傳送完整數據包到 Google Sheets。
        ts: 量測時間戳字串(會送進 Apps Script 讓 Sheet 用真實時間,不是上傳時間)
        """
        if not self.cloud_sync.get() or not CLOUD_URL:
            return

        def task():
            try:
                payload = {
                    "ts": ts,
                    "device_id": device_id,
                    "temp": values_dict.get('t'),
                    "ph": values_dict.get('ph'),
                    "tds": values_dict.get('tds'),
                    "tdse": values_dict.get('tdse'),
                    "ec": values_dict.get('ec'),
                    "turb": values_dict.get('turb'),
                    "lux": values_dict.get('lux'),
                    "c2b": values_dict.get('c2b'),
                    "c2c": values_dict.get('c2c')
                }
                response = requests.post(CLOUD_URL, json=payload, timeout=5)
                if response.status_code != 200:
                    msg = f"雲端同步失敗: HTTP {response.status_code}"
                    self.status_bar.config(text=msg)
                    print(msg)
                else:
                    self._save_last_uploaded_ts(ts)
                    self.status_bar.config(text=f"雲端同步成功: {datetime.now().strftime('%H:%M:%S')}")
            except Exception as e:
                msg = f"雲端錯誤: {str(e)[:60]}"
                self.status_bar.config(text=msg)
                print(f"網路連線錯誤: {e}")

        threading.Thread(target=task, daemon=True).start()

    # ---- 斷點補傳機制 ----

    def _read_last_uploaded_ts(self):
        """讀上次成功上傳的時間戳;沒檔就回空字串"""
        try:
            with open(LAST_UPLOADED_FILE) as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
        except Exception as e:
            print(f"讀 last_uploaded_ts 失敗: {e}")
            return ""

    def _save_last_uploaded_ts(self, ts):
        """寫 last_uploaded_ts。只有新值比舊值大才寫(避免回頭),thread-safe"""
        if not ts:
            return
        with self._ts_lock:
            current = self._read_last_uploaded_ts()
            if not current or ts > current:
                try:
                    with open(LAST_UPLOADED_FILE, "w") as f:
                        f.write(ts)
                except Exception as e:
                    print(f"寫 last_uploaded_ts 失敗: {e}")

    def _on_cloud_sync_changed(self, *args):
        """checkbox 變動 callback。OFF→ON 啟動補傳 thread。"""
        if self.cloud_sync.get() and not self._backfill_running:
            self._backfill_running = True
            threading.Thread(target=self._backfill_to_cloud, daemon=True).start()

    def _backfill_to_cloud(self):
        """掃 CSV,把 last_uploaded_ts 之後的列補傳到 Sheet,然後即時模式接手"""
        try:
            if not CLOUD_URL:
                self.status_bar.config(text="補傳跳過:沒設 cloud_url.txt")
                return
            last_ts = self._read_last_uploaded_ts()
            # 掃 CSV
            rows_to_send = []
            try:
                with open(CSV_FILE, encoding='utf-8-sig') as f:
                    reader = csv.reader(f)
                    next(reader, None)  # skip header
                    for row in reader:
                        if not row or len(row) < 11:
                            continue
                        row_ts = row[0]
                        if not last_ts or row_ts > last_ts:
                            rows_to_send.append(row)
            except FileNotFoundError:
                self.status_bar.config(text="補傳跳過:CSV 不存在")
                return

            if not rows_to_send:
                self.status_bar.config(text="無待補傳資料,即時模式")
                return

            total = len(rows_to_send)
            self.status_bar.config(text=f"開始補傳 {total} 筆…")
            sent = 0
            for row in rows_to_send:
                # 中途取消 → 停下,下次再勾從上次成功點繼續
                if not self.cloud_sync.get():
                    self.status_bar.config(text=f"補傳中斷:{sent}/{total}")
                    return
                payload = self._csv_row_to_payload(row)
                if not payload:
                    continue
                try:
                    resp = requests.post(CLOUD_URL, json=payload, timeout=10)
                    if resp.status_code == 200:
                        sent += 1
                        self._save_last_uploaded_ts(row[0])
                        if sent % 5 == 0 or sent == total:
                            self.status_bar.config(text=f"補傳中:{sent}/{total}")
                    else:
                        self.status_bar.config(text=f"補傳暫停:HTTP {resp.status_code} ({sent}/{total})")
                        return
                except Exception as e:
                    self.status_bar.config(text=f"補傳暫停:{str(e)[:40]} ({sent}/{total})")
                    return
            self.status_bar.config(text=f"補傳完成 {sent}/{total},即時模式")
        finally:
            self._backfill_running = False

    def _csv_row_to_payload(self, row):
        """CSV 列 → Apps Script payload(欄位順序對應 setup_ui 的 header)"""
        # CSV: [0]時間 [1]裝置 [2]t [3]ph [4]tds [5]tdse [6]ec [7]turb [8]lux [9]c2b [10]c2c
        try:
            return {
                "ts": row[0],
                "device_id": row[1],
                "temp": self._cast_num(row[2]),
                "ph": self._cast_num(row[3]),
                "tds": self._cast_num(row[4]),
                "tdse": self._cast_num(row[5]),
                "ec": self._cast_num(row[6]),
                "turb": self._cast_num(row[7]),
                "lux": self._cast_num(row[8]),
                "c2b": self._cast_num(row[9]),
                "c2c": self._cast_num(row[10]),
            }
        except IndexError:
            return None

    def _cast_num(self, v):
        """CSV 讀進來是字串,盡量轉成數值"""
        if v is None or v == '':
            return None
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (ValueError, TypeError):
            return v

    def handle_serial(self, port):
        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=1)
            self.status_bar.config(text=f"已連接: {port}")
            while True:
                if ser.in_waiting > 0:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if not line: continue
                    
                    try:
                        data = json.loads(line)
                        v = data.get("v", {})
                        device_id = data.get("id", "Unknown")
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        
                        # 三種「無資料」代碼:-1 真斷線、-2 使用者關閉、-3 韌體沒送
                        # 下游 monitor_email 只對 -1 發警告,-2/-3 不發
                        final_data = {}
                        for key in self.sensor_keys:
                            val = v.get(key)
                            if not self.status[key].get():
                                final_data[key] = -2
                                self.data_vars[key].set("已關閉")
                            elif val is None:
                                final_data[key] = -3
                                self.data_vars[key].set("⚠ 未送")
                            elif val == -1:
                                final_data[key] = -1
                                self.data_vars[key].set("⚠ 未接")
                            else:
                                final_data[key] = val
                                self.data_vars[key].set(f"{val}")

                        self.save_to_buffer(ts, device_id, final_data)
                        self.sync_to_cloud(device_id, final_data, ts)
                        self.status_bar.config(text=f"成功接收來自 {device_id} 的數據")
                        
                    except Exception as e:
                        print(f"解析錯誤: {e}")
        except Exception as e:
            self.status_bar.config(text=f"錯誤: {port} 已斷開")

    def start_serial_threads(self):
        ports = [p.device for p in serial.tools.list_ports.comports() if 'USB' in p.description or 'ACM' in p.device]
        if not ports:
            messagebox.showwarning("警告", "找不到任何 USB 裝置！請檢查連線。")
            return
        for port in ports:
            threading.Thread(target=self.handle_serial, args=(port,), daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = AlgaeMonitorApp(root)
    root.mainloop()
