import yfinance as yf
import pandas as pd
import pandas_ta as ta
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

# 篩選門檻 (雖然不存，但還是要過濾掉爛股)
MIN_PRICE = 10
MIN_VOLUME_SHEETS = 500

# 自動抓取鑰匙路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_FILE = os.path.join(BASE_DIR, 'service_account.json')

# ===========================
# 2. Google Sheet 存檔 (只存 日期/代號/名稱)
# ===========================
def update_rolling_data(new_data_list):
    print("正在連線 Google Sheet 更新資料...")
    
    try:
        # 連線設定
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_url(SHEET_URL)
        
        # 取得或建立分頁
        try:
            ws = sheet.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title=SHEET_NAME, rows="1000", cols="5")
            ws.append_row(["日期", "股票代號", "股票名稱"]) # 新標題

        # 1. 讀取舊資料
        all_rows = ws.get_all_values()
        
        if len(all_rows) <= 1:
            header = ["日期", "股票代號", "股票名稱"]
            existing_data = []
        else:
            header = all_rows[0]
            existing_data = all_rows[1:]

        # 2. 準備今天的新資料
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        today_rows = []
        
        for stock in new_data_list:
            # 這裡只存 User 指定的三個欄位
            row = [
                today_str,
                stock['ticker'],
                stock['name']  # 中文名稱
            ]
            today_rows.append(row)

        # 3. 排除舊資料裡「今天」的紀錄 (避免重複跑導致重複存)
        clean_history = [row for row in existing_data if row[0] != today_str]
        
        # 4. 合併
        final_data = clean_history + today_rows
        
        # 5. 滾動刪除 (只留最近 3 天)
        unique_dates = sorted(list(set([row[0] for row in final_data])), reverse=True)
        
        if len(unique_dates) > 3:
            keep_dates = unique_dates[:3]
            print(f"⚠️ 資料超過 3 天，將只保留: {keep_dates}")
            final_data = [row for row in final_data if row[0] in keep_dates]
        
        # 6. 寫回
        print("正在寫回 Google Sheet...")
        ws.clear()
        ws.append_row(header)
        if final_data:
            ws.append_rows(final_data)
            
        print("✅ 更新完成！")

    except Exception as e:
        print(f"❌ 存檔失敗: {e}")

# ===========================
# 3. 股票掃描 (增加抓取中文名稱)
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
    print(f"清單取得完成！總共 {len(all_stocks)} 檔。")
    return all_stocks

def check_stock(ticker):
    print(f"\r[{ticker}] 檢查中...", end="", flush=True)
    try:
        df = yf.download(ticker, period="2y", interval="1d", progress=False)
        if len(df) < 300: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)

        today = df.iloc[-1]
        prev  = df.iloc[-2]
        
        # 門檻過濾
        if today['Close'] < MIN_PRICE: return None
        vol_sheets = today['Volume'] / 1000
        if vol_sheets < MIN_VOLUME_SHEETS: return None

        # 指標計算
        df['RSI'] = ta.rsi(df['Close'], length=100)
        df['RSI_SMA'] = ta.sma(df['RSI'], length=200)
        df['MA20']  = ta.sma(df['Close'], length=20)
        df['MA60']  = ta.sma(df['Close'], length=60)
        df['MA120'] = ta.sma(df['Close'], length=120)
        df['MA240'] = ta.sma(df['Close'], length=240)
        
        today = df.iloc[-1]
        prev  = df.iloc[-2]

        cond_rsi = (today['RSI'] > today['RSI_SMA'])
        above_all_now = (today['Close'] > today['MA20'] and today['Close'] > today['MA60'] and today['Close'] > today['MA120'] and today['Close'] > today['MA240'])
        above_all_prev = (prev['Close'] > prev['MA20'] and prev['Close'] > prev['MA60'] and prev['Close'] > prev['MA120'] and prev['Close'] > prev['MA240'])
        cond_first_day = above_all_now and (not above_all_prev)

        if cond_rsi and cond_first_day:
            # === 取得中文名稱 ===
            stock_code = ticker.split('.')[0] # 把 2330.TW 變成 2330
            stock_name = ticker # 預設名稱為代號 (萬一找不到)
            
            if stock_code in twstock.codes:
                stock_name = twstock.codes[stock_code].name
            
            return {
                "ticker": ticker,
                "name": stock_name
            }
        return None
    except Exception:
        return None

# ===========================
# 4. 主程式
# ===========================
if __name__ == "__main__":
    if not os.path.exists(JSON_FILE):
        print(f"❌ 找不到鑰匙: {JSON_FILE}")
        exit()

    tickers = get_all_tickers()
    target_list = tickers[:50] # 跑前 50 檔

    found_stocks = []
    
    print(f"=== 開始掃描 (篩選: ${MIN_PRICE}, 量{MIN_VOLUME_SHEETS}張) ===")
    
    for i, stock_id in enumerate(target_list):
        progress = (i + 1) / len(target_list) * 100
        print(f"\r進度: {progress:.1f}% ({i+1}/{len(target_list)}) 找到: {len(found_stocks)} 檔", end="")
        
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