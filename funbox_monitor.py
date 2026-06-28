# -*- coding: utf-8 -*-
"""
Funbox 戰鬥陀螺新品 / 開賣 監控腳本
======================================

功能：
1. 定期抓取 https://shop.funbox.com.tw/categories/XI/KB
2. 解析頁面上每個商品的「名稱」與「價格」
3. 跟上一次抓到的結果比較，當發現：
   - 出現「全新的商品」(之前沒見過的名稱)，或
   - 原本顯示異常高價（如 NT$999999，代表尚未開賣）的商品，
     價格變成正常數字（代表開賣了）
   就會發送通知。

安裝需求（在終端機執行）：
    pip install requests beautifulsoup4

【重要】如果跑起來抓不到任何商品（看到「沒有找到 NT$」的警告），
代表這個網站的商品列表是用 JavaScript 動態載入的，
請改用瀏覽器渲染模式，另外安裝：
    pip install playwright
    playwright install chromium
然後把下面的 USE_BROWSER_RENDERING 改成 True。

這個腳本可以用三種模式執行（看你要本機長駐執行，還是放在雲端排程）：
    python funbox_monitor.py --test     測試一次，不發通知，只印出結果（第一次請先用這個）
    python funbox_monitor.py --once     檢查一次，有變化就發通知，然後結束（適合 GitHub Actions 等雲端排程）
    python funbox_monitor.py            不加任何參數＝長駐執行，自己每隔一段時間檢查一次（適合放在自己電腦上跑）
"""

import os
import re
import sys
import json
import time
import argparse
import smtplib
import ssl
from email.mime.text import MIMEText
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ========================= 基本設定 =========================

TARGET_URL = "https://shop.funbox.com.tw/categories/XI/KB"

CHECK_INTERVAL_SECONDS = 10 * 60  # 只有「長駐模式」會用到：建議至少 5~10 分鐘，避免太頻繁造成對方網站負擔

# 高於這個價格就視為「還沒開賣的佔位價格」（你提到的 99999，實際看到的是 999999）
PLACEHOLDER_PRICE_THRESHOLD = 100000

# 用來記錄上一次抓到的商品狀態，方便比對新舊
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "funbox_state.json")

# 是否改用瀏覽器渲染（Playwright）抓取頁面，預設用較輕量的 requests
USE_BROWSER_RENDERING = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# ----------------------- 通知方式設定 -----------------------
# 可以同時啟用多種，把想用的方式名稱放進這個 list 裡
# 例如 ["ntfy"]、["telegram"]、["ntfy", "telegram"]、["email"]
# 用 Telegram 的話，記得把 "telegram" 加進這個 list，不然設定了 token 也不會發送
NOTIFY_METHODS = ["ntfy", "telegram"]

EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "from_addr": "your_email@gmail.com",   # 寄件者 email
    "app_password": "your_app_password",    # Google「應用程式密碼」，不是登入密碼
    "to_addr": "your_email@gmail.com",      # 收件者 email（可跟寄件者相同）
}

# 下面這三個值，優先讀取「環境變數」（GitHub Actions 用 Secrets 設定時會用到），
# 如果沒有設環境變數，才會用後面 os.environ.get(...) 第二個參數裡寫的預設值。
# 本機執行的話，最簡單的方式就是直接把預設值那個字串改成你自己的值。

# ntfy.sh：完全免費、不用註冊帳號，手機裝 ntfy app 訂閱同一個 topic 名稱即可收到推播
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "your-unique-funbox-topic-name")  # 自己取一個夠獨特的名字，避免被別人猜到

# Telegram Bot：跟 @BotFather 對話建立 bot 拿到 token，
# 再跟你的 bot 對話一次，用 https://api.telegram.org/bot<token>/getUpdates 拿到 chat_id
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "your_telegram_bot_token")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "your_telegram_chat_id")

# ========================= 工具函式 =========================


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_text_requests():
    resp = requests.get(TARGET_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n")


def fetch_text_playwright():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="zh-TW")
        page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(2000)  # 多等一下，確保動態內容載入完成
        text = page.inner_text("body")
        browser.close()
        return text


def fetch_text():
    if USE_BROWSER_RENDERING:
        return fetch_text_playwright()
    return fetch_text_requests()


PRICE_RE = re.compile(r"NT\$\s*([\d,]+)")
SKIP_LINE_WORDS = {"加入購物車", "特價", "已售完", "補貨中", "缺貨", "查看商品", "詳情"}


def parse_products(text):
    """
    從頁面文字中找出 (商品名稱, 價格) 的清單。
    用「找到 NT$價格 的那一行，往上找最近一行看起來像名稱的文字」的方式解析，
    可以適應大部分電商版型，但無法保證 100% 準確，建議先用 TEST_MODE 確認。
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    products = []

    for idx, line in enumerate(lines):
        m = PRICE_RE.search(line)
        if not m:
            continue
        price = int(m.group(1).replace(",", ""))

        # 先試試看名稱跟價格是不是同一行（例如 "商品名稱 NT$295"）
        before = line[: m.start()].strip(" ·-:|")
        name = before if (before and not PRICE_RE.search(before)) else None

        # 同一行找不到名稱，就往上找最近一行可能是名稱的文字
        if not name:
            j = idx - 1
            while j >= 0:
                cand = lines[j]
                if cand in SKIP_LINE_WORDS or PRICE_RE.search(cand) or len(cand) < 2:
                    j -= 1
                    continue
                name = cand
                break

        if name:
            products.append((name.strip(" ·-:|"), price))

    return products


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ========================= 通知函式 =========================


def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_CONFIG["from_addr"]
    msg["To"] = EMAIL_CONFIG["to_addr"]
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(
            EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"], context=context
        ) as server:
            server.login(EMAIL_CONFIG["from_addr"], EMAIL_CONFIG["app_password"])
            server.sendmail(EMAIL_CONFIG["from_addr"], EMAIL_CONFIG["to_addr"], msg.as_string())
        print(f"[{now()}] ✅ Email 已送出")
    except Exception as e:
        print(f"[{now()}] ❌ Email 寄送失敗：{e}")


def send_ntfy(subject, body):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": subject.encode("utf-8")},
            timeout=10,
        )
        print(f"[{now()}] ✅ ntfy 通知已送出")
    except Exception as e:
        print(f"[{now()}] ❌ ntfy 通知失敗：{e}")


def send_telegram(body):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": body}, timeout=10)
        print(f"[{now()}] ✅ Telegram 通知已送出")
    except Exception as e:
        print(f"[{now()}] ❌ Telegram 通知失敗：{e}")


def send_notification(subject, body):
    if "email" in NOTIFY_METHODS:
        send_email(subject, body)
    if "ntfy" in NOTIFY_METHODS:
        send_ntfy(subject, body)
    if "telegram" in NOTIFY_METHODS:
        send_telegram(body)


# ========================= 主要檢查邏輯 =========================


def check_once(send_notify=True):
    text = fetch_text()

    if "NT$" not in text:
        print(
            f"[{now()}] ⚠️ 沒有在頁面中找到任何 'NT$' 價格文字。\n"
            f"   可能是這個網站的商品列表是用 JavaScript 動態載入的，\n"
            f"   請安裝 playwright（pip install playwright && playwright install chromium）\n"
            f"   並把 USE_BROWSER_RENDERING 改成 True 後再試一次。"
        )
        return []

    products = parse_products(text)
    print(f"[{now()}] 偵測到 {len(products)} 個商品：")
    for name, price in products:
        flag = "（佔位價格，尚未開賣）" if price >= PLACEHOLDER_PRICE_THRESHOLD else ""
        print(f"   - {name}：NT${price} {flag}")

    old_state = load_state()
    new_state = {}
    alerts = []

    for name, price in products:
        new_state[name] = price
        old_price = old_state.get(name)

        if old_price is None:
            # 全新出現的商品
            if price < PLACEHOLDER_PRICE_THRESHOLD:
                alerts.append(f"🆕 新商品上架！\n{name}\n價格：NT${price}")
            else:
                alerts.append(
                    f"👀 偵測到新的商品頁面（目前顯示 NT${price}，可能尚未開賣）：\n{name}"
                )
        elif old_price >= PLACEHOLDER_PRICE_THRESHOLD and price < PLACEHOLDER_PRICE_THRESHOLD:
            # 原本是佔位價格，現在變成正常價格 -> 開賣了
            alerts.append(
                f"🎉 商品開賣了！\n{name}\n現在價格：NT${price}（原本顯示 NT${old_price}）"
            )

    save_state(new_state)

    if alerts:
        body = "\n\n".join(alerts)
        if send_notify:
            send_notification("【Funbox 戰鬥陀螺】有新商品通知！", body)
        else:
            print(f"\n[{now()}] 🔔（測試模式，未實際發送通知）偵測到以下變化：\n{body}\n")
    else:
        print(f"[{now()}] 沒有偵測到新商品或價格變化。")

    return alerts


def main():
    parser = argparse.ArgumentParser(description="Funbox 戰鬥陀螺新品監控")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="印出抓到的原始網頁內容跟HTTP狀態，協助診斷為什麼抓不到商品",
    )
    parser.add_argument(
        "--notify-test",
        action="store_true",
        dest="notify_test",
        help="直接發送一則測試訊息，不抓取網頁，用來確認通知設定是否正確",
    )
    parser.add_argument(
        "--test", action="store_true", help="只執行一次，不發通知，用來確認解析結果是否正確"
    )
    parser.add_argument(
        "--once", action="store_true", help="只執行一次，有變化就發通知，然後結束（適合雲端排程）"
    )
    args = parser.parse_args()

    if args.debug:
        print(f"[{now()}] === Debug 模式：印出原始抓取內容，協助診斷 ===")
        resp = requests.get(TARGET_URL, headers=HEADERS, timeout=15)
        print(f"HTTP 狀態碼: {resp.status_code}")
        print(f"最終網址（如果被重新導向會跟原網址不同）: {resp.url}")
        print(f"回應內容長度: {len(resp.text)} 字元")
        print(f"【原始 HTML】是否包含 'NT$': {'NT$' in resp.text}")
        print(f"【原始 HTML】是否包含 '戰鬥陀螺': {'戰鬥陀螺' in resp.text}")
        print(f"【原始 HTML】是否包含 '加入購物車': {'加入購物車' in resp.text}")

        print("\n---- 套用正式解析流程後（移除 script/style 後的純文字）----")
        processed_text = fetch_text_requests()
        print(f"處理後文字長度: {len(processed_text)} 字元")
        has_nt = "NT$" in processed_text
        print(f"【處理後文字】是否包含 'NT$': {has_nt}")

        if has_nt:
            idx = processed_text.find("NT$")
            print("---- 'NT$' 第一次出現的前後文字（前後各300字）----")
            print(processed_text[max(0, idx - 300): idx + 300])
        else:
            print(
                "處理後的文字完全沒有 'NT$' 了！代表價格資料應該是藏在 <script> 標籤裡的"
                "JSON/設定資料中，而不是畫面上的可見文字，移除 script 標籤時被一起清掉了。"
                "這種情況需要改用『瀏覽器渲染』(Playwright) 的方式抓取，而不是現在的 requests 方式。"
            )

        products = parse_products(processed_text)
        print(f"\n套用 parse_products() 解析後，找到 {len(products)} 個商品：")
        for name, price in products[:15]:
            print(f"   - {name}：NT${price}")

        print("\n---- 原始 HTML 前 2000 字元（給你參考） ----")
        print(resp.text[:2000])
        return

    if args.notify_test:
        print(f"[{now()}] === 通知測試模式：直接發送測試訊息，不抓取網頁 ===")
        print(f"目前啟用的通知方式：{NOTIFY_METHODS}")
        send_notification(
            "【測試】Funbox 監控通知測試",
            "如果你收到這則訊息，代表通知設定成功！🎉",
        )
        print(
            f"[{now()}] 已嘗試發送。請檢查手機/Telegram有沒有收到。"
            f"如果上面印出 ❌ 失敗訊息，代表對應的 TOKEN / TOPIC 設定還不正確，請重新檢查。"
        )
        return

    if args.test:
        print(f"[{now()}] === 測試模式：只執行一次，不發送通知 ===")
        check_once(send_notify=False)
        print(f"\n[{now()}] 測試完成。請確認上面印出的商品名稱與價格是否正確。")
        print("確認沒問題後，就可以用 --once（雲端排程）或不加參數（本機長駐）來真正監控。")
        return

    if args.once:
        print(f"[{now()}] === 單次檢查模式 ===")
        check_once(send_notify=True)
        return

    # 不加任何參數：長駐模式，自己每隔一段時間檢查一次（適合放在自己電腦上跑）
    print(f"[{now()}] === 開始長駐監控 {TARGET_URL} ===")
    print(f"每 {CHECK_INTERVAL_SECONDS // 60} 分鐘檢查一次，按 Ctrl+C 可停止。\n")
    while True:
        try:
            check_once(send_notify=True)
        except requests.exceptions.RequestException as e:
            print(f"[{now()}] 網路錯誤，稍後會重試：{e}")
        except Exception as e:
            print(f"[{now()}] 發生未預期的錯誤：{e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
