"""
藻類監測網頁儀表板(Streamlit)。
從 Google Sheets 抓資料,顯示即時數據、互動圖表、異常分析、AI 日報。

本機跑:
    pip install -r requirements.txt
    streamlit run dashboard.py

線上 host(免費):
    1. 把 analysis/ 資料夾推上 GitHub
    2. https://share.streamlit.io 登入 → 連 GitHub repo
    3. 主檔指定 analysis/dashboard.py
    4. Settings → Secrets 貼:
         GOOGLE_SHEET_CSV_URL = "..."
         GEMINI_API_KEY       = "..."
    5. Deploy → 取得永久網址,手機也能看

Google Sheet CSV URL 怎麼拿:
    Sheet → 共用 → 「知道連結的人」可檢視
    把網址 /edit?... 換成 /export?format=csv
"""
import os
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta


# ============ 設定(從 Streamlit secrets 或環境變數讀)============
def _get_secret(key, default=""):
    """先看 streamlit secrets,再看環境變數"""
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)


SHEET_CSV_URL = _get_secret("GOOGLE_SHEET_CSV_URL")
GEMINI_API_KEY = _get_secret("GEMINI_API_KEY")

from sensor_codes import (
    DISCONNECT_CODE, USER_DISABLED_CODE, FIRMWARE_MISSING_CODE, NO_DATA_CODES,
)

SENSOR_COLS = [
    "溫度(°C)",
    "酸鹼(pH)",
    "溶解(ppm)",
    "TDS(EC)(ppm)",
    "導電(mS/cm)",
    "濁度(NTU)",
    "光照(lx)",
    "CO2_B(ppm)",
    "CO2_C(ppm)",
]

# NO_DATA_CODES 從 sensor_codes 來,但 dashboard 用法需要 list(.isin)
NO_DATA_CODES_LIST = list(NO_DATA_CODES)

UNITS_BRIEF = {
    "溫度(°C)": "°C", "酸鹼(pH)": "", "溶解(ppm)": "ppm", "TDS(EC)(ppm)": "ppm",
    "導電(mS/cm)": "mS/cm", "濁度(NTU)": "NTU", "光照(lx)": "lx",
    "CO2_B(ppm)": "ppm", "CO2_C(ppm)": "ppm"
}

HARD_LIMITS = {
    "溫度(°C)":      (15, 35),
    "酸鹼(pH)":      (6.5, 9.5),
    "TDS(EC)(ppm)": (15000, 50000),
    "導電(mS/cm)":   (25, 60),
    "濁度(NTU)":     (0, 3000),
    "光照(lx)":      (0, 50000),
    "CO2_B(ppm)":    (200, 5000),
    "CO2_C(ppm)":    (200, 5000),
}


# ============ 頁面設定 ============
st.set_page_config(
    page_title="海水小球藻監測",
    page_icon="🌱",
    layout="wide",
)


# ============ 資料載入(快取 60 秒)============
@st.cache_data(ttl=60)
def load_data(url):
    if not url:
        return None, "未設定 GOOGLE_SHEET_CSV_URL"
    try:
        df = pd.read_csv(url)
    except Exception as e:
        return None, f"讀取失敗:{e}"
    if "時間" not in df.columns:
        return None, "找不到「時間」欄位,確認 Google Sheet 第一列是標題"
    df["時間"] = pd.to_datetime(df["時間"], errors="coerce")
    df = df.dropna(subset=["時間"]).set_index("時間").sort_index()
    return df, None


def filter_disconnect(df):
    """把 -1/-2/-3 全換成 NaN,只回傳 SENSOR_COLS 中存在的欄位"""
    cols = [c for c in SENSOR_COLS if c in df.columns]
    out = df[cols].apply(pd.to_numeric, errors="coerce")
    return out.where(~out.isin(NO_DATA_CODES_LIST))


# ============ AI 日報 ============
def generate_ai_report(day_df, date):
    if not GEMINI_API_KEY:
        return None, "缺 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
    except ImportError:
        return None, "缺 google-generativeai 套件:pip install google-generativeai"

    cols = [c for c in SENSOR_COLS if c in day_df.columns]
    clean = day_df[cols].apply(pd.to_numeric, errors="coerce")
    clean = clean.where(~clean.isin(NO_DATA_CODES_LIST))

    lines = []
    for col in cols:
        s = clean[col].dropna()
        if len(s) == 0:
            lines.append(f"- {col}: 全程斷線")
            continue
        first, last = s.iloc[0], s.iloc[-1]
        lines.append(
            f"- {col}: 平均 {s.mean():.2f}, 範圍 {s.min():.2f}~{s.max():.2f}, "
            f"日內變化 {last - first:+.2f}"
        )
    stats_text = "\n".join(lines)

    prompt = f"""你是水產養殖與藻類培養專家。以下是 {date} 一日的「海水小球藻」(marine Chlorella)監測數據:

{stats_text}

請以繁體中文撰寫日報,250 字以內,包含:
1. 總體判斷(藻類狀態)
2. 3 個重點觀察(濁度趨勢、pH 變化、CO2 與光照協同等)
3. 需警戒的異常(若有)
4. 明日操作建議
直接寫,不要客套也不要重複數據。"""

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(
            prompt,
            generation_config={"max_output_tokens": 4000},  # 2.5-flash thinking 吃 ~1500-2000
        )
        return response.text, None
    except Exception as e:
        return None, f"API 錯誤:{e}"


# ============ 主頁面 ============
st.title("🌱 海水小球藻監測儀表板")

df, err = load_data(SHEET_CSV_URL)
if df is None:
    st.error(f"❌ {err}")
    with st.expander("如何設定?"):
        st.markdown("""
**Google Sheet 必須公開**:
1. 開啟你的 Google Sheet
2. 右上角「共用」→ 一般存取權改為「**知道連結的人**」、權限「檢視者」
3. 取得 CSV 匯出 URL:把分享網址裡的 `/edit#gid=0` 換成 `/export?format=csv`

**設定 Secrets**(本機用環境變數,Streamlit Cloud 用 secrets.toml):
```
GOOGLE_SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/XXX/export?format=csv"
GEMINI_API_KEY       = "AIzaSy..."  # 想用 AI 日報才需要(免費 https://aistudio.google.com/apikey)
```
        """)
    st.stop()

if df.empty:
    st.warning("⚠️ Google Sheet 連線成功,但目前沒有資料")
    st.caption(f"欄位:{list(df.columns)}")
    st.markdown("""
**可能原因**:
- Sheet 只有標題列,Pi 端還沒上傳任何資料
- 「時間」欄位的格式 pandas 認不出來(整欄被當成無效時間丟棄)
- Pi 上傳的時間欄欄名不是「時間」
    """)
    st.stop()


# ============ 側邊欄 ============
with st.sidebar:
    st.header("⚙️ 控制")
    if st.button("🔄 重新整理", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"資料總筆數:{len(df)}")
    st.caption(f"資料區間:{df.index.min().strftime('%Y-%m-%d %H:%M')} → {df.index.max().strftime('%Y-%m-%d %H:%M')}")

    st.divider()
    st.subheader("📅 時間範圍")
    range_option = st.radio(
        "選擇時段",
        ["最近 1 小時", "最近 24 小時", "最近 7 天", "全部", "自訂"],
        index=1,
    )

    if range_option == "最近 1 小時":
        df_view = df[df.index >= df.index.max() - pd.Timedelta(hours=1)]
    elif range_option == "最近 24 小時":
        df_view = df[df.index >= df.index.max() - pd.Timedelta(hours=24)]
    elif range_option == "最近 7 天":
        df_view = df[df.index >= df.index.max() - pd.Timedelta(days=7)]
    elif range_option == "自訂":
        d_start = st.date_input("起始", df.index.min().date())
        d_end = st.date_input("結束", df.index.max().date())
        df_view = df[(df.index >= pd.Timestamp(d_start)) &
                     (df.index < pd.Timestamp(d_end) + pd.Timedelta(days=1))]
    else:
        df_view = df


# ============ 標籤頁 ============
tab1, tab2, tab3, tab4 = st.tabs(["📊 即時數據", "📈 歷史趨勢", "⚠️ 異常分析", "🤖 AI 日報"])


# ---------- Tab 1: 即時數據 ----------
with tab1:
    st.subheader("最新讀值")
    latest = df.iloc[-1]
    cols = st.columns(5)
    for i, col in enumerate(SENSOR_COLS):
        if col not in df.columns:
            continue
        val = latest[col]
        with cols[i % 5]:
            if pd.isna(val) or val in NO_DATA_CODES:
                label = {-1: "未接", -2: "已關閉", -3: "未送"}.get(val, "未接")
                st.metric(col, label, delta="⚠️", delta_color="off")
            else:
                lo, hi = HARD_LIMITS.get(col, (None, None))
                if lo is not None and (val < lo or val > hi):
                    st.metric(col, f"{val:.2f} {UNITS_BRIEF.get(col, '')}",
                              delta="🔴 超限", delta_color="inverse")
                else:
                    st.metric(col, f"{val:.2f} {UNITS_BRIEF.get(col, '')}",
                              delta="✓", delta_color="off")
    st.caption(f"更新時間:{df.index[-1]}")

    st.divider()
    st.subheader("最近 24 小時走勢(全部感測器)")
    recent = df[df.index >= df.index.max() - pd.Timedelta(hours=24)]
    clean_recent = filter_disconnect(recent)
    if not clean_recent.empty:
        # 一張多子圖
        fig = go.Figure()
        for col in clean_recent.columns:
            fig.add_trace(go.Scatter(
                x=clean_recent.index, y=clean_recent[col],
                name=col, mode='lines'
            ))
        fig.update_layout(
            height=500,
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------- Tab 2: 歷史趨勢 ----------
with tab2:
    st.subheader(f"歷史趨勢 — 顯示 {len(df_view)} 筆資料")

    if df_view.empty:
        st.info("選擇的時段內沒有資料")
    else:
        clean = filter_disconnect(df_view)

        selected = st.multiselect(
            "選擇感測器(可複選)",
            options=SENSOR_COLS,
            default=["溫度(°C)", "酸鹼(pH)", "濁度(NTU)", "光照(lx)"],
        )

        for col in selected:
            if col not in clean.columns:
                continue
            fig = px.line(
                x=clean.index, y=clean[col],
                title=col,
                labels={"x": "時間", "y": col},
            )
            lo, hi = HARD_LIMITS.get(col, (None, None))
            if lo is not None:
                fig.add_hline(y=lo, line_dash="dash", line_color="red",
                              annotation_text=f"下限 {lo}")
                fig.add_hline(y=hi, line_dash="dash", line_color="red",
                              annotation_text=f"上限 {hi}")
            fig.update_layout(height=300, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 統計表"):
            stats = pd.DataFrame({
                "最小": clean.min(),
                "平均": clean.mean(),
                "最大": clean.max(),
                "標準差": clean.std(),
                "有效筆數": clean.count(),
                "真斷線筆數(-1)": (df_view[clean.columns] == DISCONNECT_CODE).sum(),
                "已關閉筆數(-2)": (df_view[clean.columns] == USER_DISABLED_CODE).sum(),
                "未送筆數(-3)": (df_view[clean.columns] == FIRMWARE_MISSING_CODE).sum(),
            }).round(2)
            st.dataframe(stats, use_container_width=True)


# ---------- Tab 3: 異常分析 ----------
with tab3:
    st.subheader("異常事件清單")
    st.caption("依目前所選時段)")

    clean = filter_disconnect(df_view)
    anomalies = []
    for col in SENSOR_COLS:
        if col not in clean.columns:
            continue
        lo, hi = HARD_LIMITS.get(col, (None, None))
        if lo is None:
            continue
        outliers = clean[(clean[col] < lo) | (clean[col] > hi)][col].dropna()
        for ts, val in outliers.items():
            anomalies.append({
                "時間": ts,
                "感測器": col,
                "讀值": round(float(val), 2),
                "正常範圍": f"[{lo}, {hi}]",
                "類型": "硬限超出",
            })

    # 真斷線統計(只算 -1,排除使用者主動關閉的 -2 跟韌體沒送的 -3)
    sensor_cols_present = [c for c in SENSOR_COLS if c in df_view.columns]
    disconnect_count = (df_view[sensor_cols_present] == DISCONNECT_CODE).sum()
    disconnect_summary = disconnect_count[disconnect_count > 0].sort_values(ascending=False)

    col_a, col_b = st.columns(2)
    with col_a:
        st.metric("硬限異常事件", len(anomalies))
    with col_b:
        st.metric("真斷線總筆數", int(disconnect_summary.sum()) if len(disconnect_summary) > 0 else 0)

    if disconnect_summary.shape[0] > 0:
        st.write("**各感測器斷線次數:**")
        st.bar_chart(disconnect_summary)

    if anomalies:
        anom_df = pd.DataFrame(anomalies).sort_values("時間", ascending=False)
        st.dataframe(anom_df, use_container_width=True)
    else:
        st.success("✅ 所選時段內沒有偵測到硬限異常")


# ---------- Tab 4: AI 日報 ----------
with tab4:
    st.subheader("AI 自動日報")
    st.caption(f"由 Gemini API 產生中文日報(免費,每天 1500 次配額)")

    selected_date = st.date_input(
        "選擇日期",
        value=df.index[-1].date(),
        min_value=df.index.min().date(),
        max_value=df.index.max().date(),
    )

    if not GEMINI_API_KEY:
        st.warning("⚠️ 還沒設定 GEMINI_API_KEY,無法呼叫 Gemini API")

    if st.button("🤖 產生 AI 日報", type="primary", disabled=not GEMINI_API_KEY):
        day_df = df[df.index.date == selected_date]
        if day_df.empty:
            st.warning(f"{selected_date} 沒有資料")
        else:
            with st.spinner(f"正在請 Gemini 分析 {selected_date}({len(day_df)} 筆資料)..."):
                report, err = generate_ai_report(day_df, selected_date.strftime("%Y-%m-%d"))
            if err:
                st.error(err)
            else:
                st.markdown("### 📋 分析結果")
                st.markdown(report)
                st.download_button(
                    "下載 .md",
                    data=report,
                    file_name=f"ai_report_{selected_date}.md",
                    mime="text/markdown",
                )


# ============ 頁尾 ============
st.divider()
st.caption("🌱 海水小球藻監測系統 · 資料每 60 秒從 Google Sheets 自動更新")
