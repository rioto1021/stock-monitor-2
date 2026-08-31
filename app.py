from flask import Flask, render_template_string, request, redirect, url_for
import pandas as pd
import numpy as np
import yfinance as yf
import time
import requests
import threading
import os
import ta  # pandas_ta の代わりに安定版 ta を使用

app = Flask(__name__)

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1543911651118415952/Uqv8CRJru__uscdMGXG_rAIHHXqOCMTTpgUjuhc9Pr852h635HUL1QWDi_5pfjkleL9a"

monitor_status = {
    "is_running": False,
    "tickers": ["285A.T", "6981.T"],
    "last_processed_times": {},
    "logs": []
}

company_names_cache = {}

def add_log(message):
    monitor_status["logs"].insert(0, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}")
    if len(monitor_status["logs"]) > 50:
        monitor_status["logs"].pop()

def send_discord_message(message):
    payload = {"content": message}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        response.raise_for_status()
    except Exception as e:
        add_log(f"Discord 送信エラー: {e}")

def get_company_name(ticker_symbol):
    if ticker_symbol in company_names_cache:
        return company_names_cache[ticker_symbol]
    try:
        ticker_obj = yf.Ticker(ticker_symbol)
        info = ticker_obj.info
        name = info.get('shortName') or info.get('longName') or ticker_symbol
        company_names_cache[ticker_symbol] = name
        return name
    except Exception as e:
        add_log(f"[{ticker_symbol}] 企業名の取得に失敗しました: {e}")
        return ticker_symbol

def get_30m_data(ticker_symbol):
    # 期間を少し長め(1mo)に取って計算に必要なデータを十分に確保
    df = yf.download(ticker_symbol, period="1mo", interval="30m", progress=False)
    
    if df.empty or len(df) < 10:
        return None
        
    # MultiIndex の完全なフラット化
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 欠損値（NaN）の補填処理を入れる（これがPSAR破綻を防ぐキー）
    df = df.ffill().bfill()

    # 1次元の Series として抽出
    high_s = df['High'].squeeze()
    low_s = df['Low'].squeeze()
    close_s = df['Close'].squeeze()

    # --- PSAR の計算 ---
    psar_indicator = ta.trend.PSARIndicator(
        high=high_s, 
        low=low_s, 
        close=close_s, 
        step=0.02, 
        max_step=0.2
    )
    
    # トレンドごとのドット（SAR値）を取得
    df['PSAR_down'] = psar_indicator.psar_down() # 上昇トレンド時のサポートSAR（価格の下）
    df['PSAR_up'] = psar_indicator.psar_up()     # 下降トレンド時のレジスタンスSAR（価格の上）
    df['PSAR_indicator'] = psar_indicator.psar() # 全体のSAR値

    # --- MACD の計算 ---
    macd_indicator = ta.trend.MACD(close=close_s, window_slow=26, window_fast=12, window_sign=9)
    df['MACD_12_26_9'] = macd_indicator.macd()
    df['MACDs_12_26_9'] = macd_indicator.macd_signal()
    df['MACDh_12_26_9'] = macd_indicator.macd_diff()

    return df

def monitor_loop():
    ticker_list_str = ", ".join(monitor_status["tickers"])
    add_log(f"監視を開始しました。（対象銘柄: {ticker_list_str}）")
    
    while monitor_status["is_running"]:
        for ticker in monitor_status["tickers"]:
            if not monitor_status["is_running"]:
                break
                
            try:
                df_30m = get_30m_data(ticker)
                
                if df_30m is not None and not df_30m.empty:
                    latest_time = df_30m.index[-1]
                    latest_row = df_30m.iloc[-1]
                    
                    last_time = monitor_status["last_processed_times"].get(ticker)

                if last_time != latest_time:
                        close_price = float(latest_row['Close'])
                        
                        psar_down_val = latest_row.get('PSAR_down', np.nan)
                        psar_up_val = latest_row.get('PSAR_up', np.nan)
                        psar_general = latest_row.get('PSAR_indicator', np.nan)

                        # PSAR_downに値があれば上昇トレンド（価格の下にSAR）
                        if not np.isnan(psar_down_val) and psar_down_val > 0:
                            sar_value = float(psar_down_val)
                            trend = "上昇 (Long)"
                        # PSAR_upに値があれば下降トレンド（価格の上にSAR）
                        elif not np.isnan(psar_up_val) and psar_up_val > 0:
                            sar_value = float(psar_up_val)
                            trend = "下降 (Short)"
                        # 万が一両方NaNだが全体値がある場合
                        elif not np.isnan(psar_general) and psar_general > 0:
                            sar_value = float(psar_general)
                            trend = "上昇 (Long)" if close_price >= sar_value else "下降 (Short)"
                        else:
                            # それでも取得できない場合のみログを出して前回値を維持
                            sar_value = 0.0
                            trend = "計算エラー"

                        macd_line = latest_row.get('MACD_12_26_9', np.nan)
                        macd_signal = latest_row.get('MACDs_12_26_9', np.nan)
                        macd_hist = latest_row.get('MACDh_12_26_9', np.nan)
                        macd_trend = "強気 (Bullish)" if macd_line > macd_signal else "弱気 (Bearish)"

                        company_name = get_company_name(ticker)

                        msg = (
                            f"**【新規データ更新】{company_name}** (`{ticker}`)\n"
                            f"⏱ 日時: {latest_time}\n"
                            f"・終値: {close_price:.1f}\n"
                            f"・SAR: {sar_value:.1f} ({trend})\n"
                            f"・MACD: {macd_line:.2f} / Signal: {macd_signal:.2f} ({macd_trend})\n"
                            f"・Histogram: {macd_hist:.2f}"
                        )
                        
                        send_discord_message(msg)
                        add_log(f"[{ticker}] 新規更新・通知完了 ({latest_time})")
                        
                        monitor_status["last_processed_times"][ticker] = latest_time
                        
            except Exception as e:
                add_log(f"[{ticker}] 監視処理エラー: {e}")
                
        time.sleep(30)
        
    add_log("監視を停止しました。")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <title>複数銘柄監視コントロールパネル</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 30px; background-color: #f4f6f9; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 650px; margin-bottom: 20px; }
        .status { font-weight: bold; padding: 5px 10px; border-radius: 4px; display: inline-block; }
        .running { background-color: #d4edda; color: #155724; }
        .stopped { background-color: #f8d7da; color: #721c24; }
        input[type="text"] { padding: 8px; font-size: 14px; width: 90%; }
        button { padding: 8px 16px; font-size: 14px; cursor: pointer; border: none; border-radius: 4px; }
        .btn-start { background-color: #28a745; color: white; }
        .btn-stop { background-color: #dc3545; color: white; }
        .log-box { background: #1e1e1e; color: #00ff00; padding: 15px; font-family: monospace; height: 250px; overflow-y: scroll; border-radius: 5px; }
        .help-text { font-size: 12px; color: #666; margin-top: 4px; }
    </style>
</head>
<body>
    <h2>📈 複数銘柄監視コントロールパネル</h2>
    <div class="card">
        <p>現在のステータス: 
            {% if status.is_running %}
                <span class="status running">監視中 (対象: {{ status.tickers | join(', ') }})</span>
            {% else %}
                <span class="status stopped">停止中</span>
            {% endif %}
        </p>
        <form action="/action" method="POST">
            <p>
                <label><b>監視対象銘柄コード (カンマ区切り):</b></label><br>
                <input type="text" name="tickers" value="{{ status.tickers | join(', ') }}" {% if status.is_running %}disabled{% endif %} required>
                <div class="help-text">例: 285A.T, AAPL, 7203.T, NVDA</div>
            </p>
            <div>
                {% if not status.is_running %}
                    <button type="submit" name="btn_action" value="start" class="btn-start">全銘柄の監視を開始</button>
                {% else %}
                    <button type="submit" name="btn_action" value="stop" class="btn-stop">監視を停止</button>
                {% endif %}
            </div>
        </form>
    </div>
    <div class="card" style="max-width: 850px;">
        <h3>実行ログ</h3>
        <div class="log-box">
            {% for log in status.logs %}
                <div>{{ log }}</div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, status=monitor_status)

@app.route("/action", methods=["POST"])
def handle_action():
    action = request.form.get("btn_action")
    if action == "start" and not monitor_status["is_running"]:
        raw_tickers = request.form.get("tickers", "")
        ticker_list = [t.strip() for t in raw_tickers.split(",") if t.strip()]
        if ticker_list:
            monitor_status["tickers"] = ticker_list
            monitor_status["is_running"] = True
            monitor_status["last_processed_times"] = {}
            thread = threading.Thread(target=monitor_loop, daemon=True)
            thread.start()
    elif action == "stop" and monitor_status["is_running"]:
        monitor_status["is_running"] = False
    return redirect(url_for("index"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)