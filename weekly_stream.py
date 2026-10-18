#!/usr/bin/env python3
"""每週串流推播

列出本週(美東週一到週日)每隊比賽場數，並從比賽最多的球隊裡，
挑出還沒被聯盟選走、符合 Cade 打法(得分、3PM、罰球、助攻、抄截；不計失誤)的串流人選，
推播到 Telegram。已被選走的球員寫在 drafted.txt。

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

from nba_injury_alert import get_json, load_config, notify

BASE = Path(__file__).resolve().parent
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}"
ROSTER = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster"
STATS = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
         "seasons/{year}/types/2/athletes/{pid}/statistics")
INJURIES = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
KEYS = {"gamesPlayed": "gp", "avgMinutes": "min", "avgPoints": "pts", "avgRebounds": "reb",
        "avgAssists": "ast", "avgSteals": "stl", "avgBlocks": "blk",
        "avgThreePointFieldGoalsMade": "tpm", "avgFreeThrowsAttempted": "fta", "freeThrowPct": "ftp",
        "fieldGoalPct": "fgp"}
TOP_N = 8
MIN_MINUTES = 20


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
    """本季出賽 5 場以上用本季，否則用上一季。"""
    cur = season_stats(pid, year)
    if cur and cur["gp"] >= 5:
        return cur
    return season_stats(pid, year - 1) or cur


def cade_value(s):
    """Cade 打法的單場價值：得分、3PM、罰球、助攻、抄截為主，失誤不計。"""
    ft_impact = (s.get("ftp", 0.78) - 0.78) * s.get("fta", 0)
    return (s.get("pts", 0) * 1.0 + s.get("tpm", 0) * 3.0 + s.get("ast", 0) * 1.2
            + s.get("stl", 0) * 2.5 + s.get("reb", 0) * 0.4 + s.get("blk", 0) * 1.0 + ft_impact * 6)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--week", help="指定該週週一，例如 2026-10-19(測試用)")
    args = ap.parse_args()

    start = datetime.strptime(args.week, "%Y-%m-%d").date() if args.week else week_start()
    games, team_ids = games_this_week(start)
    if not games:
        print("本週沒有例行賽")
        return
    year = start.year + 1 if start.month >= 8 else start.year
    most = max(games.values())
    top = sorted(t for t, g in games.items() if g == most)
    # 打滿最多場的球隊太少時，也納入只少一場的球隊
    busy = top if len(top) >= 6 else sorted((t for t, g in games.items() if g >= most - 1), key=lambda t: (-games[t], t))

    drafted_path = BASE / "drafted.txt"
    drafted = set()
    if drafted_path.exists():
        drafted = {norm(l) for l in drafted_path.read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.startswith("#")}
    out = {norm(i["athlete"]["displayName"]) for t in (get_json(INJURIES) or {}).get("injuries", [])
           for i in t.get("injuries", []) if i.get("status") in ("Out", "Doubtful")}

    roster = []
    with ThreadPoolExecutor(10) as ex:
        for abbr, r in zip(busy, ex.map(lambda a: get_json(ROSTER.format(tid=team_ids[a])) or {}, busy)):
            for a in r.get("athletes", []):
                for p in (a.get("items") or [a]):
                    k = norm(p["displayName"])
                    if k not in drafted and k not in out:
                        roster.append((abbr, p["id"], p["displayName"]))
    with ThreadPoolExecutor(16) as ex:
        stats = list(ex.map(lambda r: player_stats(r[1], year), roster))

    cands = []
    for (abbr, _, name), s in zip(roster, stats):
        if not s or s.get("min", 0) < MIN_MINUTES:
            continue
        cands.append((cade_value(s) * games[abbr], name, abbr, s))
    cands.sort(key=lambda c: -c[0])

    end = start + timedelta(days=6)
    head = f"📅 本週串流 {start:%m/%d}–{end:%m/%d}"
    lines = [f"本週打 {most} 場：" + "、".join(top)]
    lines.append("")
    lines.append("建議串流（還沒被選走、場均 20 分鐘以上，依 Cade 打法 × 本週場數排序）：")
    for i, (_, name, abbr, s) in enumerate(cands[:TOP_N], 1):
        lines.append(f"{i}. {name} ({abbr}，{games[abbr]}場) {s.get('pts', 0):.1f}分 {s.get('tpm', 0):.1f}三 "
                     f"{s.get('ast', 0):.1f}助 {s.get('stl', 0):.1f}抄 罰球{s.get('ftp', 0) * 100:.0f}%")
    if not cands:
        lines.append("(沒有符合條件的人選)")
    lines.append("")
    lines.append("請先到聯盟確認他們還是自由球員。")
    body = "\n".join(lines)
    print(head + "\n" + body)
    if not args.dry:
        notify(load_config(), head, body, "")


if __name__ == "__main__":
    main()
