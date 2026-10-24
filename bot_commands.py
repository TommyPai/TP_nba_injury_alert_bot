#!/usr/bin/env python3
"""Telegram 指令選單(對話框左下角的 Menu)。

跟著每 5 分鐘的傷病檢查一起執行:讀取你傳給機器人的指令,執行對應的報告後推播回來。
只回應 config / Secrets 裡設定的那個聊天室,其他人傳的指令一律忽略。
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import espn_league
from nba_injury_alert import STATE_PATH, load_config, load_json, log, notify, save_json

BASE = Path(__file__).resolve().parent
# (指令, 選單說明, 要執行的程式);說明開頭的分類讓選單一眼看出用途
COMMANDS = [
    ("daily", "👀 每日|自由球員關注名單", ["daily_watch.py"]),
    ("weekly", "📅 每週|本週串流與對戰預測", ["weekly_stream.py"]),
    ("review", "🔍 戰況|本週到目前為止", ["matchup_review.py"]),
    ("lastweek", "🔍 戰況|上週檢討", ["matchup_review.py", "--last"]),
    ("injuries", "🚑 傷病|我的球員現況", None),
    ("help", "ℹ️ 說明|所有功能與推播時間", None),
]
HELP = """所有功能
👀 /daily 自由球員關注名單:角色上升、今天有比賽、整體最佳(每天早上 8 點自動推)
📅 /weekly 本週串流與對戰預測:你 vs 對手九類、串流人選(每週一早上 8 點自動推)
🔍 /review 本週到目前為止的戰況:哪幾類輸、誰拖累
🔍 /lastweek 上週檢討
🚑 /injuries 你的球員傷病現況
🚨 傷病通知:每 5 分鐘自動檢查,先發或主力受傷時推播

點選單後約 5 到 15 分鐘回覆(機器人每 5 分鐘醒來一次)。"""


def tg(token, method, payload):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def my_injuries():
    lg = espn_league.connect()
    if not lg:
        return "沒有 ESPN 登入資訊,查不到你的陣容"
    me = espn_league.my_team(lg)
    if not me:
        return f"找不到你的球隊「{espn_league.TEAM_NAME}」"
    hurt = [p for p in me.roster if (p.injuryStatus or "ACTIVE") not in ("ACTIVE", "NORMAL")]
    if not hurt:
        return "✅ 你的球員目前都健康"
    zh = {"OUT": "缺陣", "DAY_TO_DAY": "每日觀察", "QUESTIONABLE": "出賽成疑", "DOUBTFUL": "極可能缺陣",
          "INJURY_RESERVE": "傷兵名單", "SUSPENSION": "禁賽"}
    return "\n".join(f"• {p.name}({p.proTeam}):{zh.get(p.injuryStatus, p.injuryStatus)}"
                     f"{',在 IL 位' if p.lineupSlot == 'IR' else ''}" for p in hurt)


def main():
    cfg = load_config()
    token, chat = cfg["notify"]["telegram_bot_token"], str(cfg["notify"]["telegram_chat_id"])
    if not (token and chat):
        return
    state = load_json(STATE_PATH, {})
    before = json.dumps(state, sort_keys=True)

    # 選單內容有變才重新登記
    sig = json.dumps([[c, d] for c, d, _ in COMMANDS], ensure_ascii=False)
    if state.get("tg_commands") != sig:
        tg(token, "setMyCommands", {"commands": [{"command": c, "description": d} for c, d, _ in COMMANDS]})
        state["tg_commands"] = sig
        log("已更新 Telegram 指令選單")

    todo = []
    for u in tg(token, "getUpdates", {"offset": state.get("tg_offset", 0), "timeout": 0,
                                      "allowed_updates": ["message"]}).get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        text = (m.get("text") or "").strip()
        if str(m.get("chat", {}).get("id")) != chat or not text.startswith("/"):
            continue
        cmd = text.split()[0].split("@")[0][1:].lower()
        if cmd in {c for c, _, _ in COMMANDS} and cmd not in todo:
            todo.append(cmd)

    # 先存下已讀位置,就算後面的報告失敗也不會重複執行同一個指令
    if json.dumps(state, sort_keys=True) != before:
        save_json(STATE_PATH, state)

    for cmd in todo:
        log(f"執行指令 /{cmd}")
        script = next(s for c, _, s in COMMANDS if c == cmd)
        try:
            if cmd == "help":
                notify(cfg, "ℹ️ 機器人功能", HELP, "")
            elif cmd == "injuries":
                notify(cfg, "🚑 我的球員傷病現況", my_injuries(), "")
            else:
                subprocess.run([sys.executable, *script], cwd=BASE, timeout=420, check=True)
        except Exception as e:
            log(f"/{cmd} 失敗:{e}")
            notify(cfg, f"⚠️ /{cmd} 執行失敗", str(e)[:300], "")


if __name__ == "__main__":
    main()
