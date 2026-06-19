"""
AquaMind 統合監測應用程式 (aquamind_app.py)

從 rpi_gui_monitor.py 演化而來,設計為可獨立交付的水質監測整合應用:
- 6 個感測器即時顯示 + 個別開關(溫度、酸鹼、TDS、TDS(EC)、導電、光照)
- 移除濁度、CO2_B、CO2_C(原專案用不到)
- 雲端同步至 Google Sheets,checkbox OFF→ON 觸發 CSV 斷點補傳
- 「設定」選單:GUI 直接輸入 Apps Script URL、Gemini API key、SMTP,寫入 chmod 600
- USB 斷線自動重連、Pi/Jetson 都能跑
- 按右上角 X → wrapper 不重啟(配合 setup/install_resilience.sh)

私密資料儲存位置(全部都在本機,絕不會上傳):
- Apps Script URL → ~/Desktop/cloud_url.txt
- Gemini API key + SMTP → ~/aquamind_config.env (chmod 600)
- 歷史 CSV → ~/Desktop/algae_monitor_data.csv

執行:
    python3 aquamind_app.py
(autostart wrapper 會自動以正確的 DISPLAY/Python 版本啟動)
"""
import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports
import json
import csv
import os
import sys
import threading
from datetime import datetime
import time
import requests

# --- 路徑常數 ---
DESKTOP_PATH = os.path.expanduser("~/Desktop")
CSV_FILE = os.path.join(DESKTOP_PATH, "algae_monitor_data.csv")
LAST_UPLOADED_FILE = os.path.join(DESKTOP_PATH, ".last_uploaded_ts")
CLOUD_URL_FILE = os.path.join(DESKTOP_PATH, "cloud_url.txt")
CONFIG_ENV_FILE = os.path.expanduser("~/aquamind_config.env")

# --- 自動啟動 + crash 自動重啟 相關 ---
WRAPPER_FILE = os.path.join(DESKTOP_PATH, "run_gui.sh")
AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "aquamind-gui.desktop")

WRAPPER_TEMPLATE = """#!/bin/bash
# AquaMind 重啟迴圈 — 由 aquamind_app.py 設定選單寫入
#   - crash / USB 斷線 → 30 秒自動重起
#   - 使用者按右上角 X → 寫 ~/.gui_user_closed 旗標 → wrapper exit 不再重起
cd "$HOME/Desktop"
LOG="$HOME/gui.log"
MARKER="$HOME/.gui_user_closed"
APP_PATH="__APP_PATH__"
PYTHON_BIN="__PYTHON_BIN__"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

rm -f "$MARKER"
while true; do
  rm -f "$MARKER"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 啟動 $(basename $APP_PATH) (DISPLAY=$DISPLAY)" >> "$LOG"
  $PYTHON_BIN "$APP_PATH" >> "$LOG" 2>&1
  RC=$?
  if [ -f "$MARKER" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 使用者按 X 關閉,wrapper 一併退出" >> "$LOG"
    rm -f "$MARKER"
    exit 0
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 程式異常結束 (exit=$RC),30 秒後重啟" >> "$LOG"
  sleep 30
done
"""

AUTOSTART_TEMPLATE = """[Desktop Entry]
Type=Application
Name=AquaMind GUI Monitor
Comment=自動啟動 AquaMind,crash 後 30 秒重啟
Exec=__WRAPPER_PATH__
X-GNOME-Autostart-enabled=true
NoDisplay=true
Terminal=false
"""


def is_autostart_enabled():
    """看 autostart + wrapper 兩個檔在不在"""
    return os.path.exists(AUTOSTART_FILE) and os.path.exists(WRAPPER_FILE)


def enable_autostart():
    """寫 wrapper script + autostart .desktop。使用當前 Python 跟當前主程式路徑。"""
    python_bin = sys.executable
    app_path = os.path.abspath(sys.argv[0]) if sys.argv else os.path.abspath(__file__)

    wrapper_content = WRAPPER_TEMPLATE.replace("__APP_PATH__", app_path).replace("__PYTHON_BIN__", python_bin)
    with open(WRAPPER_FILE, "w") as f:
        f.write(wrapper_content)
    os.chmod(WRAPPER_FILE, 0o755)

    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    autostart_content = AUTOSTART_TEMPLATE.replace("__WRAPPER_PATH__", WRAPPER_FILE)
    with open(AUTOSTART_FILE, "w") as f:
        f.write(autostart_content)


def disable_autostart():
    """移除兩個檔(allow not exist)"""
    for path in (WRAPPER_FILE, AUTOSTART_FILE):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

# --- 序列 / 緩衝 ---
BAUD_RATE = 9600
BUFFER_SIZE = 20         # 每累積 20 筆寫入一次 SD 卡
FLUSH_INTERVAL = 60      # 即使數據不夠,每 60 秒強制寫入一次


# ==================== 設定檔讀寫 ====================

def load_cloud_url():
    """讀 cloud_url.txt(沒有就回空字串,GUI 自動跳過雲端 sync)"""
    try:
        with open(CLOUD_URL_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def save_cloud_url(url):
    """寫 cloud_url.txt"""
    with open(CLOUD_URL_FILE, "w") as f:
        f.write(url.strip())


def parse_env_file(path):
    """解析簡易 .env(export VAR=value),回傳 dict"""
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('export '):
                    line = line[7:]
                if '=' in line:
                    k, v = line.split('=', 1)
                    result[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return result


def save_env_file(path, env_dict):
    """寫 .env,只寫有值的行;權限 chmod 600(只有自己能讀)"""
    lines = [
        "# AquaMind 環境變數設定檔 — 由 aquamind_app.py 設定視窗寫入",
        "# 不要把此檔貼上 git / 截圖外傳",
        "",
    ]
    for k, v in env_dict.items():
        if v:
            lines.append(f"export {k}={v}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ==================== 設定對話框 ====================

class SettingsDialog:
    """設定對話框 — GUI 輸入所有私密資料"""

    def __init__(self, parent, on_save=None):
        self.on_save = on_save
        self.top = tk.Toplevel(parent)
        self.top.title("AquaMind 設定")
        self.top.geometry("640x600")
        self.top.transient(parent)
        self.top.grab_set()

        # 載入現有值
        self.cloud_url = load_cloud_url()
        env = parse_env_file(CONFIG_ENV_FILE)
        self.gemini_key = env.get("GEMINI_API_KEY", "")
        self.smtp_user = env.get("SMTP_USER", "")
        self.smtp_pass = env.get("SMTP_PASSWORD", "")
        self.smtp_to = env.get("SMTP_TO", "")

        self._build_ui()

    def _build_ui(self):
        main = tk.Frame(self.top, padx=18, pady=15)
        main.pack(fill="both", expand=True)

        # 標題 + 提示
        tk.Label(main, text="AquaMind 設定", font=("Arial", 16, "bold")).pack(anchor="w")
        tk.Label(main, text="所有資料只儲存在本機 ~/Desktop/ 跟 ~/aquamind_config.env,絕不會上傳任何地方",
                 fg="gray", font=("Arial", 9)).pack(anchor="w", pady=(0, 12))

        # === 區塊 1:Google Sheets ===
        cloud_frame = tk.LabelFrame(main, text=" 1. Google Sheets 雲端同步 ",
                                     font=("Arial", 11, "bold"), padx=10, pady=8)
        cloud_frame.pack(fill="x", pady=4)
        tk.Label(cloud_frame, text="Apps Script Web App URL:", anchor="w").pack(fill="x")
        tk.Label(cloud_frame,
                 text="從 Google Sheets → 擴充功能 → Apps Script → 部署 → 網頁應用程式 → URL",
                 fg="gray", font=("Arial", 9)).pack(fill="x")
        self.cloud_url_entry = tk.Entry(cloud_frame, width=80)
        self.cloud_url_entry.insert(0, self.cloud_url)
        self.cloud_url_entry.pack(fill="x", pady=3)

        # === 區塊 2:Gemini AI ===
        ai_frame = tk.LabelFrame(main, text=" 2. Gemini AI(每日分析報告,可選) ",
                                  font=("Arial", 11, "bold"), padx=10, pady=8)
        ai_frame.pack(fill="x", pady=4)
        tk.Label(ai_frame, text="Gemini API Key:", anchor="w").pack(fill="x")
        tk.Label(ai_frame, text="從 https://aistudio.google.com/apikey 免費申請",
                 fg="gray", font=("Arial", 9)).pack(fill="x")
        gemini_row = tk.Frame(ai_frame)
        gemini_row.pack(fill="x", pady=3)
        self.gemini_entry = tk.Entry(gemini_row, width=70, show='*')
        self.gemini_entry.insert(0, self.gemini_key)
        self.gemini_entry.pack(side="left", fill="x", expand=True)
        self.show_gemini = tk.BooleanVar(value=False)
        tk.Checkbutton(gemini_row, text="顯示", variable=self.show_gemini,
                       command=self._toggle_gemini_visibility).pack(side="left", padx=5)

        # === 區塊 3:Email 通知 ===
        email_frame = tk.LabelFrame(main, text=" 3. Email 通知(程式停止時通報,可選) ",
                                     font=("Arial", 11, "bold"), padx=10, pady=8)
        email_frame.pack(fill="x", pady=4)
        tk.Label(email_frame, text="Gmail 寄件地址:", anchor="w").pack(fill="x")
        self.smtp_user_entry = tk.Entry(email_frame, width=80)
        self.smtp_user_entry.insert(0, self.smtp_user)
        self.smtp_user_entry.pack(fill="x", pady=2)
        tk.Label(email_frame, text="Gmail 應用程式密碼(不是登入密碼,從 Google 帳號 → 安全性 → 應用程式密碼):",
                 anchor="w").pack(fill="x")
        self.smtp_pass_entry = tk.Entry(email_frame, width=80, show='*')
        self.smtp_pass_entry.insert(0, self.smtp_pass)
        self.smtp_pass_entry.pack(fill="x", pady=2)
        tk.Label(email_frame, text="收件 Email(多人時用逗號分隔,例如 a@x.com, b@y.com):",
                 anchor="w").pack(fill="x")
        self.smtp_to_entry = tk.Entry(email_frame, width=80)
        self.smtp_to_entry.insert(0, self.smtp_to)
        self.smtp_to_entry.pack(fill="x", pady=2)

        # === 右下角:取消 + 儲存 ===
        tk.Label(main, text="⚠️ 改完請按「儲存」,直接關閉視窗不會自動保存",
                 fg="darkred", font=("Arial", 10)).pack(anchor="e", pady=(5, 0))

        btn_frame = tk.Frame(main)
        btn_frame.pack(fill="x", pady=10)
        # side="right" 從右邊往左疊,所以「儲存」最右、「取消」次右
        tk.Button(btn_frame, text="💾 儲存", command=self.save,
                  width=14, bg="#2e8b57", fg="white",
                  font=("Arial", 12, "bold")).pack(side="right", padx=5)
        tk.Button(btn_frame, text="取消", command=self.top.destroy,
                  width=10).pack(side="right", padx=5)
        # 訊息顯示在左邊
        self.result_var = tk.StringVar(value="")
        tk.Label(btn_frame, textvariable=self.result_var, fg="green",
                 font=("Arial", 10)).pack(side="left", padx=5)

    def _toggle_gemini_visibility(self):
        self.gemini_entry.config(show='' if self.show_gemini.get() else '*')

    def save(self):
        try:
            url = self.cloud_url_entry.get().strip()
            save_cloud_url(url)
            env = {
                "GEMINI_API_KEY": self.gemini_entry.get().strip(),
                "SMTP_USER": self.smtp_user_entry.get().strip(),
                "SMTP_PASSWORD": self.smtp_pass_entry.get().strip(),
                "SMTP_TO": self.smtp_to_entry.get().strip(),
            }
            save_env_file(CONFIG_ENV_FILE, env)
            self.result_var.set("✓ 已儲存,2 秒後自動關閉")
            if self.on_save:
                self.on_save()
            self.top.after(2000, self.top.destroy)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))


# ==================== 主程式 ====================

class AquaMindApp:
    """主應用程式 — 即時監測 + 雲端同步 + GUI 設定"""

    def __init__(self, root):
        self.root = root
        self.root.title("AquaMind 水質監測")
        self.root.geometry("900x650")
        # 攔截右上角 X → 寫旗標檔讓 wrapper 知道別重啟
        self.root.protocol("WM_DELETE_WINDOW", self._on_user_close)

        # 感測器設定(已移除 turb、c2b、c2c)
        self.sensor_keys = ["t", "ph", "tds", "tdse", "ec", "lux"]
        self.labels = {
            "t": "溫度", "ph": "酸鹼", "tds": "溶解",
            "tdse": "TDS(EC)", "ec": "導電", "lux": "光照",
        }
        self.units = {
            "t": "°C", "ph": "pH", "tds": "ppm",
            "tdse": "ppm", "ec": "mS/cm", "lux": "lx",
        }

        self.status = {k: tk.BooleanVar(value=True) for k in self.sensor_keys}
        self.data_vars = {k: tk.StringVar(value="---") for k in self.sensor_keys}
        self.cloud_sync = tk.BooleanVar(value=False)

        # 雲端 URL(設定視窗修改後會 reload)
        self.cloud_url = load_cloud_url()

        # 寫入緩衝
        self.data_buffer = []
        self.buffer_lock = threading.Lock()
        self.last_flush_time = time.time()

        # 補傳機制
        self._backfill_running = False
        self._ts_lock = threading.Lock()
        self.cloud_sync.trace_add("write", self._on_cloud_sync_changed)

        self._build_ui()
        self._init_csv()
        self.start_serial_threads()
        self.start_timer_thread()

    # ---- UI ----

    def _build_ui(self):
        # 選單列
        menubar = tk.Menu(self.root)

        # 設定
        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="設定 (URL / API key / Email)…",
                                   command=self._open_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="關於 AquaMind", command=self._show_about)
        settings_menu.add_command(label="離開", command=self._on_user_close)
        menubar.add_cascade(label="設定", menu=settings_menu)

        # 啟動 — autostart + 自動重啟 切換
        startup_menu = tk.Menu(menubar, tearoff=0)
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        startup_menu.add_checkbutton(
            label="開機自動啟動 + crash 自動重啟",
            variable=self.autostart_var,
            command=self._toggle_autostart,
        )
        startup_menu.add_separator()
        startup_menu.add_command(label="說明 (自動啟動行為)", command=self._show_autostart_help)
        menubar.add_cascade(label="啟動", menu=startup_menu)

        self.root.config(menu=menubar)

        # 標題
        tk.Label(self.root, text="AquaMind 水質監測", font=("Arial", 22, "bold")).pack(pady=10)

        main_frame = tk.Frame(self.root)
        main_frame.pack(fill="both", expand=True, padx=20)

        # 左:即時數據
        display_frame = tk.LabelFrame(main_frame, text=" 即時數據 ", font=("Arial", 13))
        display_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        for key in self.sensor_keys:
            row = tk.Frame(display_frame)
            row.pack(fill="x", pady=6, padx=15)
            tk.Label(row, text=f"{self.labels[key]} ({self.units[key]})",
                     font=("Arial", 12), width=18, anchor="w").pack(side="left")
            tk.Label(row, textvariable=self.data_vars[key],
                     font=("Arial", 14, "bold"), fg="blue").pack(side="left")

        # 右:控制
        control_frame = tk.LabelFrame(main_frame, text=" 監控開關 ", font=("Arial", 13))
        control_frame.pack(side="right", fill="y", padx=10, pady=10)
        for key in self.sensor_keys:
            tk.Checkbutton(control_frame, text=self.labels[key],
                           variable=self.status[key], font=("Arial", 11)).pack(anchor="w", pady=2, padx=10)

        tk.Label(control_frame, text="--- 雲端 ---", font=("Arial", 11, "bold")).pack(pady=8)
        tk.Checkbutton(control_frame, text="同步至 Google Sheets",
                       variable=self.cloud_sync, font=("Arial", 11), fg="green").pack(anchor="w", padx=10)
        self.cloud_hint = tk.Label(control_frame, text="", fg="orange",
                                    font=("Arial", 9), wraplength=160, justify="left")
        self.cloud_hint.pack(padx=10, pady=3)
        self._update_cloud_hint()

        # 狀態列
        self.status_bar = tk.Label(self.root, text="啟動中...",
                                    bd=1, relief="sunken", anchor="w", padx=8)
        self.status_bar.pack(side="bottom", fill="x")

    def _update_cloud_hint(self):
        if self.cloud_url:
            self.cloud_hint.config(text=f"URL 已設定 ✓\n({self.cloud_url[:30]}...)", fg="green")
        else:
            self.cloud_hint.config(text="(尚未設定 URL,\n從「設定」選單填)", fg="orange")

    def _init_csv(self):
        if not os.path.exists(CSV_FILE):
            with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
                header = ["時間", "裝置"] + [f"{self.labels[k]}({self.units[k]})" for k in self.sensor_keys]
                csv.writer(f).writerow(header)

    def _open_settings(self):
        SettingsDialog(self.root, on_save=self._reload_config)

    def _reload_config(self):
        """設定視窗存檔後呼叫,重新讀取 cloud_url"""
        self.cloud_url = load_cloud_url()
        self._update_cloud_hint()
        self.status_bar.config(text=f"設定已更新: {datetime.now().strftime('%H:%M:%S')}")

    def _show_about(self):
        messagebox.showinfo("關於 AquaMind",
            "AquaMind 水質監測 + Google Sheets 雲端同步\n\n"
            "本機資料位置:\n"
            f"  CSV:      {CSV_FILE}\n"
            f"  Cloud URL:{CLOUD_URL_FILE}\n"
            f"  Env:      {CONFIG_ENV_FILE}\n\n"
            "所有私密資料只儲存在本機,絕不會上傳第三方。")

    def _toggle_autostart(self):
        """切換自動啟動 + crash 自動重啟。沒有破壞性 — 純檔案寫/刪。"""
        try:
            if self.autostart_var.get():
                enable_autostart()
                self.status_bar.config(
                    text=f"已啟用「開機自動啟動 + crash 自動重啟」"
                         f"(下次開機 + 桌面登入後生效)"
                )
                messagebox.showinfo(
                    "已啟用自動啟動",
                    "已寫入兩個檔:\n"
                    f"  • {WRAPPER_FILE}\n"
                    f"  • {AUTOSTART_FILE}\n\n"
                    "之後行為:\n"
                    "  • 開機 + 自動登入桌面 → 本程式自動跑\n"
                    "  • 程式 crash / USB 斷線 → 30 秒自動重起\n"
                    "  • 你按右上角 X → wrapper 一併退出,不會無限重啟\n\n"
                    "立刻生效需重登桌面或重開機,目前 process 不受影響。"
                )
            else:
                disable_autostart()
                self.status_bar.config(text="已停用自動啟動")
                messagebox.showinfo(
                    "已停用自動啟動",
                    "已刪除兩個檔:\n"
                    f"  • {WRAPPER_FILE}\n"
                    f"  • {AUTOSTART_FILE}\n\n"
                    "之後行為:\n"
                    "  • 開機後不會自動跑(要手動雙擊 aquamind_app.py 或 SSH 啟動)\n"
                    "  • 程式 crash 後也不會自動重啟"
                )
        except Exception as e:
            # 萬一檔案寫不進去,還原 checkbox 狀態
            self.autostart_var.set(is_autostart_enabled())
            messagebox.showerror("切換失敗", str(e))

    def _show_autostart_help(self):
        messagebox.showinfo(
            "自動啟動行為說明",
            "啟用後,Pi / Jetson 上會寫兩個檔:\n\n"
            f"  1. {WRAPPER_FILE}\n"
            "      bash 重啟迴圈 — crash 後 30 秒自動拉新的\n\n"
            f"  2. {AUTOSTART_FILE}\n"
            "      標準 XDG autostart — 桌面登入時自動跑 wrapper\n\n"
            "兩個檔的位置都在使用者家目錄,不會影響系統其他東西。\n"
            "隨時可以取消勾選還原成手動模式。"
        )

    def _on_user_close(self):
        marker = os.path.expanduser("~/.gui_user_closed")
        try:
            with open(marker, "w") as f:
                f.write("closed by user")
        except Exception:
            pass
        print("[user] 視窗被使用者主動關閉,wrapper 不會再重啟", flush=True)
        self.root.destroy()

    # ---- 緩衝 + CSV ----

    def save_to_buffer(self, timestamp, device_id, values_dict):
        with self.buffer_lock:
            row = [timestamp, device_id]
            for key in self.sensor_keys:
                row.append(values_dict.get(key, -1))
            self.data_buffer.append(row)
            if len(self.data_buffer) >= BUFFER_SIZE:
                self.flush_buffer()

    def flush_buffer(self):
        if not self.data_buffer:
            return
        try:
            with open(CSV_FILE, mode='a', newline='', encoding='utf-8-sig') as f:
                csv.writer(f).writerows(self.data_buffer)
            self.data_buffer = []
            self.last_flush_time = time.time()
            self.status_bar.config(text=f"數據已寫入本機 CSV: {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            self.status_bar.config(text=f"寫入失敗: {e}")

    def start_timer_thread(self):
        def timer_loop():
            while True:
                time.sleep(10)
                if time.time() - self.last_flush_time >= FLUSH_INTERVAL:
                    with self.buffer_lock:
                        self.flush_buffer()
        threading.Thread(target=timer_loop, daemon=True).start()

    # ---- 雲端 ----

    def sync_to_cloud(self, device_id, values_dict, ts):
        if not self.cloud_sync.get() or not self.cloud_url:
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
                    "lux": values_dict.get('lux'),
                }
                print(f"[LIVE  ] POST ts={payload['ts']!r} device={device_id}", flush=True)
                response = requests.post(self.cloud_url, json=payload, timeout=5)
                if response.status_code != 200:
                    self.status_bar.config(text=f"雲端同步失敗: HTTP {response.status_code}")
                else:
                    self._save_last_uploaded_ts(ts)
                    self.status_bar.config(text=f"雲端同步成功: {datetime.now().strftime('%H:%M:%S')}")
            except Exception as e:
                self.status_bar.config(text=f"雲端錯誤: {str(e)[:60]}")
                print(f"網路連線錯誤: {e}", flush=True)

        threading.Thread(target=task, daemon=True).start()

    # ---- 斷點補傳 ----

    def _read_last_uploaded_ts(self):
        try:
            with open(LAST_UPLOADED_FILE) as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def _save_last_uploaded_ts(self, ts):
        if not ts:
            return
        with self._ts_lock:
            current = self._read_last_uploaded_ts()
            if not current or ts > current:
                try:
                    with open(LAST_UPLOADED_FILE, "w") as f:
                        f.write(ts)
                except Exception:
                    pass

    def _on_cloud_sync_changed(self, *args):
        if self.cloud_sync.get() and not self._backfill_running:
            if not self.cloud_url:
                self.status_bar.config(text="尚未設定雲端 URL,請從「設定」選單填入")
                self.cloud_sync.set(False)
                return
            self._backfill_running = True
            threading.Thread(target=self._backfill_to_cloud, daemon=True).start()

    def _backfill_to_cloud(self):
        try:
            if not self.cloud_url:
                return
            last_ts = self._read_last_uploaded_ts()
            rows_to_send = []
            try:
                with open(CSV_FILE, encoding='utf-8-sig') as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if not row or len(row) < 2 + len(self.sensor_keys):
                            continue
                        if not last_ts or row[0] > last_ts:
                            rows_to_send.append(row)
            except FileNotFoundError:
                return

            if not rows_to_send:
                self.status_bar.config(text="無待補傳資料,即時模式")
                return

            total = len(rows_to_send)
            self.status_bar.config(text=f"開始補傳 {total} 筆…")
            sent = 0
            for row in rows_to_send:
                if not self.cloud_sync.get():
                    self.status_bar.config(text=f"補傳中斷:{sent}/{total}")
                    return
                payload = self._csv_row_to_payload(row)
                if not payload:
                    continue
                print(f"[BACK  ] POST ts={payload.get('ts')!r}", flush=True)
                try:
                    resp = requests.post(self.cloud_url, json=payload, timeout=10)
                    if resp.status_code == 200:
                        sent += 1
                        self._save_last_uploaded_ts(row[0])
                        if sent % 5 == 0 or sent == total:
                            self.status_bar.config(text=f"補傳中:{sent}/{total}")
                    else:
                        self.status_bar.config(text=f"補傳暫停:HTTP {resp.status_code}")
                        return
                except Exception as e:
                    self.status_bar.config(text=f"補傳暫停:{str(e)[:40]}")
                    return
            self.status_bar.config(text=f"補傳完成 {sent}/{total},即時模式")
        finally:
            self._backfill_running = False

    def _csv_row_to_payload(self, row):
        # CSV 欄位:[0]時間 [1]裝置 [2]t [3]ph [4]tds [5]tdse [6]ec [7]lux
        try:
            return {
                "ts": row[0],
                "device_id": row[1],
                "temp": self._cast_num(row[2]),
                "ph": self._cast_num(row[3]),
                "tds": self._cast_num(row[4]),
                "tdse": self._cast_num(row[5]),
                "ec": self._cast_num(row[6]),
                "lux": self._cast_num(row[7]),
            }
        except IndexError:
            return None

    def _cast_num(self, v):
        if v is None or v == '':
            return None
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (ValueError, TypeError):
            return v

    # ---- 序列 + 自動重連 ----

    def handle_serial(self, port):
        """USB 斷線/Arduino 重啟 → 自動重掃 + 重連,不卡死。"""
        while True:
            ser = None
            try:
                available = [p.device for p in serial.tools.list_ports.comports()
                             if 'USB' in p.description or 'ACM' in p.device]
                if port not in available:
                    if available:
                        new_port = available[0]
                        print(f"[serial] {port} 不在了,改用 {new_port}", flush=True)
                        port = new_port
                    else:
                        self.status_bar.config(text="找不到 USB 裝置,10 秒後重試")
                        time.sleep(10)
                        continue

                ser = serial.Serial(port, BAUD_RATE, timeout=1)
                self.status_bar.config(text=f"已連接: {port}")
                print(f"[serial] 連上 {port}", flush=True)

                while True:
                    if ser.in_waiting > 0:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            v = data.get("v", {})
                            device_id = data.get("id", "Unknown")
                            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
                            self.status_bar.config(text=f"成功接收 {device_id} 數據")
                        except Exception as e:
                            print(f"解析錯誤: {e}", flush=True)
            except Exception as e:
                print(f"[serial] {port} 出錯: {type(e).__name__}: {e},10 秒後重連", flush=True)
                self.status_bar.config(text=f"序列斷線:{port} (10秒後重連)")
                try:
                    if ser is not None:
                        ser.close()
                except Exception:
                    pass
                time.sleep(10)

    def start_serial_threads(self):
        ports = [p.device for p in serial.tools.list_ports.comports()
                 if 'USB' in p.description or 'ACM' in p.device]
        if not ports:
            print("[startup] 暫無 USB,啟動 polling thread 等候裝置出現", flush=True)
            self.status_bar.config(text="等候 USB 裝置出現...")
            threading.Thread(target=self.handle_serial, args=("(掃描中)",), daemon=True).start()
            return
        for port in ports:
            threading.Thread(target=self.handle_serial, args=(port,), daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = AquaMindApp(root)
    root.mainloop()
