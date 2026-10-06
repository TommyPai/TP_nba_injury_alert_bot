#!/usr/bin/env python3
"""NBA Fantasy 傷病即時提醒

每隔 N 秒檢查 ESPN 傷病報告。只要有「先發」或「場均上場超過 25 分鐘」的球員
出現新的傷病狀態,就立刻推播(Windows 通知 / Discord / Telegram / ntfy),
並從同隊名單挑出值得關注的替補球員。

只用 Python 標準函式庫,不需安裝任何套件。

用法:
    python nba_injury_alert.py                 持續監控
    python nba_injury_alert.py --once          只檢查一次就結束
    python nba_injury_alert.py --once --dry    只印在畫面上,不推播(測試用)
    python nba_injury_alert.py --test-notify   送一則測試通知,確認推播設定
    python nba_injury_alert.py --setup-telegram 自動找出 Telegram chat id 並寫入設定
    python nba_injury_alert.py --alert-existing 第一次執行也把目前已有的傷病全部推播
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
CACHE_PATH = BASE / "stats_cache.json"

INJURY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster"
STATS_URL = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
             "seasons/{year}/types/2/athletes/{pid}/statistics")

DEFAULT_CONFIG = {
    "poll_seconds": 120,
    "min_minutes": 25,
    "starter_ratio": 0.5,
    "min_games_for_current_season": 5,
    "replacements": 3,
    "replacement_min_minutes": 8,
    "ignore_statuses": [],
    "my_players": [],
    "score_weights": {"pts": 1.0, "reb": 1.2, "ast": 1.5, "stl": 3.0, "blk": 3.0,
                      "fg3m": 1.0, "tov": -1.0},
    "notify": {
        "windows_toast": True,
        "discord_webhook": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "ntfy_topic": "",
    },
}

STATUS_ZH = {
    "Out": ("🔴", "確定缺陣"),
    "Doubtful": ("🟠", "極可能缺陣"),
    "Questionable": ("🟡", "出賽成疑"),
    "Day-To-Day": ("🟡", "每日觀察"),
    "Probable": ("🟢", "可能出賽"),
    "Suspension": ("🔴", "禁賽"),
}
POS_GROUP = {"PG": "G", "SG": "G", "G": "G", "SF": "F", "PF": "F", "F": "F", "C": "C"}
STAT_KEYS = {
    "avgMinutes": "min", "gamesPlayed": "gp", "gamesStarted": "gs", "avgPoints": "pts",
    "avgRebounds": "reb", "avgAssists": "ast", "avgSteals": "stl", "avgBlocks": "blk",
    "avgTurnovers": "tov", "avgThreePointFieldGoalsMade": "fg3m",
}
STATS_TTL = 12 * 3600
ROSTER_TTL = 6 * 3600


# ---------- 基礎工具 ----------

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nba-injury-alert"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(1.5 * (attempt + 1))


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config():
    if not CONFIG_PATH.exists():
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        log(f"已建立設定檔 {CONFIG_PATH.name},可填入推播資訊與你的球員名單")
    user = load_json(CONFIG_PATH, {})
    cfg = {**DEFAULT_CONFIG, **user}
    cfg["notify"] = {**DEFAULT_CONFIG["notify"], **user.get("notify", {})}
    cfg["score_weights"] = {**DEFAULT_CONFIG["score_weights"], **user.get("score_weights", {})}
    # 環境變數優先(雲端用 GitHub Secrets 傳入,token 不必寫進檔案)
    for env, key in (("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
                     ("TELEGRAM_CHAT_ID", "telegram_chat_id"),
                     ("DISCORD_WEBHOOK", "discord_webhook"),
                     ("NTFY_TOPIC", "ntfy_topic")):
        if os.environ.get(env):
            cfg["notify"][key] = os.environ[env].strip()
    if os.environ.get("MY_PLAYERS"):
        cfg["my_players"] = [n.strip() for n in os.environ["MY_PLAYERS"].split(",") if n.strip()]
    return cfg


# ---------- 資料抓取(含快取) ----------

class Cache:
    def __init__(self):
        raw = load_json(CACHE_PATH, {})
        self.stats = raw.get("stats", {})
        self.rosters = raw.get("rosters", {})

    def save(self):
        save_json(CACHE_PATH, {"stats": self.stats, "rosters": self.rosters})


def fetch_season_stats(pid, year):
    d = get_json(STATS_URL.format(year=year, pid=pid))
    if not d:
        return None
    out = {}
    for cat in d.get("splits", {}).get("categories", []):
        for s in cat.get("stats", []):
            if s["name"] in STAT_KEYS:
                out[STAT_KEYS[s["name"]]] = float(s["value"])
    return out or None


def get_stats(cache, pid, year, cfg):
    """回傳球員場均數據。本季出賽太少(季前賽/開季初)就改用上一季。"""
    key = f"{pid}:{year}"
    hit = cache.stats.get(key)
    if hit and time.time() - hit["t"] < STATS_TTL:
        return hit["v"]
    cur = fetch_season_stats(pid, year)
    if cur and cur.get("gp", 0) >= cfg["min_games_for_current_season"]:
        stats = {**cur, "season": year}
    else:
        prev = fetch_season_stats(pid, year - 1)
        pick = prev or cur
        stats = {**pick, "season": year - 1 if prev else year} if pick else {}
    for k in STAT_KEYS.values():
        stats.setdefault(k, 0.0)
    cache.stats[key] = {"t": time.time(), "v": stats}
    return stats


def get_roster(cache, tid):
    hit = cache.rosters.get(tid)
    if hit and time.time() - hit["t"] < ROSTER_TTL:
        return hit["v"]
    d = get_json(ROSTER_URL.format(tid=tid)) or {}
    players = []
    for a in d.get("athletes", []):
        players.extend(a["items"] if "items" in a else [a])
    roster = [{"id": str(p["id"]), "name": p.get("displayName", "?"),
               "pos": p.get("position", {}).get("abbreviation", "")} for p in players]
    cache.rosters[tid] = {"t": time.time(), "v": roster}
    return roster


def parse_injuries(data):
    """攤平成 [{pid, name, team, tid, pos, status, comment, date, detail}]"""
    rows = []
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {})
            pid = str(ath.get("id") or "")
            if not pid:
                for link in ath.get("links", []):
                    m = re.search(r"/id/(\d+)", link.get("href", ""))
                    if m:
                        pid = m.group(1)
                        break
            if not pid:
                continue
            det = inj.get("details") or {}
            detail = " ".join(str(x) for x in (det.get("side"), det.get("location"), det.get("type"))
                              if x and x != "Not Specified")
            rows.append({
                "pid": pid,
                "name": ath.get("displayName", "?"),
                "team": ath.get("team", {}).get("abbreviation") or team.get("displayName", "?"),
                "tid": str(team.get("id", "")),
                "pos": ath.get("position", {}).get("abbreviation", ""),
                "status": inj.get("status", ""),
                "comment": inj.get("shortComment") or inj.get("longComment") or "",
                "date": inj.get("date", ""),
                "detail": detail,
            })
    return rows


# ---------- 判斷與建議 ----------

def is_key_player(s, cfg):
    gp = s.get("gp", 0)
    starter = gp > 0 and s.get("gs", 0) / gp >= cfg["starter_ratio"]
    return (s.get("min", 0) > cfg["min_minutes"]) or starter


def fantasy_score(s, cfg):
    return sum(s.get(k, 0.0) * w for k, w in cfg["score_weights"].items())


def season_label(year):
    y = int(year or 0)
    return f"{y - 1}-{str(y)[2:]}賽季" if y else "賽季未知"


def line(s):
    return (f"{s['min']:.1f}分鐘 | {s['pts']:.1f}分 {s['reb']:.1f}板 {s['ast']:.1f}助 "
            f"{s['stl']:.1f}抄 {s['blk']:.1f}阻 {s['fg3m']:.1f}三")


def suggest_replacements(cache, inj, stats, year, out_ids, cfg):
    roster = [p for p in get_roster(cache, inj["tid"])
              if p["id"] != inj["pid"] and p["id"] not in out_ids]
    with ThreadPoolExecutor(max_workers=8) as ex:
        all_stats = list(ex.map(lambda p: get_stats(cache, p["id"], year, cfg), roster))
    group = POS_GROUP.get(inj["pos"])
    cands = []
    for p, s in zip(roster, all_stats):
        if s.get("min", 0) < cfg["replacement_min_minutes"]:
            continue
        gp = s.get("gp", 0)
        bench = not (gp > 0 and s.get("gs", 0) / gp >= cfg["starter_ratio"])
        bonus = 1.15 if group and POS_GROUP.get(p["pos"]) == group else 1.0
        cands.append((bench, fantasy_score(s, cfg) * bonus, p, s))
    # 替補優先(最可能因此多拿到上場時間,也最可能在自由球員名單);不足再補先發
    cands.sort(key=lambda c: (not c[0], -c[1]))
    return [(p, s, bench) for bench, _, p, s in cands[:cfg["replacements"]]]


def build_message(inj, stats, reps, cfg):
    emoji, zh = STATUS_ZH.get(inj["status"], ("⚠️", inj["status"]))
    mine = "⭐你的球員 " if inj["name"] in cfg["my_players"] else ""
    gp = int(stats.get("gp", 0))
    role = f"先發 {int(stats.get('gs', 0))}/{gp}" if gp else "無出賽紀錄"
    head = f"{emoji} {mine}[{inj['team']}] {inj['name']} {zh}({inj['status']})"
    body = []
    if inj["detail"]:
        body.append(f"傷勢:{inj['detail']}")
    if inj["comment"]:
        body.append(inj["comment"])
    body.append(f"{inj['name']}:{role}({season_label(stats.get('season'))})")
    body.append("  " + line(stats))
    short = []
    if reps:
        body.append("建議關注:")
        for i, (p, s, bench) in enumerate(reps, 1):
            tag = "替補" if bench else "先發"
            body.append(f"{i}. {p['name']} ({p['pos']},{tag}) {line(s)}")
            short.append(p["name"])
    else:
        body.append("(找不到合適的同隊替補)")
    return head, "\n".join(body), ("可關注:" + "、".join(short)) if short else ""


# ---------- 推播 ----------

def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "nba-injury-alert"})
    urllib.request.urlopen(req, timeout=15).read()


def toast(title, text):
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null;"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n=$x.GetElementsByTagName('text');"
        "$n.Item(0).AppendChild($x.CreateTextNode($env:NBA_TITLE)) > $null;"
        "$n.Item(1).AppendChild($x.CreateTextNode($env:NBA_BODY)) > $null;"
        "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)"
    )
    env = {**os.environ, "NBA_TITLE": title, "NBA_BODY": text}
    subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env,
                   capture_output=True, timeout=20)


def notify(cfg, head, body, short):
    n = cfg["notify"]
    full = f"{head}\n{body}"
    channels = [
        ("Windows 通知", n["windows_toast"] and sys.platform == "win32",
         lambda: toast(head, short or body[:120])),
        ("Discord", n["discord_webhook"],
         lambda: post_json(n["discord_webhook"], {"content": f"**{head}**\n{body}"[:1990]})),
        ("Telegram", n["telegram_bot_token"] and n["telegram_chat_id"],
         lambda: post_json(f"https://api.telegram.org/bot{n['telegram_bot_token']}/sendMessage",
                           {"chat_id": n["telegram_chat_id"], "text": full[:4000]})),
        ("ntfy", n["ntfy_topic"],
         lambda: post_json("https://ntfy.sh/", {"topic": n["ntfy_topic"], "title": head,
                                                "message": body, "priority": 4})),
    ]
    for name, enabled, fn in channels:
        if not enabled:
            continue
        try:
            fn()
        except Exception as e:  # 單一通道失敗不影響其他通道
            log(f"推播失敗 ({name}): {e}")


# ---------- 主流程 ----------

def check(cfg, cache, state, dry, alert_existing):
    data = get_json(INJURY_URL)
    year = data["season"]["year"]
    rows = [r for r in parse_injuries(data) if r["status"] not in cfg["ignore_statuses"]]
    first_run = "seen" not in state
    seen = set(state.get("seen", []))
    out_ids = {r["pid"] for r in rows if r["status"] in ("Out", "Doubtful", "Suspension")}

    new = [r for r in rows if f"{r['pid']}:{r['status']}" not in seen]
    with ThreadPoolExecutor(max_workers=8) as ex:  # 先平行抓新傷兵的數據
        stats_list = list(ex.map(lambda r: get_stats(cache, r["pid"], year, cfg), new))

    silent = first_run and not alert_existing
    if first_run:
        log(f"首次執行:目前共 {len(rows)} 則傷病,"
            + ("僅列出不推播(加 --alert-existing 可一併推播)" if silent else "全部依規則推播"))
    hits = 0
    for inj, s in zip(new, stats_list):
        if not is_key_player(s, cfg):
            continue
        hits += 1
        reps = suggest_replacements(cache, inj, s, year, out_ids, cfg)
        head, body, short = build_message(inj, s, reps, cfg)
        print(f"\n{'=' * 60}\n{head}\n{body}", flush=True)
        if not dry and not silent:
            notify(cfg, head, body, short)
    if not new:
        log("沒有新的傷病更新")
    elif not hits:
        log(f"{len(new)} 則新傷病,但都不是先發/場均超過 {cfg['min_minutes']} 分鐘的球員")

    new_seen = sorted(f"{r['pid']}:{r['status']}" for r in rows)
    if first_run or new_seen != state.get("seen"):  # 沒變就不寫檔,雲端才不會每次都產生 commit
        state["seen"] = new_seen
        save_json(STATE_PATH, state)
    cache.save()


def setup_telegram(cfg):
    """用 config.json 裡的 bot token 自動找出 chat id 並寫回設定檔。"""
    token = cfg["notify"]["telegram_bot_token"]
    if not token:
        log("請先在 config.json 的 notify.telegram_bot_token 填入 BotFather 給你的 token")
        return
    d = get_json(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = [u[k]["chat"] for u in (d or {}).get("result", [])
             for k in ("message", "channel_post", "my_chat_member") if k in u]
    if not chats:
        log("找不到對話。請先在 Telegram 打開你的 bot,按 Start 並傳一句話,再重新執行此指令")
        return
    chat = chats[-1]
    raw = load_json(CONFIG_PATH, {})
    raw.setdefault("notify", {})["telegram_chat_id"] = str(chat["id"])
    save_json(CONFIG_PATH, raw)
    cfg["notify"]["telegram_chat_id"] = str(chat["id"])
    name = chat.get("first_name") or chat.get("title") or chat.get("username") or ""
    log(f"已綁定 Telegram 對話 {name}({chat['id']}),寫入 config.json")
    notify(cfg, "✅ NBA Fantasy 提醒已連線", "之後有先發/主力球員傷病會即時傳到這裡。", "")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="NBA Fantasy 傷病即時提醒")
    ap.add_argument("--once", action="store_true", help="只檢查一次")
    ap.add_argument("--dry", action="store_true", help="只印出,不推播")
    ap.add_argument("--alert-existing", action="store_true", help="首次執行也推播現有傷病")
    ap.add_argument("--test-notify", action="store_true", help="送測試通知")
    ap.add_argument("--setup-telegram", action="store_true", help="自動綁定 Telegram chat id")
    args = ap.parse_args()

    cfg = load_config()
    if args.setup_telegram:
        setup_telegram(cfg)
        return
    if args.test_notify:
        notify(cfg, "🧪 NBA Fantasy 提醒測試", "推播設定正常。\n建議關注:球員A、球員B", "可關注:球員A、球員B")
        log("已送出測試通知")
        return

    cache, state = Cache(), load_json(STATE_PATH, {})
    log(f"開始監控,每 {cfg['poll_seconds']} 秒檢查一次 (Ctrl+C 結束)")
    while True:
        try:
            check(cfg, cache, state, args.dry, args.alert_existing)
        except Exception as e:
            log(f"檢查失敗,稍後重試: {e}")
        if args.once:
            break
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
