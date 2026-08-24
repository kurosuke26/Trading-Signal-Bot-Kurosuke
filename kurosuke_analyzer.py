#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import yfinance as yf
from datetime import datetime
import requests

DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')

def get_stock_data(tickers):
    """Yahoo! Finance からデータを取得"""
    print("取得開始...")
    data = {}
    
    for ticker in tickers:
        try:
            print(f"  取得中：{ticker}...", end=" ")
            stock = yf.Ticker(ticker)
            info = stock.info
            
            data[ticker] = {
                'name': info.get('longName', 'N/A'),
                'current_price': info.get('currentPrice', 0),
                'dividend_yield': info.get('dividendYield', 0) * 100 if info.get('dividendYield') else 0,
                'pbr': info.get('priceToBook', 0),
                'per': info.get('trailingPE', 0),
            }
            print("OK")
        except Exception as e:
            print(f"Error: {str(e)}")
    
    return data

def send_discord_message(message):
    """Discord に送信"""
    if not DISCORD_WEBHOOK_URL:
        print("Discord Webhook URL が設定されていません")
        return False
    
    try:
        payload = {'content': message}
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        
        if response.status_code == 204:
            print("Discord メッセージを送信しました")
            return True
        else:
            print(f"送信失敗：{response.status_code}")
            return False
    except Exception as e:
        print(f"Error: {str(e)}")
        return False

def analyze_stocks():
    """分析実行"""
    tickers = ['6758.T', '7203.T', '9984.T', '6861.T', '8306.T']
    
    print("=" * 80)
    print("Kurosuke 割安チェッカー - 自動分析")
    print(f"実行時刻：{datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}")
    print("=" * 80)
    
    # データ取得
    stock_data = get_stock_data(tickers)
    
    if not stock_data:
        print("データ取得に失敗しました")
        return False
    
    # Discord メッセージ作成
    message = "Kurosuke割安チェッカー - 本日の分析\n"
    message += f"実行時刻：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n\n"
    
    for ticker, data in stock_data.items():
        message += f"{ticker}（{data['name']}）\n"
        message += f"株価：¥{data['current_price']:,.0f}\n"
        message += f"配当利回り：{data['dividend_yield']:.2f}%\n"
        message += f"PBR：{data['pbr']:.2f}\n"
        message += f"PER：{data['per']:.1f}\n\n"
    
    print("\nDiscord に投稿中...")
    success = send_discord_message(message)
    
    if success:
        print("分析完了！")
        return True
    else:
        print("分析は完了しましたが、投稿に失敗しました")
        return False

if __name__ == "__main__":
    try:
        success = analyze_stocks()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"Error: {str(e)}")
        sys.exit(1)