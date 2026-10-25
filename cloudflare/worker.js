// Telegram 門鈴:你點選單時 Telegram 立刻通知這裡,這裡馬上回「收到」並叫 GitHub 跑報告。
//
// 需要在 Cloudflare Worker 的 Settings → Variables and Secrets 設定:
//   TELEGRAM_BOT_TOKEN  (Secret) 機器人 token
//   TELEGRAM_CHAT_ID    (Text)   你的聊天室 ID,只回應這個聊天室
//   GITHUB_TOKEN        (Secret) GitHub fine-grained token,只開這個 repo 的 Actions 讀寫權限
//   GITHUB_REPO         (Text)   TommyPai/TP_nba_injury_alert_bot
//   WEBHOOK_SECRET      (Secret) 自己編一串英數字,設定 webhook 時也要用同一串

const COMMANDS = ["daily", "weekly", "review", "lastweek", "injuries", "help"];

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    // 只接受帶著正確暗號的請求(Telegram 會在每次通知附上設定 webhook 時的 secret_token)
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json().catch(() => ({}));
    const msg = update.message;
    if (!msg || !msg.text || String(msg.chat.id) !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response("ok");
    }
    const cmd = msg.text.trim().split(/\s+/)[0].split("@")[0].replace(/^\//, "").toLowerCase();
    if (!COMMANDS.includes(cmd)) return new Response("ok");

    const r = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/command.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "tg-fantasy-bot",
        },
        body: JSON.stringify({ ref: "main", inputs: { cmd } }),
      },
    );
    const text = r.ok
      ? `⏳ 收到 /${cmd},約 1 到 2 分鐘後回覆`
      : `⚠️ 無法啟動 /${cmd}(GitHub 回應 ${r.status}),請檢查 GITHUB_TOKEN 是否過期`;
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: msg.chat.id, text }),
    });
    return new Response("ok");
  },
};
