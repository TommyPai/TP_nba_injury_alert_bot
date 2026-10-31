#!/usr/bin/env python3
"""每週串流推播

列出本週(美東週一到週日)每隊比賽場數，並從比賽最多的球隊裡，
挑出撿得到、符合球隊打法(見 blend.WEIGHTS)的串流人選，推播到 Telegram。

有接上 ESPN 聯盟(ESPN_S2、ESPN_SWID)時：
- 只推薦聯盟裡真正的自由球員
- 加上「你 vs 本週對手」的九類預測，串流人選會偏向拉鋸中的類別
沒接上時，改用 drafted.txt 排除已被選走的人。

用法:
    python weekly_stream.py          推播本週(週日執行時改看下一週)
    python weekly_stream.py --dry    只印出，不推播
"""
import argparse
import html
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import espn_league
from blend import BUILD_NAME, blended_stats, build_value, season_stats as _season_stats
from nba_injury_alert import get_json, load_config, notify

BASE = Path(__file__).resolve().parent
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}"
ROSTER = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster"
STATS = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
         "seasons/{year}/types/2/athletes/{pid}/statistics")
INJURIES = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
KEYS = {"gamesPlayed": "gp", "avgMinutes": "min", "avgPoints": "pts", "avgRebounds": "reb",
        "avgAssists": "ast", "avgSteals": "stl", "avgBlocks": "blk", "avgTurnovers": "tov",
        "avgThreePointFieldGoalsMade": "tpm", "avgFreeThrowsAttempted": "fta", "freeThrowPct": "ftp",
        "avgFieldGoalsAttempted": "fga", "fieldGoalPct": "fgp"}
TOP_N = 8
MIN_MINUTES = 20
# (顯示名稱, 欄位, 拉鋸門檻);計數類用相對差距,命中率用絕對差距
CATS = [("得分", "pts", .08), ("籃板", "reb", .08), ("助攻", "ast", .08), ("抄截", "stl", .10),
        ("火鍋", "blk", .12), ("3PM", "tpm", .10), ("FG%", "fg", .010), ("FT%", "ft", .015),
        ("失誤", "tov", .08)]


def norm(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"[^a-z]", "", n)


def week_start():
    """NBA fantasy 週以美東週一起算；週日執行就看下一週。"""
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    d = et.date()
    return d + timedelta(days=1) if d.weekday() == 6 else d - timedelta(days=d.weekday())


def games_this_week(start):
    """回傳 (每隊場數, 球隊 ID, 每天有比賽的球隊清單)。"""
    games, team_ids, day_teams = {}, {}, []
    days = [start + timedelta(days=i) for i in range(7)]
    with ThreadPoolExecutor(7) as ex:
        boards = list(ex.map(lambda d: get_json(SCOREBOARD.format(d=d.strftime("%Y%m%d"))) or {}, days))
    for b in boards:
        today = set()
        for e in b.get("events", []):
            if e.get("season", {}).get("type") != 2:
                continue
            for c in e["competitions"][0]["competitors"]:
                abbr = c["team"]["abbreviation"]
                games[abbr] = games.get(abbr, 0) + 1
                team_ids[abbr] = c["team"]["id"]
                today.add(abbr)
        day_teams.append(today)
    return games, team_ids, day_teams


def season_stats(pid, year):
    return _season_stats(pid, year, get_json)


def player_stats(pid, year):
    """今年和去年混合評估(見 blend.py):上場時間最快反映今年角色,產量和命中率逐場增加今年的權重。"""
    return blended_stats(pid, year, get_json)


def cade_value(s, boost=()):
    """單場價值,權重見 blend.WEIGHTS;boost 是本週拉鋸中的類別,權重加倍。"""
    return build_value(s, boost)


def active_slots(team):
    """每天的先發位置數:陣容裡目前不在板凳(BE)和傷兵(IR)位的人數,判斷不出來就用 10。"""
    n = sum(1 for p in team.roster if p.lineupSlot not in ("BE", "IR", ""))
    return n if 5 <= n <= 15 else 10


def project(players, day_teams, pid_team, year, slots):
    """一隊本週的預估九類總量。
    逐日計算:當天有比賽的人裡,只有價值最高的 slots 個能排進先發、數據才算數(板凳不算);
    IR 和確定缺陣的不算。"""
    tot = dict.fromkeys(["pts", "reb", "ast", "stl", "blk", "tpm", "tov", "fga", "fgm", "fta", "ftm"], 0.0)
    pool = []
    for p in players:
        if p.lineupSlot == "IR" or (p.injuryStatus or "").upper() == "OUT":
            continue
        team = pid_team.get(str(p.playerId))
        s = player_stats(p.playerId, year) if team else None
        if s:
            pool.append((team, s, cade_value(s)))
    for today in day_teams:
        starters = sorted((x for x in pool if x[0] in today), key=lambda x: -x[2])[:slots]
        for _, s, _ in starters:
            for k in ("pts", "reb", "ast", "stl", "blk", "tpm", "tov"):
                tot[k] += s.get(k, 0)
            tot["fga"] += s.get("fga", 0)
            tot["fgm"] += s.get("fga", 0) * s.get("fgp", 0)
            tot["fta"] += s.get("fta", 0)
            tot["ftm"] += s.get("fta", 0) * s.get("ftp", 0)
    tot["fg"] = tot["fgm"] / tot["fga"] if tot["fga"] else 0
    tot["ft"] = tot["ftm"] / tot["fta"] if tot["fta"] else 0
    return tot


def compare(me, opp):
    """回傳 [(名稱, 欄位, 我, 對手, 'win'|'close'|'lose')]。失誤越少越好。"""
    out = []
    for name, k, th in CATS:
        a, b = me[k], opp[k]
        diff = (a - b) if k in ("fg", "ft") else (a - b) / max(b, 1)
        if k == "tov":
            diff = -diff
        out.append((name, k, a, b, "win" if diff > th else "lose" if diff < -th else "close"))
    return out


def fmt(k, v):
    return f"{v * 100:.1f}" if k in ("fg", "ft") else f"{v:.0f}"


# 表格用英文縮寫:中文字在 Telegram 的等寬字體裡寬度不固定,會把欄位擠歪
ABBR = {"pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL", "blk": "BLK", "tpm": "3PM",
        "fg": "FG%", "ft": "FT%", "tov": "TO"}
MARK = {"win": "✅", "close": "⚖️", "lose": "❌"}


def matchup_table(res):
    rows = ["CAT     YOU    OPP"]
    rows += [f"{ABBR[k]:<4}{fmt(k, a):>7}{fmt(k, b):>7} {MARK[t]}" for _, k, a, b, t in res]
    return "\n".join(rows)


def to_html(blocks):
    """blocks 是 (文字, 是否表格) 的清單;一般文字跳脫成 HTML,表格包成 <pre> 等寬顯示。"""
    return "\n".join(f"<pre>{html.escape(t)}</pre>" if pre else html.escape(t) for t, pre in blocks)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--week", help="指定該週週一，例如 2026-10-19(測試用)")
    args = ap.parse_args()

    start = datetime.strptime(args.week, "%Y-%m-%d").date() if args.week else week_start()
    games, team_ids, day_teams = games_this_week(start)
    if not games and not args.week:  # 本週沒有例行賽(例如季前)就改看下一週
        start += timedelta(days=7)
        games, team_ids, day_teams = games_this_week(start)
    today_et = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
    weeks_ahead = max(0, (start - (today_et - timedelta(days=today_et.weekday()))).days // 7)
    if not games:
        print("本週沒有例行賽")
        return
    year = start.year + 1 if start.month >= 8 else start.year
    most = max(games.values())
    top = sorted(t for t, g in games.items() if g == most)
    # 打滿最多場的球隊太少時，也納入只少一場的球隊
    busy = top if len(top) >= 6 else sorted((t for t, g in games.items() if g >= most - 1), key=lambda t: (-games[t], t))

    # 所有有比賽的球隊名單:球員 ID → 球隊,順便給對戰預測用
    with ThreadPoolExecutor(10) as ex:
        rosters = dict(zip(team_ids, ex.map(lambda a: get_json(ROSTER.format(tid=team_ids[a])) or {}, team_ids)))
    pid_team, people = {}, []
    for abbr, r in rosters.items():
        for a in r.get("athletes", []):
            for p in (a.get("items") or [a]):
                pid_team[str(p["id"])] = abbr
                people.append((abbr, str(p["id"]), p["displayName"]))

    lg = espn_league.connect()
    me = espn_league.my_team(lg) if lg else None
    lines, boost = [], ()
    if lg:
        taken = espn_league.rostered_ids(lg)
        is_free = lambda pid, name: pid not in taken
        # 往後看下一週時(例如週日執行),對手也要抓下一週的
        opp = espn_league.opponent(lg, me, lg.currentMatchupPeriod + weeks_ahead) if me else None
        if me and opp:
            slots = active_slots(me)
            mine_p = project(me.roster, day_teams, pid_team, year, slots)
            opp_p = project(opp.roster, day_teams, pid_team, year, slots)
            res = compare(mine_p, opp_p)
            group = lambda tag: "、".join(n for n, _, _, _, t in res if t == tag) or "無"
            lines.append(f"🆚 本週對手:{opp.team_name}")
            lines.append(f"領先:{group('win')}")
            lines.append(f"拉鋸:{group('close')}  ← 這週的勝負關鍵")
            lines.append(f"落後:{group('lose')}")
            lines.append(("table", matchup_table(res)))
            lines.append(f"✅領先 ⚖️拉鋸 ❌落後;每天只計算先發 {slots} 人的數據(板凳不算);FG%、FT% 是命中率,TO 越少越好")
            lines.append("")
            boost = tuple(k for _, k, _, _, t in res if t == "close")
        elif me is None:
            lines.append(f"(找不到你的球隊「{espn_league.TEAM_NAME}」,只列自由球員)")
    else:
        drafted_path = BASE / "drafted.txt"
        drafted = set()
        if drafted_path.exists():
            drafted = {norm(l) for l in drafted_path.read_text(encoding="utf-8").splitlines()
                       if l.strip() and not l.startswith("#")}
        is_free = lambda pid, name: norm(name) not in drafted

    out = {norm(i["athlete"]["displayName"]) for t in (get_json(INJURIES) or {}).get("injuries", [])
           for i in t.get("injuries", []) if i.get("status") in ("Out", "Doubtful")}
    pool = [(abbr, pid, name) for abbr, pid, name in people
            if abbr in busy and is_free(pid, name) and norm(name) not in out]
    with ThreadPoolExecutor(16) as ex:
        stats = list(ex.map(lambda r: player_stats(r[1], year), pool))

    cands = []
    for (abbr, _, name), s in zip(pool, stats):
        if not s or s.get("min", 0) < MIN_MINUTES:
            continue
        cands.append((cade_value(s, boost) * games[abbr], name, abbr, s))
    cands.sort(key=lambda c: -c[0])

    end = start + timedelta(days=6)
    head = f"📅 本週串流 {start:%m/%d}–{end:%m/%d}"
    lines.append(f"本週打 {most} 場：" + "、".join(top))
    lines.append("")
    who = "你聯盟的自由球員" if lg else "還沒被選走"
    focus = f",拉鋸類別加權" if boost else ""
    lines.append(f"建議串流（{who}、混合今年與去年後場均 20 分鐘以上，依{BUILD_NAME} × 本週場數排序{focus}）：")
    for i, (_, name, abbr, s) in enumerate(cands[:TOP_N], 1):
        lines.append(f"{i}. {name} ({abbr}，{games[abbr]}場) {s.get('pts', 0):.1f}分 {s.get('tpm', 0):.1f}三 "
                     f"{s.get('ast', 0):.1f}助 {s.get('stl', 0):.1f}抄 {s.get('blk', 0):.1f}帽 "
                     f"罰球{s.get('ftp', 0) * 100:.0f}%")
    if not cands:
        lines.append("(沒有符合條件的人選)")
    if not lg:
        lines.append("")
        lines.append("請先到聯盟確認他們還是自由球員。")
    # lines 裡的 ("table", 文字) 是要用等寬字體顯示的表格
    blocks = [(l[1], True) if isinstance(l, tuple) else (l, False) for l in lines]
    print(head + "\n" + "\n".join(t for t, _ in blocks))
    if not args.dry:
        notify(load_config(), head, to_html(blocks), "", html=True)


if __name__ == "__main__":
    main()
