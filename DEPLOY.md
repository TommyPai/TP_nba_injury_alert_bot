# 部署到 GitHub Actions(電腦關機也會通知)

1. 到 https://github.com/new 建立 repo,名稱隨意,**選 Public**
   (Public 的 Actions 分鐘數免費不限;Private 每月 2000 分鐘,每 5 分鐘跑一次會超過)。
   程式碼裡沒有任何 token,token 只放在下一步的 Secrets。
2. 把這個資料夾的檔案上傳到 repo(`nba_injury_alert.py`、`.github/workflows/alert.yml`、`.gitignore`)。
   網頁上傳最簡單,注意 `.github` 資料夾要一起傳。
3. repo → Settings → Secrets and variables → Actions:
   - Secrets 新增 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
     (chat id 先在本機跑 `py nba_injury_alert.py --setup-telegram` 取得,看 config.json)
   - Variables 新增 `MY_PLAYERS`(選填),用逗號分隔,例如 `Coby White, Naz Reid`
4. repo → Actions 分頁 → 啟用 workflows → 選 "NBA injury alert" → Run workflow,
   勾選 test 送一則測試通知,確認 Telegram 收得到。
5. 之後每 5 分鐘自動執行。第一次執行只建立基準(不推播),之後有新傷病才通知。
