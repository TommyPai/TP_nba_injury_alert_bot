#!/usr/bin/env python3
"""每日自由球員關注名單,推播到 Telegram。

有接上 ESPN 聯盟(ESPN_S2、ESPN_SWID)時只列聯盟裡的自由球員,否則用 drafted.txt 排除。
三段:
  📈 角色上升:今年上場時間比去年多 5 分鐘以上(最早的爆發訊號)
  📅 今天有比賽:當天串流用
  ⭐ 整體最佳:今年 + 去年混合數據,依 Cade 打法(得分、3PM、罰球、助攻、抄截)排序,去年出賽少的打折

    python daily_watch.py          推播
    python daily_watch.py --dry    只印出
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
from blend import blend, cade_per_min, season_stats
from nba_injury_alert import get_json, load_config, notify

BASE = Path(__file__).resolve().parent
ROSTER = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}"
INJURIES = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
MIN_MINUTES = 18      # 混合後場均分鐘下限
RISE_MIN = 5          # 角色上升:今年比去年多幾分鐘
RISE_FLOOR = 22       # 角色上升:今年至少要打幾分鐘
TOP_N = 8


def norm(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"[^a-z]", "", n)


def short(name, n=18):
    return name if len(name) <= n else name[: n - 1] + "."


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()  # 美東日期
    year = today.year + 1 if today.month >= 8 else today.year

    # 今天有比賽的球隊
    board = get_json(SCOREBOARD.format(d=today.strftime("%Y%m%d"))) or {}
    playing = {c["team"]["abbreviation"] for e in board.get("events", [])
               if e.get("season", {}).get("type") == 2
               for c in e["competitions"][0]["competitors"]}

    # 全聯盟球員
    with ThreadPoolExecutor(10) as ex:
        rosters = list(ex.map(lambda t: get_json(ROSTER.format(tid=t)) or {}, range(1, 31)))
    people = []
    for r in rosters:
        abbr = (r.get("team") or {}).get("abbreviation", "")
        for a in r.get("athletes", []):
            for p in (a.get("items") or [a]):
                people.append((abbr, str(p["id"]), p["displayName"]))

    # 自由球員判斷
    lg = espn_league.connect()
    if lg:
        taken = espn_league.rostered_ids(lg)
        is_free = lambda pid, name: pid not in taken
    else:
        path = BASE / "drafted.txt"
        drafted = {norm(l) for l in path.read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.startswith("#")} if path.exists() else set()
        is_free = lambda pid, name: norm(name) not in drafted
    out = {norm(i["athlete"]["displayName"]) for t in (get_json(INJURIES) or {}).get("injuries", [])
           for i in t.get("injuries", []) if i.get("status") in ("Out", "Doubtful")}
    pool = [x for x in people if is_free(x[1], x[2]) and norm(x[2]) not in out]

    def stats(pid):
        cur, prev = season_stats(pid, year, get_json), season_stats(pid, year - 1, get_json)
        return cur, prev, blend(cur, prev)
    with ThreadPoolExecutor(16) as ex:
        data = list(ex.map(lambda x: stats(x[1]), pool))

    rows = []
    for (abbr, pid, name), (cur, prev, b) in zip(pool, data):
        if not b or b.get("min", 0) < MIN_MINUTES:
            continue
        # 健康優先:去年出賽少的人打折(打滿 70 場不打折,最多打 7 折)
        health = 0.7 + 0.3 * min(1.0, (prev["gp"] if prev else 40) / 70)
        value = cade_per_min(b) * b["min"] * health
        rows.append({"name": name, "team": abbr, "cur": cur, "prev": prev, "b": b, "value": value})
    rows.sort(key=lambda r: -r["value"])

    def line(r):
        b, prev = r["b"], r["prev"]
        g = f"去年{prev['gp']:.0f}場" if prev else "去年無數據"
        return (f"{short(r['name'])}({r['team']}) {b['min']:.0f}分鐘 {b['pts']:.1f}分 {b['tpm']:.1f}三 "
                f"{b['ast']:.1f}助 {b['stl']:.1f}抄 罰{b.get('ftp', 0) * 100:.0f}% | {g}")

    rising = [r for r in rows if r["cur"] and r["prev"] and r["cur"]["gp"] >= 2
              and r["cur"]["min"] >= RISE_FLOOR and r["cur"]["min"] - r["prev"]["min"] >= RISE_MIN]
    tonight = [r for r in rows if r["team"] in playing]

    head = f"👀 自由球員每日關注 {today:%m/%d}"
    lines = []
    lines.append("📈 角色上升(今年上場時間比去年多 5 分鐘以上)")
    if rising:
        for r in rising[:6]:
            lines.append(f"• {r['name']}({r['team']}) {r['prev']['min']:.0f}→{r['cur']['min']:.0f}分鐘,"
                         f"今年{r['cur']['gp']:.0f}場 {r['cur']['pts']:.1f}分 {r['cur']['tpm']:.1f}三 "
                         f"{r['cur']['ast']:.1f}助 {r['cur']['stl']:.1f}抄")
    else:
        lines.append("• 目前沒有")
    lines.append("")
    lines.append(f"📅 今天有比賽({len(playing) // 2} 場)" if playing else "📅 今天沒有比賽")
    for i, r in enumerate(tonight[:5], 1):
        lines.append(f"{i}. {line(r)}")
    lines.append("")
    lines.append("⭐ 整體最佳(Cade 打法,今年+去年混合,健康優先)")
    for i, r in enumerate(rows[:TOP_N], 1):
        lines.append(f"{i}. {line(r)}")
    if not rows:
        lines.append("(沒有符合條件的人選)")
    lines.append("")
    lines.append("已排除確定缺陣、極可能缺陣的人" + ("" if lg else ";請先到聯盟確認還是自由球員"))

    print(head + "\n" + "\n".join(lines))
    if not args.dry:
        notify(load_config(), head, html.escape("\n".join(lines)), "", html=True)


if __name__ == "__main__":
    main()
