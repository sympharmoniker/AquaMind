"""
數據自動分流工具(v2)— 把主 CSV 切成 12 小時一個的備份檔。

監看 ~/Desktop/algae_monitor_data.csv,每 10 秒檢查新資料,依時間戳分到:
    00:00~11:59 → split_YYYYMMDD_0000-1159.csv
    12:00~23:59 → split_YYYYMMDD_1200-2359.csv

v2 修正(相對於 v1):
    1. 每筆 row 處理完立刻更新書籤(舊版整批處理完才更新 → 中途 crash 會重複)
    2. target_file 開過就快取,一輪處理只開關一次(舊版每筆都開關 → 傷 SD 卡)
    3. 沒新資料時每 5 分鐘印一次「待命」訊息(舊版完全靜默 → 看不出活著沒)
    4. 連續錯誤計數,致命時 backoff 5 分鐘(舊版 every 10 秒重試,disk full 也不停)
    5. 寫到一半的 row(沒換行)不處理,等下一輪再讀完整版
"""
import os
import time
import csv
from datetime import datetime

# --- 設定 ---
DESKTOP_PATH = os.path.expanduser("~/Desktop")
MAIN_CSV = os.path.join(DESKTOP_PATH, "algae_monitor_data.csv")
BOOKMARK_FILE = os.path.join(DESKTOP_PATH, ".slicer_bookmark.txt")

CHECK_INTERVAL = 10        # 多久檢查主檔案一次(秒)
HEARTBEAT_INTERVAL = 300   # 沒新資料時多久印一次「待命」(秒)
MAX_CONSECUTIVE_ERRORS = 10  # 連續這麼多次主迴圈錯誤就 backoff

# 感測器欄位定義(需與 rpi_gui_monitor.py 同步)
SENSOR_KEYS = ["t", "ph", "tds", "tdse", "ec", "turb", "lux", "c2b", "c2c"]
SENSOR_LABELS = {
    "t": "溫度", "ph": "酸鹼", "tds": "溶解", "tdse": "TDS(EC)", "ec": "導電",
    "turb": "濁度", "lux": "光照", "c2b": "CO2_B", "c2c": "CO2_C"
}
SENSOR_UNITS = {
    "t": "°C", "ph": "pH", "tds": "ppm", "tdse": "ppm", "ec": "mS/cm",
    "turb": "NTU", "lux": "lx", "c2b": "ppm", "c2c": "ppm"
}


def ts():
    return datetime.now().strftime("%H:%M:%S")


def get_split_filename(timestamp_str):
    """根據數據的時間戳記決定 12 小時分段檔名"""
    try:
        dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        date_str = dt.strftime("%Y%m%d")
        time_range = "0000-1159" if dt.hour < 12 else "1200-2359"
        return os.path.join(DESKTOP_PATH, f"split_{date_str}_{time_range}.csv")
    except ValueError:
        # 時間戳格式錯,丟到 error_logs
        return os.path.join(DESKTOP_PATH, "split_error_logs.csv")


def get_csv_header():
    """跟 rpi_gui_monitor.py 同步的標題列"""
    header = ["時間", "裝置"]
    for key in SENSOR_KEYS:
        header.append(f"{SENSOR_LABELS[key]}({SENSOR_UNITS[key]})")
    return header


def load_bookmark():
    """讀取書籤(上次處理到主 CSV 第幾個 byte)"""
    if not os.path.exists(BOOKMARK_FILE):
        return 0
    try:
        with open(BOOKMARK_FILE, 'r') as f:
            return int(f.read().strip())
    except (ValueError, IOError):
        return 0


def save_bookmark(pos):
    """更新書籤"""
    with open(BOOKMARK_FILE, 'w') as bf:
        bf.write(str(pos))


def process_new_data(start_pos):
    """讀主 CSV 從 start_pos 開始,把新 row 寫到對應 12 小時切片檔。

    回傳: (處理的 row 數, 結束位置)
    """
    rows_processed = 0
    file_handles = {}  # target_file_path -> (file_obj, csv_writer)
    pos = start_pos

    try:
        with open(MAIN_CSV, 'r', encoding='utf-8-sig') as f:
            f.seek(start_pos)

            while True:
                line = f.readline()
                if not line:
                    break  # EOF

                # 修正 5: 沒換行的 row 是「主程式還在寫」,不處理,下一輪再讀完整版
                if not line.endswith('\n'):
                    break

                pos_after = f.tell()

                stripped = line.strip()
                if not stripped:
                    pos = pos_after
                    save_bookmark(pos)
                    continue

                try:
                    row = next(csv.reader([line]))
                except csv.Error as e:
                    print(f"[{ts()}] ⚠ CSV 解析失敗: {e},跳過")
                    pos = pos_after
                    save_bookmark(pos)
                    continue

                # 跳過標題行(第一次跑或主檔被重新寫過)
                first_cell = row[0].strip() if row else ""
                if first_cell in ("時間", "Timestamp", "時間戳", "Date", "datetime"):
                    pos = pos_after
                    save_bookmark(pos)
                    continue

                if len(row) < 2:
                    print(f"[{ts()}] ⚠ 欄位不足,跳過: {row}")
                    pos = pos_after
                    save_bookmark(pos)
                    continue

                # 決定切片檔
                timestamp = row[0].strip()
                target_file = get_split_filename(timestamp)

                # 修正 2: target_file 沒開過才 open(這輪內後續直接重用)
                if target_file not in file_handles:
                    file_existed_before = os.path.exists(target_file)
                    of = open(target_file, 'a', newline='', encoding='utf-8-sig')
                    fw = csv.writer(of)
                    file_handles[target_file] = (of, fw)
                    if not file_existed_before:
                        fw.writerow(get_csv_header())
                        print(f"[{ts()}] ✓ 新建 {os.path.basename(target_file)}")

                _, writer = file_handles[target_file]
                writer.writerow(row)
                rows_processed += 1

                # 修正 1: 每筆寫成功就更新書籤,crash 時最多只會重複最後 1 筆
                pos = pos_after
                save_bookmark(pos)

    finally:
        # 關閉所有打開的切片檔(觸發 OS flush)
        for of, _ in file_handles.values():
            try:
                of.close()
            except Exception:
                pass

    return rows_processed, pos


def main():
    print("=== 數據自動分流工具(v2)已啟動 ===")
    print(f"主檔案: {MAIN_CSV}")
    print(f"切片規則: 00:00~11:59 → split_YYYYMMDD_0000-1159.csv")
    print(f"          12:00~23:59 → split_YYYYMMDD_1200-2359.csv")
    print(f"檢查週期 {CHECK_INTERVAL}s / 待命心跳 {HEARTBEAT_INTERVAL}s")
    print()

    last_pos = load_bookmark()
    last_heartbeat = 0.0
    consecutive_errors = 0

    while True:
        try:
            if not os.path.exists(MAIN_CSV):
                if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                    print(f"[{ts()}] ⚠ 主檔案不存在,等待中")
                    last_heartbeat = time.time()
                time.sleep(CHECK_INTERVAL)
                continue

            current_size = os.path.getsize(MAIN_CSV)

            # 主檔被截短 / 被刪重建 → 重設書籤
            if current_size < last_pos:
                print(f"[{ts()}] ⚠ 主檔縮小({current_size} < {last_pos}),重設書籤")
                last_pos = 0
                save_bookmark(0)

            if current_size > last_pos:
                rows, last_pos = process_new_data(last_pos)
                if rows > 0:
                    print(f"[{ts()}] ✓ 同步 {rows} 筆 | 主檔 {current_size} bytes | 書籤 {last_pos}")
                    last_heartbeat = time.time()
            elif time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                # 修正 3: 沒新資料也要心跳,讓人從 log 看出還活著
                print(f"[{ts()}] ✓ 待命 | 主檔 {current_size} bytes | 書籤 {last_pos}")
                last_heartbeat = time.time()

            consecutive_errors = 0

        except KeyboardInterrupt:
            print(f"\n[{ts()}] 收到 Ctrl+C,離開")
            break
        except Exception as e:
            # 修正 4: 連續錯誤計數,過多就 backoff 而不是死命重試
            consecutive_errors += 1
            print(f"[{ts()}] ❌ 主迴圈錯誤(第 {consecutive_errors} 次連續): {type(e).__name__}: {e}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"[{ts()}] ⚠ 連續 {MAX_CONSECUTIVE_ERRORS} 次失敗,等 5 分鐘再重試")
                time.sleep(300)
                consecutive_errors = 0

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
