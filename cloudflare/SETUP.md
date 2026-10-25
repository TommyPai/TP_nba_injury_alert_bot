# Telegram 選單即時回應(Cloudflare 門鈴)設定

沒設定也能用,只是點選單後要等 5 到 15 分鐘;設定後約 1 到 2 分鐘。

## 1. 建 GitHub token(讓門鈴能叫 GitHub 跑報告)
1. https://github.com/settings/personal-access-tokens/new
2. Token name:`tg-bot`;Expiration:選最長(到期後要換新的)
3. Repository access:**Only select repositories** → 選 `TP_nba_injury_alert_bot`
4. Permissions → Repository permissions → **Actions:Read and write**(其他都不用開)
5. Generate token,複製起來(只會顯示一次)

## 2. 建 Cloudflare Worker
1. 註冊或登入 https://dash.cloudflare.com (免費方案就夠)
2. 左側 **Workers & Pages** → **Create** → **Create Worker** → 名稱填 `tg-fantasy-bot` → **Deploy**
3. **Edit code** → 把 repo 裡 `cloudflare/worker.js` 的內容整個貼上取代 → **Deploy**
4. 記下網址,例如 `https://tg-fantasy-bot.你的帳號.workers.dev`
5. 回到 Worker 頁面 → **Settings** → **Variables and Secrets** → 逐一 **Add**:

| 名稱 | 類型 | 值 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Secret | 機器人 token(和 GitHub Secrets 裡同一個) |
| `TELEGRAM_CHAT_ID` | Text | `8038554375` |
| `GITHUB_TOKEN` | Secret | 第 1 步複製的 token |
| `GITHUB_REPO` | Text | `TommyPai/TP_nba_injury_alert_bot` |
| `WEBHOOK_SECRET` | Secret | 自己編一串英文和數字,例如 `fantasy2027abc`(不要用符號) |

## 3. 把 Telegram 接到門鈴
在瀏覽器網址列貼上下面這行(把三個 `<...>` 換成你的值),按 Enter:

    https://api.telegram.org/bot<機器人token>/setWebhook?url=<Worker網址>&secret_token=<WEBHOOK_SECRET>

看到 `"ok":true,"result":true` 就完成了。到 Telegram 點選單 `/help` 測試,應該馬上收到「⏳ 收到」。

## 想改回原本的方式
網址列貼上 `https://api.telegram.org/bot<機器人token>/deleteWebhook`。
機器人會自動改回每 5 分鐘檢查一次指令。
