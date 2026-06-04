"""
把 Claude Code 對話 (JSONL) 轉成 Markdown 檔。

v2 行為:
    - 同一個輸出檔重複呼叫只 append 新訊息,**不會重複寫舊內容**
    - 用 sidecar state 檔(`<output>.state.json`)記住上次處理到 jsonl 第幾行
    - 偵測到 jsonl 換新 session 會自動重置 + 備份舊檔成 .bak
    - 沒新訊息就什麼都不做(可以放心反覆呼叫)

用法:
    python export_conversation.py <jsonl> <output_md>

範例:
    python export_conversation.py session.jsonl 對話紀錄/當前對話.md
    # 第 1 次:寫入所有訊息 + 建立 state 檔
    # 第 2 次(session 變長):只 append 新訊息
    # 第 N 次(session 換了):備份舊檔成 .bak,從頭來

要從頭重新匯出:刪掉 sidecar 的 state 檔(`<output>.state.json`)即可。
"""
import json
import re
import sys
from pathlib import Path


# 隱私遮罩規則 — 寫入 markdown 前一律套用,擋掉敏感字串外洩
# 規則來源:CLAUDE.md 第 6 條(隱私資料保護,最高優先級)
REDACT_PATTERNS = [
    # Gemini / Google AI Studio API key
    (re.compile(r'AIzaSy[A-Za-z0-9_-]{30,}'), '[REDACTED-GEMINI-API-KEY]'),
    # Anthropic / OpenAI 風格 API key
    (re.compile(r'sk-(?:ant-)?[A-Za-z0-9_-]{20,}'), '[REDACTED-API-KEY]'),
    # AWS access key
    (re.compile(r'AKIA[A-Z0-9]{16}'), '[REDACTED-AWS-KEY]'),
    # Google Apps Script Web app URL
    (re.compile(r'https?://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec'), '[REDACTED-APPS-SCRIPT-URL]'),
    # Google Sheet URL(含 ID)
    (re.compile(r'https?://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]+(?:/[^\s)]*)?'), '[REDACTED-SHEET-URL]'),
    # 使用者 email(包含本人)
    (re.compile(r'aaron90407@gmail\.com', re.IGNORECASE), '[REDACTED-EMAIL]'),
]


def redact(text):
    """套用所有遮罩規則,把敏感字串換成 [REDACTED-XXX] 標籤。"""
    if not text:
        return text
    for pat, mask in REDACT_PATTERNS:
        text = pat.sub(mask, text)
    return text


def extract_text(content):
    """從 message.content 提取純文字(可能是 str 或 list[dict])"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_use":
                    name = item.get("name", "?")
                    parts.append(f"\n> 🔧 *[使用工具: {name}]*\n")
                elif item.get("type") == "tool_result":
                    parts.append("\n> 📋 *[工具執行結果]*\n")
                elif item.get("type") == "image":
                    parts.append("\n> 🖼️ *[圖片]*\n")
        return "\n".join(parts)
    return ""


def should_skip(entry):
    """跳過不需要的訊息"""
    t = entry.get("type")
    if t in ("system", "queue-operation"):
        return True
    if entry.get("isMeta"):
        return True
    msg = entry.get("message", {})
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, str):
            if content.startswith("<command-name>") or content.startswith("<local-command"):
                return True
            if content.startswith("<system-reminder>") and len(content) < 300:
                return True
    return False


def format_message(entry):
    """把一筆訊息格式化成 Markdown(回傳 None = 不可用)"""
    msg_type = entry.get("type", "unknown")
    msg = entry.get("message", {})
    content = msg.get("content", "") if isinstance(msg, dict) else ""
    text = extract_text(content)
    text = redact(text)  # 寫入前先擋敏感字串

    if not text.strip():
        return None

    timestamp = entry.get("timestamp", "")[:19].replace("T", " ")

    if msg_type == "user":
        return f"---\n\n### 👤 使用者 `{timestamp}`\n\n{text}\n"
    elif msg_type == "assistant":
        return f"\n### 🤖 Claude `{timestamp}`\n\n{text}\n"
    return None


def load_state(state_path):
    """讀 sidecar state(沒有就回傳預設)"""
    if not state_path.exists():
        return {"session_id": None, "last_line": 0, "n_written": 0}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"session_id": None, "last_line": 0, "n_written": 0}


def save_state(state_path, state):
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def convert(jsonl_path, md_path):
    jsonl_path = Path(jsonl_path)
    md_path = Path(md_path)
    state_path = md_path.with_suffix(md_path.suffix + ".state.json")

    # 用 jsonl 檔名(不含路徑跟副檔名)當 session id
    session_id = jsonl_path.stem
    state = load_state(state_path)

    with open(jsonl_path, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    # 偵測 session 換了 / jsonl 被截斷 → 從頭來
    if state["session_id"] != session_id or len(all_lines) < state["last_line"]:
        if state["session_id"] is not None:
            print(f"[!] Session 變了({state['session_id'][:8]}... → {session_id[:8]}...),重置")
        if md_path.exists():
            backup = md_path.with_suffix(md_path.suffix + ".bak")
            if backup.exists():
                backup.unlink()
            md_path.rename(backup)
            print(f"[!] 舊檔備份為 {backup.name}")
        state = {"session_id": session_id, "last_line": 0, "n_written": 0}

    # 從 last_line 開始處理新訊息
    new_messages = []
    last_processed_line = state["last_line"]

    for i, line in enumerate(all_lines):
        if i < state["last_line"]:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            last_processed_line = i + 1
            continue
        if should_skip(entry):
            last_processed_line = i + 1
            continue
        formatted = format_message(entry)
        if formatted:
            new_messages.append(formatted)
        last_processed_line = i + 1

    if not new_messages:
        # 仍要更新 state 的 last_line(可能跳過了一些不可用訊息)
        state["last_line"] = last_processed_line
        save_state(state_path, state)
        print(f"[OK] 沒有新訊息,檔案不變(總訊息 {state['n_written']})")
        return

    is_first_write = state["n_written"] == 0

    if is_first_write:
        # 第一次寫入:加 header
        header = f"# Claude Code 對話紀錄\n\n來源檔案：`{jsonl_path.name}`\n\n"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write("\n".join(new_messages))
    else:
        # 後續 append(前面留個空行銜接)
        with open(md_path, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write("\n".join(new_messages))

    state["last_line"] = last_processed_line
    state["n_written"] += len(new_messages)
    save_state(state_path, state)

    mode = "首次寫入" if is_first_write else "增量追加"
    print(f"[OK] {mode}:{md_path}")
    print(f"     新增訊息:{len(new_messages)}")
    print(f"     檔案總訊息:{state['n_written']}")
    print(f"     當前大小:{md_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    jsonl_path = sys.argv[1]
    md_path = sys.argv[2]
    convert(jsonl_path, md_path)
