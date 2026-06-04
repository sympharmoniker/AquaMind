"""
邊緣 AI 監測 — 本地異常檢測 + 趨勢預測,不依賴雲端 API。

兩個核心功能:
    1. 異常檢測(IsolationForest 無監督模型,自動學什麼是正常)
    2. 趨勢預測(簡單線性外推 + 滾動視窗,預測未來 N 小時)

排除感測器:CO2_B(沒實裝)

CLI 用法:
    # 訓練模型(用一份或多份歷史 CSV)
    python edge_ai.py train <CSV1> [<CSV2> ...]

    # 用最新資料檢查異常
    python edge_ai.py check <CSV>

    # 預測未來 24 小時趨勢
    python edge_ai.py forecast <CSV> [--hours 24]

訓練好的模型存在 anomaly_model.pkl(與本檔同目錄)。
"""
import sys
import pickle
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

# 要分析的感測器(全部,不含 CO2_B)
SENSORS = [
    '溫度(°C)',
    '酸鹼(pH)',
    '溶解(ppm)',
    'TDS(EC)(ppm)',
    '導電(mS/cm)',
    '濁度(NTU)',
    '光照(lx)',
    'CO2_C(ppm)',
]

# 「無資料」代碼(rpi_gui_monitor 寫入慣例)
NO_DATA_CODES = {-1, -2, -3}

# 模型存放位置(與本檔同目錄)
MODEL_PATH = Path(__file__).parent / 'anomaly_model.pkl'

# 光照狀態判斷閾值(根據實測資料)
# OFF(<50):刻意關燈時段,濁度乾淨
# ON(>1500):刻意開燈時段,濁度受光干擾
# MID(50-1500):光照感測器掉了 / 故障,該時段所有讀值不可信
LIGHT_OFF_MAX = 50
LIGHT_ON_MIN = 1500


def classify_light_state(lux):
    """根據光照數值分類:OFF / ON / MID"""
    if pd.isna(lux):
        return 'MID'
    if lux < LIGHT_OFF_MAX:
        return 'OFF'
    elif lux > LIGHT_ON_MIN:
        return 'ON'
    else:
        return 'MID'


def estimate_lux_coefficient(df, baseline_window='30min'):
    """從資料估光照-濁度回歸係數 k(假設 濁度_raw ≈ 濁度_real + k * lux)。

    作法:
      1. 用 OFF 時段(lx<50 且濁度>0)的濁度當「真實基線」
      2. **對 OFF 取滾動中位數**過濾攪拌尖刺(真實 high frequency 物理事件)
         → 留下「兩次攪拌之間的安靜值」當基線
      3. 時間插值把基線延伸到 ON 時段
      4. ON 時段殘差 = 濁度_raw - 平滑基線
      5. 過原點最小平方擬合:殘差 = k * lux
         k = Σ(lux·res) / Σ(lux²)

    參數:
      baseline_window: OFF 基線滾動視窗大小(預設 30min — 假設攪拌間隔 < 視窗)

    回傳:(k, n_used) 或 (None, 0) 樣本不足時。
    """
    if '光照(lx)' not in df.columns or '濁度(NTU)' not in df.columns:
        return None, 0
    if 'light_state' not in df.columns:
        return None, 0

    # OFF 基線(濁度 > 0 且 OFF 狀態,去重)
    off_mask = (df['light_state'] == 'OFF') & (df['濁度(NTU)'] > 0) & df['濁度(NTU)'].notna()
    df_off = df.loc[off_mask, ['時間', '濁度(NTU)']].dropna()
    df_off = df_off.groupby('時間', as_index=False).mean()
    if len(df_off) < 30:
        return None, 0

    off_series = df_off.set_index('時間')['濁度(NTU)'].sort_index()
    # 滾動中位數壓掉攪拌尖刺(min_periods 小一點避免邊緣 NaN)
    off_series = off_series.rolling(baseline_window, center=True, min_periods=3).median()
    off_series = off_series.dropna()
    if len(off_series) < 30:
        return None, 0

    # ON 時段殘差(濁度 > 0 且 ON 狀態,去重)
    on_mask = (df['light_state'] == 'ON') & (df['濁度(NTU)'] > 0) & df['濁度(NTU)'].notna()
    df_on = df.loc[on_mask, ['時間', '濁度(NTU)', '光照(lx)']].dropna()
    df_on = df_on.groupby('時間', as_index=False).mean()
    if len(df_on) < 30:
        return None, 0

    # 把 OFF 基線插值到 ON 時間點上
    union_idx = off_series.index.union(pd.DatetimeIndex(df_on['時間']))
    baseline_full = off_series.reindex(union_idx).sort_index()
    baseline_full = baseline_full.interpolate(method='time')
    baseline_on = baseline_full.reindex(pd.DatetimeIndex(df_on['時間']))

    df_on = df_on.set_index('時間')
    df_on['baseline'] = baseline_on.values
    df_on['residual'] = df_on['濁度(NTU)'] - df_on['baseline']
    df_on = df_on.dropna()

    if len(df_on) < 30:
        return None, 0

    lux = df_on['光照(lx)'].values.astype(float)
    res = df_on['residual'].values.astype(float)

    denom = float(np.sum(lux * lux))
    if denom == 0:
        return None, 0

    k = float(np.sum(lux * res) / denom)
    return k, len(df_on)


def clean_dataframe(df, lux_k=None):
    """資料清理 — 光照回歸校正 + 濁度修補。

    規則:
      1. 加 light_state 欄(OFF / ON / MID)
      2. 估光照係數 k(訓練時),或用傳入的 k(推論時)
      3. 濁度校正:濁度_cleaned = 濁度_raw - k * lux,clip 到 >= 0
      4. 濁度 == 0 / NaN / MID 整段 → NaN(這些點不可信)
      5. 殘存 NaN 用時間插值補回(連 OFF/ON 校正後的真實值)

    參數:
      lux_k: None 表示自動估;float 則直接套用(讓 dashboard 用訓練好的 k)

    回傳:複製過的 df,多 'light_state'、'濁度_cleaned'、'lux_k_used' 三欄。
    """
    df = df.copy()
    df['時間'] = pd.to_datetime(df['時間'], errors='coerce')
    df = df.dropna(subset=['時間']).sort_values('時間').reset_index(drop=True)

    # 光照狀態分類
    df['光照(lx)'] = pd.to_numeric(df['光照(lx)'], errors='coerce')
    df['light_state'] = df['光照(lx)'].apply(classify_light_state)

    # 處理濁度
    df['濁度(NTU)'] = pd.to_numeric(df['濁度(NTU)'], errors='coerce')

    # 估或套用光照係數
    if lux_k is None:
        k, n_used = estimate_lux_coefficient(df)
        if k is None:
            k = 0.0
            print(f"  [警告] OFF/ON 樣本不足,無法估光照係數,跳過校正")
        else:
            print(f"  光照校正係數 k = {k:.5f}(用 {n_used:,} 筆 ON 點擬合)")
            print(f"  → 開燈 lux=2000 時,濁度讀值被偏移 {k*2000:+.2f} NTU,會被扣回去")
    else:
        k = float(lux_k)

    df['lux_k_used'] = k

    # 光照回歸校正:濁度_cleaned = 濁度_raw - k * lux
    lux_filled = df['光照(lx)'].fillna(0)
    corrected = df['濁度(NTU)'] - k * lux_filled

    # 標壞點 → NaN(這些點即使校正了也不可信)
    bad_mask = (
        (df['濁度(NTU)'] == 0)
        | df['濁度(NTU)'].isna()
        | (df['light_state'] == 'MID')
    )
    df['濁度_cleaned'] = corrected.where(~bad_mask, np.nan)

    # 濁度物理上 >= 0
    df['濁度_cleaned'] = df['濁度_cleaned'].clip(lower=0)

    # 殘存 NaN 用時間插值補
    df_idx = df.set_index('時間')
    df_idx['濁度_cleaned'] = df_idx['濁度_cleaned'].interpolate(method='time').ffill().bfill()
    df = df_idx.reset_index()

    return df


def load_csv(path):
    """讀單一 CSV"""
    df = pd.read_csv(path, encoding='utf-8-sig')
    df['時間'] = pd.to_datetime(df['時間'], errors='coerce')
    df = df.dropna(subset=['時間']).sort_values('時間').reset_index(drop=True)
    return df


def load_and_combine(paths):
    """讀多個 CSV 合併成一個"""
    dfs = []
    for p in paths:
        df = load_csv(p)
        df['_source'] = Path(p).stem
        dfs.append(df)
        print(f"  讀入 {Path(p).name}:{len(df)} 筆")
    return pd.concat(dfs, ignore_index=True)


def prepare_features(df):
    """準備模型 input:轉數字 + 過濾無資料代碼 + 缺值補中位數"""
    cols = [c for c in SENSORS if c in df.columns]
    X = df[cols].apply(pd.to_numeric, errors='coerce')
    X = X.where(~X.isin(list(NO_DATA_CODES)))
    X = X.fillna(X.median())
    return X, cols


def train_anomaly(csv_paths, contamination=0.05):
    """訓練 IsolationForest 異常檢測模型(用清理過的資料)"""
    print("=== 訓練異常檢測模型(IsolationForest)===\n")
    print("資料來源:")
    df = load_and_combine(csv_paths)

    # 資料清理(訓練模式:自動估光照係數 k)
    df = clean_dataframe(df)
    lux_k = float(df['lux_k_used'].iloc[0]) if 'lux_k_used' in df.columns and len(df) > 0 else 0.0
    n_total = len(df)

    # 排除 MID 時段(感測器掉了的整段不可信)
    df_train = df[df['light_state'] != 'MID'].copy()
    n_mid_excluded = n_total - len(df_train)
    print(f"\n資料清理:")
    print(f"  總筆數          : {n_total:,}")
    print(f"  排除 MID 時段   : {n_mid_excluded:,}(光照感測器中間值,疑似掉落)")
    print(f"  剩下訓練樣本    : {len(df_train):,}")

    # 把原始濁度替換為清理過的(去掉 0 值並插值)
    df_train['濁度(NTU)'] = df_train['濁度_cleaned']

    X, cols = prepare_features(df_train)
    print(f"\n訓練資料維度:{len(X):,} 筆 × {len(cols)} 維")
    print(f"特徵欄位:{cols}\n")

    # 標準化(IsolationForest 對 scale 敏感)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # 訓練
    model = IsolationForest(
        contamination=contamination,
        random_state=42,
        n_estimators=100,
        n_jobs=-1,
    )
    model.fit(X_scaled)

    # 訓練集自評
    scores = model.score_samples(X_scaled)
    preds = model.predict(X_scaled)
    n_anomaly = int((preds == -1).sum())

    print(f"訓練結果:")
    print(f"  contamination = {contamination}(假設 {contamination*100:.0f}% 是異常)")
    print(f"  訓練集標為異常:{n_anomaly:,} / {len(X):,}({n_anomaly/len(X)*100:.1f}%)")
    print(f"  分數分布:min={scores.min():.3f}  median={np.median(scores):.3f}  max={scores.max():.3f}")
    print(f"  (分數越低越異常,正常範圍通常 > -0.2)")

    # 儲存
    artifact = {
        'model': model,
        'scaler': scaler,
        'cols': cols,
        'sensors': SENSORS,
        'trained_at': datetime.now().isoformat(timespec='seconds'),
        'n_samples': len(X),
        'sources': [Path(p).name for p in csv_paths],
        'contamination': contamination,
        'lux_k': lux_k,
    }
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump(artifact, f)
    print(f"\n[OK] 模型已存:{MODEL_PATH}")


def check_anomaly(csv_path, recent_hours=1):
    """用已訓練模型檢查最近 N 小時資料"""
    if not MODEL_PATH.exists():
        print(f"❌ 找不到模型 {MODEL_PATH},請先跑 train")
        return

    with open(MODEL_PATH, 'rb') as f:
        artifact = pickle.load(f)

    print(f"=== 異常檢測 — 最近 {recent_hours} 小時 ===")
    print(f"模型訓練於:{artifact['trained_at']}")
    print(f"訓練樣本:{artifact['n_samples']:,}\n")

    df = load_csv(csv_path)
    cutoff = df['時間'].max() - timedelta(hours=recent_hours)
    df_recent = df[df['時間'] >= cutoff].copy()

    if df_recent.empty:
        print(f"❌ 最近 {recent_hours} 小時無資料")
        return

    model = artifact['model']
    scaler = artifact['scaler']
    cols = artifact['cols']

    # 準備特徵
    X = df_recent[cols].apply(pd.to_numeric, errors='coerce')
    X = X.where(~X.isin(list(NO_DATA_CODES)))
    X_imputed = X.fillna(X.median())
    # 傳 numpy array 避免 sklearn 抱怨 feature names mismatch
    X_scaled = scaler.transform(X_imputed.values)

    scores = model.score_samples(X_scaled)
    preds = model.predict(X_scaled)
    n_anomaly = int((preds == -1).sum())

    print(f"檢查筆數:{len(df_recent):,}")
    print(f"判定異常:{n_anomaly:,}({n_anomaly/len(df_recent)*100:.1f}%)")
    print(f"分數範圍:{scores.min():.3f} ~ {scores.max():.3f}\n")

    if n_anomaly == 0:
        print("[OK] 全部正常")
        return

    # 找最異常的那筆
    worst_idx = int(np.argmin(scores))
    worst_row = df_recent.iloc[worst_idx]
    worst_x = X_imputed.iloc[worst_idx].values
    worst_scaled = scaler.transform(worst_x.reshape(1, -1))[0]

    print(f"⚠ 最異常的時間:{worst_row['時間']}")
    print(f"  異常分數:{scores[worst_idx]:.3f}\n")

    # 最可疑的 3 個感測器(Z-score 絕對值最大)
    deviations = np.abs(worst_scaled)
    idx_sorted = np.argsort(deviations)[::-1][:3]
    print(f"  最可疑的 3 個感測器:")
    for i in idx_sorted:
        z = worst_scaled[i]
        sign = "偏高" if z > 0 else "偏低"
        print(f"    {cols[i]} = {worst_x[i]:.2f}  Z={z:+.2f}({sign})")


def forecast(csv_path, hours_ahead=24, sensor=None):
    """預測未來 N 小時各感測器趨勢(線性外推)"""
    df = load_csv(csv_path)
    sensors_to_forecast = [sensor] if sensor else SENSORS

    print(f"=== 趨勢預測 — 未來 {hours_ahead} 小時 ===")
    print(f"資料來源:{Path(csv_path).name}")
    print(f"分析方法:最後 24 小時線性回歸外推\n")

    cutoff = df['時間'].max() - timedelta(hours=24)
    df_recent = df[df['時間'] >= cutoff]

    if df_recent.empty or len(df_recent) < 10:
        print("❌ 最近 24 小時資料太少,無法預測")
        return

    print(f"{'感測器':<20} {'目前':>10} {'/hr 速率':>12} {f'{hours_ahead}h 後':>12} {'變化':>10}  趨勢")
    print("-" * 80)

    for s in sensors_to_forecast:
        if s not in df.columns:
            continue
        series = pd.to_numeric(df_recent[s], errors='coerce')
        series = series[~series.isin(list(NO_DATA_CODES))].dropna()

        if len(series) < 10:
            print(f"{s:<20} 有效資料 < 10 筆,跳過")
            continue

        t = np.arange(len(series))
        y = series.values
        slope, intercept = np.polyfit(t, y, 1)

        rate_per_hour = slope * (len(series) / 24)
        current = float(y[-1])
        end_value = float(slope * (len(series) + hours_ahead * len(series) / 24) + intercept)
        change = end_value - current

        if abs(rate_per_hour) < 0.001:
            arrow = "→ 持平"
        elif rate_per_hour > 0:
            arrow = "↗ 上升"
        else:
            arrow = "↘ 下降"

        print(f"{s:<20} {current:>10.2f} {rate_per_hour:>+12.4f} {end_value:>12.2f} {change:>+10.2f}  {arrow}")


def usage():
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        usage()

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == 'train':
        train_anomaly(args)
    elif cmd == 'check':
        recent_hours = 1
        # 簡單 parse --hours N
        if '--hours' in args:
            i = args.index('--hours')
            recent_hours = int(args[i+1])
            args = args[:i] + args[i+2:]
        check_anomaly(args[0], recent_hours=recent_hours)
    elif cmd == 'forecast':
        hours_ahead = 24
        if '--hours' in args:
            i = args.index('--hours')
            hours_ahead = int(args[i+1])
            args = args[:i] + args[i+2:]
        forecast(args[0], hours_ahead=hours_ahead)
    else:
        print(f"❌ 未知指令:{cmd}\n")
        usage()
