#!/usr/bin/env python3
"""每週串流推播

列出本週(美東週一到週日)每隊比賽場數，並從比賽最多的球隊裡，
挑出撿得到、符合 Cade 打法(得分、3PM、罰球、助攻、抄截；不計失誤)的串流人選，推播到 Telegram。

有接上 ESPN 聯盟(ESPN_S2、ESPN_SWID)時：
- 只推薦聯盟裡真正的自由球員
- 加上「你 vs 本週對手」的九類預測，串流人選會偏向拉鋸中的類別
沒接上時，改用 drafted.txt 排除已被選走的人。

用法:
    python weekly_stream.py          推播本週(週日執行時改看下一週)
    python weekly_stream.py --dry    只印出，不推播
"""
import argparse
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import espn_league
from blend import blend
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
    games, team_ids = {}, {}
    days = [start + timedelta(days=i) for i in range(7)]
    with ThreadPoolExecutor(7) as ex:
        boards = list(ex.map(lambda d: get_json(SCOREBOARD.format(d=d.strftime("%Y%m%d"))) or {}, days))
    for b in boards:
        for e in b.get("events", []):
            if e.get("season", {}).get("type") != 2:
                continue
            for c in e["competitions"][0]["competitors"]:
                abbr = c["team"]["abbreviation"]
                games[abbr] = games.get(abbr, 0) + 1
                team_ids[abbr] = c["team"]["id"]
    return games, team_ids


def season_stats(pid, year):
    d = get_json(STATS.format(year=year, pid=pid))
    if not d:
        return None
    s = {}
    for cat in d.get("splits", {}).get("categories", []):
        for x in cat.get("stats", []):
            if x["name"] in KEYS:
                s[KEYS[x["name"]]] = float(x["value"])
    for k in ("ftp", "fgp"):
        if s.get(k, 0) > 1:
            s[k] /= 100
    return s if s.get("gp") else None


def player_stats(pid, year):
    """今年和去年混合評估(見 blend.py):上場時間最快反映今年角色,產量和命中率逐場增加今年的權重。"""
    return blend(season_stats(pid, year), season_stats(pid, year - 1))


def cade_value(s, boost=()):
    """Cade 打法的單場價值：得分、3PM、罰球、助攻、抄截為主，失誤不計。
    boost 是本週拉鋸中的類別，權重加倍。"""
    b = lambda k: 2.0 if k in boost else 1.0
    ft_impact = (s.get("ftp", 0.78) - 0.78) * s.get("fta", 0)
    fg_impact = (s.get("fgp", 0.47) - 0.47) * s.get("fga", 0)
    return (s.get("pts", 0) * 1.0 * b("pts") + s.get("tpm", 0) * 3.0 * b("tpm")
            + s.get("ast", 0) * 1.2 * b("ast") + s.get("stl", 0) * 2.5 * b("stl")
            + s.get("reb", 0) * 0.4 * b("reb") + s.get("blk", 0) * 1.0 * b("blk")
            + ft_impact * 6 * b("ft") + fg_impact * 3 * (b("fg") - 1))


def project(players, games, pid_team, year):
    """一隊本週的預估九類總量(場均 × 本週場數;IR 和確定缺陣的不算)。"""
    tot = dict.fromkeys(["pts", "reb", "ast", "stl", "blk", "tpm", "tov", "fga", "fgm", "fta", "ftm"], 0.0)
    for p in players:
        if p.lineupSlot == "IR" or (p.injuryStatus or "").upper() == "OUT":
            continue
        g = games.get(pid_team.get(str(p.playerId)), 0)
        s = player_stats(p.playerId, year) if g else None
        if not s:
            continue
        for k in ("pts", "reb", "ast", "stl", "blk", "tpm", "tov"):
            tot[k] += s.get(k, 0) * g
        tot["fga"] += s.get("fga", 0) * g
        tot["fgm"] += s.get("fga", 0) * s.get("fgp", 0) * g
        tot["fta"] += s.get("fta", 0) * g
        tot["ftm"] += s.get("fta", 0) * s.get("ftp", 0) * g
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
    return f"{v * 100:.1f}%" if k in ("fg", "ft") else f"{v:.0f}"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--week", help="指定該週週一，例如 2026-10-19(測試用)")
    args = ap.parse_args()

    start = datetime.strptime(args.week, "%Y-%m-%d").date() if args.week else week_start()
    games, team_ids = games_this_week(start)
    if not games and not args.week:  # 本週沒有例行賽(例如季前)就改看下一週
        start += timedelta(days=7)
        games, team_ids = games_this_week(start)
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
        opp = espn_league.opponent(lg, me) if me else None
        if me and opp:
            mine_p = project(me.roster, games, pid_team, year)
            opp_p = project(opp.roster, games, pid_team, year)
            res = compare(mine_p, opp_p)
            group = lambda tag: "、".join(n for n, _, _, _, t in res if t == tag) or "無"
            lines.append(f"🆚 本週對手:{opp.team_name}")
            lines.append(f"領先:{group('win')}")
            lines.append(f"拉鋸:{group('close')}  ← 這週的勝負關鍵")
            lines.append(f"落後:{group('lose')}")
            lines.append("預估(你/對手):" + " ".join(f"{n} {fmt(k, a)}/{fmt(k, b)}" for n, k, a, b, _ in res))
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
    lines.append(f"建議串流（{who}、混合今年與去年後場均 20 分鐘以上，依 Cade 打法 × 本週場數排序{focus}）：")
    for i, (_, name, abbr, s) in enumerate(cands[:TOP_N], 1):
        lines.append(f"{i}. {name} ({abbr}，{games[abbr]}場) {s.get('pts', 0):.1f}分 {s.get('tpm', 0):.1f}三 "
                     f"{s.get('ast', 0):.1f}助 {s.get('stl', 0):.1f}抄 {s.get('blk', 0):.1f}帽 "
                     f"罰球{s.get('ftp', 0) * 100:.0f}%")
    if not cands:
        lines.append("(沒有符合條件的人選)")
    if not lg:
        lines.append("")
        lines.append("請先到聯盟確認他們還是自由球員。")
    body = "\n".join(lines)
    print(head + "\n" + body)
    if not args.dry:
        notify(load_config(), head, body, "")


if __name__ == "__main__":
    main()
