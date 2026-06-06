"""
程式存活檢查 — cron 每 N 分鐘跑,發現監看的程式沒在跑就寄 email 通知。
(檔名後綴 _email 表示此程式會發信。設定在 config.py)

用法:
    cd ~/AquaMind/analysis
    python3 watchdog_email.py        # 跑一次

排程(crontab,每 10 分鐘一次):
    */10 * * * * cd /home/rasppi/AquaMind/analysis && /usr/bin/python3 watchdog_email.py >> /tmp/watchdog.log 2>&1

設計:
    - 用 pgrep 看 ps 列表有沒有對應關鍵字
    - 第一次發現死掉 → 寄 email
    - 之後 10 分鐘還是死 → **不再寄**(避免轟炸信箱)
    - 程式復活 → state 清除,下次死又會通報
"""
import os
import subprocess
from datetime import datetime

from email_helper import send_email, is_configured as email_configured

# 要監看的程式(ps 關鍵字 → 顯示名稱)
# slicer 已停用(使用者決定不再跑),只監看 GUI
WATCHED = {
    "rpi_gui_monitor.py": "🖥  主程式 GUI(rpi_gui_monitor.py)",
}

# 已通報的死亡狀態存這裡,避免每 10 分鐘狂寄
STATE_FILE = os.path.expanduser("~/.watchdog_state.txt")


def is_running(keyword):
    """檢查 ps 有沒有任何進程含 keyword"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", keyword],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def load_state():
    """讀已通報的程式清單(每行一個 key)"""
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except Exception:
        return set()


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for item in sorted(state):
            f.write(item + "\n")


def main():
    if not email_configured():
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ⚠ Email 未設好,跳過")
        return

    already_reported = load_state()
    new_state = set()
    fresh_deaths = []      # 這次新發現死的(要寄信)
    recovered = []         # 之前死現在活的(只印 log,不寄信)

    for key, name in WATCHED.items():
        if is_running(key):
            if key in already_reported:
                recovered.append(name)
            # 活著 → 不放進 new_state(等於從通報清單清掉)
        else:
            new_state.add(key)
            if key not in already_reported:
                fresh_deaths.append(name)

    save_state(new_state)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if fresh_deaths:
        subject = "🚨 程式停止運作"
        lines = [f"檢查時間:{ts}", "", "以下程式偵測到未在運作:"]
        for name in fresh_deaths:
            lines.append(f"  • {name}")
        lines += [
            "",
            "可能原因:Pi 重開機、記憶體不足、程式 crash 等。",
            "處理:SSH 登入 Pi,用 nohup 重新啟動該程式。",
            "",
            "(此信只在「狀態變死」時寄一次,後續仍死不再轟炸;",
            " 程式復活後若再死,會再寄一次新通知。)",
        ]
        body = "\n".join(lines)
        ok, err = send_email(subject, body)
        if ok:
            print(f"[{ts}] ✉ 通報 {len(fresh_deaths)} 個程式死亡")
        else:
            print(f"[{ts}] ⚠ 寄信失敗:{err}")

    if recovered:
        for name in recovered:
            print(f"[{ts}] ✓ {name} 已復活")

    if not fresh_deaths and not recovered:
        n_alive = len(WATCHED) - len(new_state)
        print(f"[{ts}] ✓ {n_alive}/{len(WATCHED)} 程式運作中")


if __name__ == "__main__":
    main()
