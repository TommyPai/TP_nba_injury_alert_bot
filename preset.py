#!/usr/bin/env python3
"""先把指定美國日期的陣容排好:當天有比賽、不是 OUT 的人全部排先發(價值高的優先),其餘坐板凳;IR 不動。
今天用 ROSTER(要在第一場開打前),之後的日子用 FUTURE_ROSTER。
用法:python3 preset.py 2026-11-09 2026-11-15 [--dry] [--bench "Keon Ellis"]"""
import argparse
from datetime import date, timedelta

import requests

import auto_fix as a
import espn_league
import weekly_stream as w
from blend import build_value, season_stats
from nba_injury_alert import get_json

BASE_DAY, BASE_SP = date(2026, 11, 8), 20  # 美國 11/8 = 計分日 20
STARTERS = [0, 1, 2, 3, 4, 5, 6, 11, 11, 11]  # PG SG SF PF C G F UT UT UT
NAME = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 11: "UT", 12: "BE", 13: "IR"}
_val = {}


def roster_at(sp=None):
    """sp=None 讀今天;回傳 (ESPN 今天的計分日, team id, 那天的名單)。"""
    cookies, _ = a.creds()
    r = requests.get(a.READ.format(y=espn_league.YEAR, lid=espn_league.LEAGUE_ID), cookies=cookies, timeout=30,
                     params={"view": ["mTeam", "mRoster"], **({"scoringPeriodId": sp} if sp else {})})
    r.raise_for_status()
    js = r.json()
    team = next(t for t in js["teams"] if t.get("name", "").strip().lower() == espn_league.TEAM_NAME.strip().lower())
    return js.get("scoringPeriodId"), team["id"], team["roster"]["entries"]


def value(pid):
    if pid not in _val:
        _val[pid] = build_value(season_stats(pid, espn_league.YEAR, get_json) or {})
    return _val[pid]


def match(order):
    owner = [None] * len(STARTERS)
    slots = lambda e: e["playerPoolEntry"]["player"]["eligibleSlots"]

    def place(i, seen):
        for j, s in enumerate(STARTERS):
            if s in slots(order[i]) and j not in seen:
                seen.add(j)
                if owner[j] is None or place(owner[j], seen):
                    owner[j] = i
                    return True
        return False

    for i in range(len(order)):
        place(i, set())
    return {order[i]["playerId"]: STARTERS[j] for j, i in enumerate(owner) if i is not None}


def preset(days, bench=(), dry=False):
    """回傳每天的 (美國日期, 計分日, ESPN 狀態碼, 訊息, 有比賽的先發, 有比賽卻沒排進去的人)。"""
    out = []
    today = roster_at()[0]
    for d in days:
        sp = BASE_SP + (d - BASE_DAY).days
        events = (get_json(w.SCOREBOARD.format(d=d.strftime("%Y%m%d"))) or {}).get("events", [])
        tids = {str(c["team"]["id"]) for ev in events for c in ev["competitions"][0]["competitors"]}
        _, team_id, entries = roster_at(sp)
        act = [e for e in entries if e["lineupSlotId"] != a.IR]
        pl = lambda e: e["playerPoolEntry"]["player"]
        plays = lambda e: (str(pl(e)["proTeamId"]) in tids and (pl(e).get("injuryStatus") or "") != "OUT"
                           and pl(e)["fullName"] not in bench)
        order = sorted(act, key=lambda e: (not plays(e), -(value(e["playerId"]) if plays(e) else 0)))
        want = match(order)
        items = [{"playerId": e["playerId"], "type": "LINEUP", "fromLineupSlotId": e["lineupSlotId"],
                  "toLineupSlotId": want.get(e["playerId"], a.BE)}
                 for e in act if want.get(e["playerId"], a.BE) != e["lineupSlotId"]]
        starters = [f"{pl(e)['fullName']}({NAME[want[e['playerId']]]})" for e in act if plays(e) and e["playerId"] in want]
        missed = [pl(e)["fullName"] for e in act if plays(e) and e["playerId"] not in want]
        code, text = 200, ""
        if items and not dry:
            code, text = a.post({"teamId": team_id, "type": "ROSTER" if sp == today else "FUTURE_ROSTER",
                                 "scoringPeriodId": sp, "items": items})
        out.append((d, sp, code, text, starters, missed))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=date.fromisoformat)
    ap.add_argument("end", type=date.fromisoformat)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--bench", action="append", default=[])
    args = ap.parse_args()
    days = [args.start + timedelta(i) for i in range((args.end - args.start).days + 1)]
    for d, sp, code, text, starters, missed in preset(days, args.bench, args.dry):
        print(d, f"sp{sp}", code, text, "| 先發:", "、".join(starters), "| 沒排進:", missed or "無", flush=True)


if __name__ == "__main__":
    main()
