import yfinance as yf
import pandas as pd
import twstock
import time
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os

# ===========================
# 1. 設定區 (User Configuration)
# ===========================

# Google Sheet 網址 (👇 請換成你的網址)
SHEET_URL = 'https://docs.google.com/spreadsheets/d/1mvC4i7Pw7uxS-OV5bav0uhvb6tAvRufTataFzwQQ2Ic/edit?usp=sharing'
SHEET_NAME = 'rsi_scanner'  # 我們存入這個分頁

# 篩選門檻
MIN_PRICE = 10
MIN_VOLUME_SHEETS = 3000

# 建立自動抓鑰匙的路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_FILE = os.path.join(BASE_DIR, 'service_account.json')

# ===========================
# 2. 技術指標計算 (內建公式版，免安裝 pandas-ta)
# ===========================
def calculate_sma(series, length):
    """計算簡單移動平均 (SMA)"""
    return series.rolling(window=length).mean()

def calculate_rsi(series, length=100):
    """計算 RSI (使用 Wilder's Smoothing，與 TradingView 算法一致)"""
    delta = series.diff()
    
    # 分別取出 漲幅(up) 和 跌幅(down)
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    
    # 使用指數移動平均 (alpha=1/length) 模擬 Wilder's Smoothing
    ma_up = up.ewm(alpha=1/length, adjust=False).mean()
    ma_down = down.ewm(alpha=1/length, adjust=False).mean()
    
    rs = ma_up / ma_down
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ===========================
# 3. Google Sheet 存檔
# ===========================
def update_rolling_data(new_data_list):
    print("正在連線 Google Sheet 更新資料...")
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_url(SHEET_URL)
        
        try:
            ws = sheet.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title=SHEET_NAME, rows="1000", cols="5")
            ws.append_row(["日期", "股票代號", "股票名稱"])

        all_rows = ws.get_all_values()
        if len(all_rows) <= 1:
            header = ["日期", "股票代號", "股票名稱"]
            existing_data = []
        else:
            header = all_rows[0]
            existing_data = all_rows[1:]

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        today_rows = []
        
        for stock in new_data_list:
            row = [today_str, stock['ticker'], stock['name']]
            today_rows.append(row)

        clean_history = [row for row in existing_data if row[0] != today_str]
        final_data = clean_history + today_rows
        
        unique_dates = sorted(list(set([row[0] for row in final_data])), reverse=True)
        if len(unique_dates) > 3:
            keep_dates = unique_dates[:3]
            final_data = [row for row in final_data if row[0] in keep_dates]
        
        ws.clear()
        ws.append_row(header)
        if final_data:
            ws.append_rows(final_data)
        print("✅ 更新完成！")

    except Exception as e:
        print(f"❌ 存檔失敗: {e}")

# ===========================
# 4. 股票掃描邏輯
# ===========================
def get_all_tickers():
    print("正在取得全台股清單...")
    all_stocks = []
    for code, info in twstock.codes.items():
        if info.type == "股票":
            if info.market == "上市":
                all_stocks.append(code + ".TW")
            elif info.market == "上櫃":
                all_stocks.append(code + ".TWO")
    return all_stocks

def check_stock(ticker):
    print(f"\r[{ticker}] ", end="", flush=True)
    try:
        # 下載數據
        df = yf.download(ticker, period="2y", interval="1d", progress=False)
        if len(df) < 300: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)

        today = df.iloc[-1]
        prev  = df.iloc[-2]
        
        # 門檻過濾
        if today['Close'] < MIN_PRICE: return None
        vol_sheets = today['Volume'] / 1000
        if vol_sheets < MIN_VOLUME_SHEETS: return None

        # === 這裡改用內建函數計算，不依賴外部套件 ===
        # 計算 RSI 100
        df['RSI'] = calculate_rsi(df['Close'], length=100)
        # 計算 RSI 的 SMA 200
        df['RSI_SMA'] = calculate_sma(df['RSI'], length=200)

        # 計算價格均線
        df['MA20']  = calculate_sma(df['Close'], length=20)
        df['MA60']  = calculate_sma(df['Close'], length=60)
        df['MA120'] = calculate_sma(df['Close'], length=120)
        df['MA240'] = calculate_sma(df['Close'], length=240)
        
        # 重新抓取含指標的當日數據
        today = df.iloc[-1]
        prev  = df.iloc[-2]

        # 策略條件
        cond_rsi = (today['RSI'] > today['RSI_SMA'])
        
        above_all_now = (
            today['Close'] > today['MA20'] and 
            today['Close'] > today['MA60'] and 
            today['Close'] > today['MA120'] and 
            today['Close'] > today['MA240']
        )
        
        above_all_prev = (
            prev['Close'] > prev['MA20'] and 
            prev['Close'] > prev['MA60'] and 
            prev['Close'] > prev['MA120'] and 
            prev['Close'] > prev['MA240']
        )
        
        cond_first_day = above_all_now and (not above_all_prev)

        if cond_rsi and cond_first_day:
            stock_code = ticker.split('.')[0]
            stock_name = ticker
            if stock_code in twstock.codes:
                stock_name = twstock.codes[stock_code].name
            
            return {"ticker": stock_code, "name": stock_name}
        return None
    except Exception:
        return None

# ===========================
# 5. 主程式
# ===========================
if __name__ == "__main__":
    # 在 GitHub 雲端上，我們會透過 secret 產生 json，所以這裡直接檢查
    # 如果是在本地跑，請確保 service_account.json 存在
    if not os.path.exists(JSON_FILE):
        print(f"❌ 找不到鑰匙: {JSON_FILE}")
        # 在 GitHub Actions 裡這一步不會失敗，因為我們會自動產生
        exit()

    tickers = get_all_tickers()
    target_list = tickers # 跑全部
    
    found_stocks = []
    print(f"=== 開始掃描 (篩選: ${MIN_PRICE}, 量{MIN_VOLUME_SHEETS}張) ===")
    
    for i, stock_id in enumerate(target_list):
        # 顯示簡易進度
        if i % 10 == 0:
            print(f".", end="", flush=True) 
            
        res = check_stock(stock_id)
        if res:
            print(f"\n✨ 發現: {res['name']} ({res['ticker']})")
            found_stocks.append(res)
        
        time.sleep(0.5)

    print("\n" + "="*30)
    print(f"🎉 掃描完成！共找到 {len(found_stocks)} 檔。")
    
    if found_stocks:
        update_rolling_data(found_stocks)
    else:
        print("今日無符合條件股票。")
