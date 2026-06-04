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


def clean_dataframe(df):
    """資料清理 — 處理光照狀態 + 濁度修補。

    規則:
      1. 加入 light_state 欄(OFF / ON / MID)
      2. 濁度 == 0 → NaN(使用者規則:濁度不可能是 0)
      3. light_state == MID 的整列濁度 → NaN(感測器掉了該時段全可疑)
      4. 用時間插值補回濁度的 NaN(用相鄰 OFF 時段的真實值連起來)

    回傳:複製過的 df,多 'light_state' 跟 '濁度_cleaned' 兩欄。
    """
    df = df.copy()
    df['時間'] = pd.to_datetime(df['時間'], errors='coerce')
    df = df.dropna(subset=['時間']).sort_values('時間').reset_index(drop=True)

    # 光照狀態分類
    df['光照(lx)'] = pd.to_numeric(df['光照(lx)'], errors='coerce')
    df['light_state'] = df['光照(lx)'].apply(classify_light_state)

    # 處理濁度
    df['濁度(NTU)'] = pd.to_numeric(df['濁度(NTU)'], errors='coerce')

    # 標記要清除的(NaN 化)
    bad_mask = (df['濁度(NTU)'] == 0) | (df['light_state'] == 'MID')
    df['濁度_cleaned'] = df['濁度(NTU)'].where(~bad_mask, np.nan)

    # 用時間做插值(連 OFF 時段真實值跨過 ON 假 0)
    df_idx = df.set_index('時間')
    df_idx['濁度_cleaned'] = df_idx['濁度_cleaned'].interpolate(method='time')
    # 前後若仍 NaN(資料最頭尾),前後填補
    df_idx['濁度_cleaned'] = df_idx['濁度_cleaned'].ffill().bfill()
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

    # 資料清理
    df = clean_dataframe(df)
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
