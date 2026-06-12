#!/bin/bash
# AquaMind GUI 韌性方案安裝器
#   - autostart .desktop:Pi 開機 + 登入桌面後自動啟動 GUI
#   - run_gui.sh wrapper:GUI crash 後 30 秒自動重啟
#   - 寫入位置都在 Pi 使用者家目錄,不會污染 git repo
#
# 用法(在 Pi 上跑一次就好):
#   bash ~/AquaMind/setup/install_resilience.sh
#
# 移除:
#   rm ~/Desktop/run_gui.sh ~/.config/autostart/aquamind-gui.desktop

set -e

if [ ! -f "$HOME/Desktop/rpi_gui_monitor.py" ]; then
    echo "❌ 找不到 ~/Desktop/rpi_gui_monitor.py — 請確認你在對的 Pi 上、檔案在桌面"
    exit 1
fi

# 0. 把 git repo 裡的新版 rpi_gui_monitor.py 同步到桌面(含斷點補傳)
REPO_GUI="$HOME/AquaMind/rpi_gui_monitor.py"
DESKTOP_GUI="$HOME/Desktop/rpi_gui_monitor.py"
if [ -f "$REPO_GUI" ]; then
    if ! cmp -s "$REPO_GUI" "$DESKTOP_GUI"; then
        BACKUP="$DESKTOP_GUI.before-resilience.bak"
        cp "$DESKTOP_GUI" "$BACKUP"
        cp "$REPO_GUI" "$DESKTOP_GUI"
        echo "[OK] 桌面版 GUI 更新為 git 最新版(舊版備份成 $(basename $BACKUP))"
    else
        echo "[OK] 桌面版 GUI 已是最新,跳過 cp"
    fi
else
    echo "[!] ~/AquaMind/rpi_gui_monitor.py 不存在,跳過版本同步(請先 git pull)"
fi

# 1. wrapper:crash 自動重啟迴圈,但「使用者按 X」會乖乖退出
WRAPPER="$HOME/Desktop/run_gui.sh"
cat > "$WRAPPER" <<'WRAP_EOF'
#!/bin/bash
# rpi_gui_monitor.py 重啟迴圈
#   - crash / USB 斷線 / 訊號異常 → 30 秒自動重起
#   - 使用者按右上角 X → 寫 ~/.gui_user_closed 旗標 → wrapper exit 不再重起
# 安裝來源:~/AquaMind/setup/install_resilience.sh
cd "$HOME/Desktop"
LOG="$HOME/gui.log"
MARKER="$HOME/.gui_user_closed"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

# wrapper 一啟動就清掉舊旗標(避免上次的關閉狀態誤判)
rm -f "$MARKER"

while true; do
  rm -f "$MARKER"  # 每次啟動前再清一次,確保乾淨
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 啟動 rpi_gui_monitor.py (DISPLAY=$DISPLAY)" >> "$LOG"
  /usr/bin/python3 "$HOME/Desktop/rpi_gui_monitor.py" >> "$LOG" 2>&1
  RC=$?

  # 使用者主動關閉 → wrapper 也跟著退,不再重啟
  if [ -f "$MARKER" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 使用者按 X 關閉,wrapper 一併退出" >> "$LOG"
    rm -f "$MARKER"
    exit 0
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 程式異常結束 (exit=$RC),30 秒後重啟" >> "$LOG"
  sleep 30
done
WRAP_EOF
chmod +x "$WRAPPER"
echo "[OK] 寫好 wrapper: $WRAPPER"

# 2. autostart:登入桌面自動跑 wrapper
mkdir -p "$HOME/.config/autostart"
AUTO="$HOME/.config/autostart/aquamind-gui.desktop"
cat > "$AUTO" <<AUTO_EOF
[Desktop Entry]
Type=Application
Name=AquaMind GUI Monitor
Comment=自動啟動 rpi_gui_monitor.py,crash 後 30 秒重啟
Exec=$HOME/Desktop/run_gui.sh
X-GNOME-Autostart-enabled=true
NoDisplay=true
Terminal=false
AUTO_EOF
echo "[OK] 寫好 autostart: $AUTO"

# 3. 把現有 log 備份(避免新舊混雜)
if [ -f "$HOME/gui.log" ] && [ -s "$HOME/gui.log" ]; then
    BACKUP="$HOME/gui.log.before-resilience.bak"
    cp "$HOME/gui.log" "$BACKUP"
    echo "[OK] 舊 gui.log 備份成 $BACKUP"
fi

cat <<'DONE'

🎉 安裝完成。

行為:
  - 下次 Pi 開機 + 自動登入桌面 → GUI 自動啟動
  - GUI 若 crash → 30 秒後 wrapper 自動重啟它
  - 日誌都寫在 ~/gui.log

要立刻測試:
  1. 殺掉現在手動跑的 GUI: pkill -f rpi_gui_monitor.py
  2. 重新登出桌面 + 再登入,或直接 sudo reboot
  3. 登入桌面後 wrapper 會在背景跑,GUI 視窗會出現

要回滾:
  rm ~/Desktop/run_gui.sh ~/.config/autostart/aquamind-gui.desktop

DONE
