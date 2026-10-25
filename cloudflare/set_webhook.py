"""把 Telegram 機器人接到 Cloudflare 門鈴(webhook)。

token 從 config.json 讀;暗號(WEBHOOK_SECRET)由你在終端機輸入,輸入時不會顯示。
    py cloudflare/set_webhook.py --new     自動產生新暗號、設定 Telegram,暗號複製到剪貼簿
                                           (再貼到 Cloudflare Worker 的 WEBHOOK_SECRET)
    py cloudflare/set_webhook.py           手動輸入既有的暗號
    py cloudflare/set_webhook.py --remove  取消,改回每 5 分鐘檢查一次
"""
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

WORKER_URL = "https://rapid-voice-d1db.b03609005.workers.dev/"

token = json.load(open(__import__("pathlib").Path(__file__).resolve().parent.parent / "config.json", encoding="utf-8"))["notify"]["telegram_bot_token"].strip()
api = f"https://api.telegram.org/bot{token}"

if "--remove" in sys.argv:
    r = json.load(urllib.request.urlopen(f"{api}/deleteWebhook", timeout=15))
    print("已取消 webhook" if r.get("ok") else f"失敗:{r}")
    sys.exit()

if "--new" in sys.argv:
    # 自動產生新暗號,只複製到剪貼簿、不顯示在畫面上
    import secrets
    import subprocess
    secret = secrets.token_urlsafe(24)
    subprocess.run("clip", input=secret.encode("ascii"), check=True, shell=True)
else:
    print("(請先切到英文輸入法,全形字 Telegram 不接受)")
    secret = getpass.getpass("輸入你在 Cloudflare 設的 WEBHOOK_SECRET(畫面不會顯示,打完按 Enter):").strip()
ok_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
if not secret or not set(secret) <= ok_chars:
    sys.exit(f"❌ 暗號只能用半形英文、數字、底線、減號(你輸入了 {len(secret)} 個字,"
             "可能含全形字或空白)。請切到英文輸入法再試一次;"
             "如果 Cloudflare 那邊的暗號本身有符號,先回 Cloudflare 改成純英數字")
print(f"(收到 {len(secret)} 個字的暗號,格式正確)")

data = urllib.parse.urlencode({"url": WORKER_URL, "secret_token": secret,
                               "allowed_updates": json.dumps(["message"])}).encode()
try:
    r = json.load(urllib.request.urlopen(f"{api}/setWebhook", data=data, timeout=15))
except urllib.error.HTTPError as e:
    r = json.loads(e.read().decode("utf-8", "replace") or "{}")
if not r.get("ok"):
    sys.exit(f"❌ Telegram 拒絕,原因:{r.get('description')}")
if "--new" in sys.argv:
    print("✅ Telegram 已接上門鈴,新暗號已複製到剪貼簿(畫面不顯示)。")
    print("下一步:到 Cloudflare → Worker → Settings → Variables and Secrets,")
    print("把 WEBHOOK_SECRET 編輯後按 Ctrl+V 貼上新值,存檔並 Deploy,再到 Telegram 點 /help 測試。")
else:
    print("✅ 已接上 Cloudflare 門鈴,到 Telegram 點 /help 測試")
