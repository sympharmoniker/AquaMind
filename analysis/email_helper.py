"""
Email 寄送共用模組(library)。

設定來源優先序:
  1. 環境變數(SMTP_USER / SMTP_PASSWORD / SMTP_TO / SMTP_SUBJECT_PREFIX)
     ← aquamind_app.py 設定視窗寫入 ~/aquamind_config.env,cron 啟動時 source 進來
  2. analysis/config.py(舊版 Pi 設定,保留向後相容)

收件人(SMTP_TO 或 EMAIL_RECEIVER)支援多人,**用逗號或分號分隔**:
    SMTP_TO="alice@example.com, bob@example.com; charlie@example.com"
"""
import os
import smtplib
from email.mime.text import MIMEText

# 嘗試 import 舊版 config.py(沒有就跳過,別爆)
try:
    from config import (
        EMAIL_ENABLED as _CFG_ENABLED,
        EMAIL_SENDER as _CFG_SENDER,
        EMAIL_APP_PASSWORD as _CFG_PASSWORD,
        EMAIL_RECEIVER as _CFG_RECEIVER,
        EMAIL_SUBJECT_PREFIX as _CFG_PREFIX,
    )
except ImportError:
    _CFG_ENABLED = True
    _CFG_SENDER = ""
    _CFG_PASSWORD = ""
    _CFG_RECEIVER = ""
    _CFG_PREFIX = "[AquaMind]"


def _get(env_key, fallback):
    """env 有就用,否則用 fallback"""
    v = os.environ.get(env_key, "")
    return v if v else fallback


def _resolve():
    """整合 env + config.py,回傳當前設定。"""
    return {
        "enabled": _CFG_ENABLED,
        "sender": _get("SMTP_USER", _CFG_SENDER),
        "password": _get("SMTP_PASSWORD", _CFG_PASSWORD),
        "receivers_raw": _get("SMTP_TO", _CFG_RECEIVER),
        "prefix": _get("SMTP_SUBJECT_PREFIX", _CFG_PREFIX),
    }


def _parse_receivers(raw):
    """字串 → 收件人 list(逗號/分號分隔)。"""
    if not raw:
        return []
    # 同時接受 , 跟 ;
    normalized = raw.replace(";", ",")
    return [r.strip() for r in normalized.split(",") if r.strip()]


def is_configured():
    """判斷 email 設定是否齊全且看似真實值。"""
    cfg = _resolve()
    if not cfg["enabled"]:
        return False
    receivers = _parse_receivers(cfg["receivers_raw"])
    if not cfg["sender"] or not cfg["password"] or not receivers:
        return False
    # 排除明顯的範例字串
    if "your_account" in cfg["sender"] or "xxxx" in cfg["password"]:
        return False
    return True


def send_email(subject, body):
    """寄送一封 email。Gmail SMTP SSL 465,支援多收件人。

    回傳 (success: bool, error_msg: str | None)
    """
    cfg = _resolve()
    if not cfg["enabled"]:
        return False, "EMAIL_ENABLED = False"
    if not is_configured():
        return False, "Email 設定未完成(請從 GUI 設定視窗填好,或填 config.py)"

    receivers = _parse_receivers(cfg["receivers_raw"])

    try:
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = f"{cfg['prefix']} {subject}"
        msg["From"] = cfg["sender"]
        # To 標頭:多人時用逗號 join,顯示在郵件裡每個人都看得到其他收件人
        msg["To"] = ", ".join(receivers)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(cfg["sender"], cfg["password"].replace(" ", ""))
            # 明確傳 to_addrs list,而不是依賴 server.send_message() 解析 To 標頭
            # 這樣不管 To 標頭格式怎樣,SMTP 都會對每個 receiver 各送一封
            server.sendmail(cfg["sender"], receivers, msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)
