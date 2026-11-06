#!/usr/bin/env python3
"""我的球員被列為 OUT 時:移到 IR、撿能上場的自由球員,不放任何人。

--scan     跟著每 5 分鐘的傷病檢查執行。發現名單上有人 OUT 就推播候選人,每個候選人附一個 /fix 指令,
           點一下才會動手;同一次傷病只推一次。
--run      Cloudflare 門鈴收到 /fix 後由 fix.yml 執行。動手前重新檢查一次,任何條件不符就只通知、不動。

ESPN 規則:當天第一場開打後才撿的人隔天才生效;IR 只收 OUT;排先發只能排當天。
候選人照串流規則:只看本季數據、本週健康、對手排名調整最多 ±30%。
"""
import argparse
import json
from datetime import datetime, timedelta, timezone

import requests

import espn_league
import weekly_stream as w
from blend import build_value, season_stats
from nba_injury_alert import STATE_PATH, get_json, load_config, load_json, log, notify, save_json

READ = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{y}/segments/0/leagues/{lid}"
WRITE = READ.replace("lm-api-reads", "lm-api-writes") + "/transactions/"
STANDINGS = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
TEAM_STATS = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/statistics"
SLOT_ID = {"PG": 0, "SG": 1, "SF": 2, "PF": 3, "C": 4, "G": 5, "F": 6, "UT": 11, "BE": 12, "IR": 13}
IR, BE, IR_SLOTS = 13, 12, 2
SWING = 0.3      # 對手排名第 1 名 +30%,最後一名 -30%
TOP = 3          # 推播幾個候選人
ROLLOVER = 8     # ESPN 計分日約在 08:00 UTC 換日


def creds():
    s2, swid = espn_league._creds()
    return {"espn_s2": s2, "SWID": swid}, swid


def my_entries():
    """只打一次 ESPN,回傳 (我的 team id, 名單);平常每 5 分鐘的檢查不用連整個聯盟。"""
    cookies, _ = creds()
    r = requests.get(READ.format(y=espn_league.YEAR, lid=espn_league.LEAGUE_ID), params={"view": ["mTeam", "mRoster"]},
                     cookies=cookies, timeout=30)
    r.raise_for_status()
    team = next(t for t in r.json()["teams"]
                if t.get("name", "").strip().lower() == espn_league.TEAM_NAME.strip().lower())
    return team["id"], [{"id": e["playerId"], "slot": e["lineupSlotId"], "name": e["playerPoolEntry"]["player"]["fullName"],
                         "status": e["playerPoolEntry"]["player"].get("injuryStatus")}
                        for e in team["roster"]["entries"]]


def post(body):
    cookies, swid = creds()
    r = requests.post(WRITE.format(y=espn_league.YEAR, lid=espn_league.LEAGUE_ID), cookies=cookies, timeout=30,
                      json={"isLeagueManager": False, "memberId": swid, "executionType": "EXECUTE", **body})
    return r.status_code, r.text[:300]


def player_status(lg, pid):
    r = lg.espn_request.league_get(params={"view": "kona_player_info"}, headers={
        "x-fantasy-filter": json.dumps({"players": {"filterIds": {"value": [pid]}}})})
    return ((r.get("players") or [{}])[0]).get("status")


def schedule():
    """回傳 (本週一的美國日期, 今天第幾天, 第一場是否已開打, 每天的 {球隊: 對手}, 球隊 ID)。"""
    now = datetime.now(timezone.utc)
    us_today = (now - timedelta(hours=ROLLOVER)).date()
    start = us_today - timedelta(days=us_today.weekday())
    opp, tids, started = [{} for _ in range(7)], {}, False
    for i in range(7):
        d = start + timedelta(days=i)
        for ev in (get_json(w.SCOREBOARD.format(d=d.strftime("%Y%m%d"))) or {}).get("events", []):
            cs = ev["competitions"][0]["competitors"]
            a, b = cs[0]["team"], cs[1]["team"]
            opp[i][a["abbreviation"]], opp[i][b["abbreviation"]] = b["abbreviation"], a["abbreviation"]
            tids[a["abbreviation"]], tids[b["abbreviation"]] = a["id"], b["id"]
            if d == us_today and datetime.fromisoformat(ev["date"].replace("Z", "+00:00")) <= now:
                started = True
    return start, us_today.weekday(), started, opp, tids


def adds_left(lg, team_id, today):
    """每個計分日累積 1 次,這週已用的從 ESPN 讀。"""
    d = lg.espn_request.league_get(params={"view": "mTeam"})
    t = next(x for x in d["teams"] if x["id"] == team_id)
    used = t["transactionCounter"]["matchupAcquisitionTotals"].get(str(lg.currentMatchupPeriod), 0)
    return today + 1 - used


def opponent_factors(tids):
    """對手失分排名(影響得分類)和失誤排名(影響抄截)換算成係數。"""
    st = get_json(STANDINGS) or {}
    pa = {e["team"]["abbreviation"]: {x["name"]: x.get("value") for x in e["stats"]}["avgPointsAgainst"]
          for c in st.get("children", []) for e in c["standings"]["entries"]}

    def tov(a):
        t = get_json(TEAM_STATS.format(tid=tids[a])) or {}
        for c in t.get("results", {}).get("stats", {}).get("categories", []):
            for x in c["stats"]:
                if x["name"] == "avgTurnovers":
                    return float(x["value"])
        return 0.0

    to = {a: tov(a) for a in tids}
    rank = lambda d: {a: i for i, a in enumerate(sorted(d, key=lambda k: -d[k]))}
    scale = lambda r, n: 1 + SWING * (n - 1 - 2 * r) / (n - 1)
    pr, tr = rank(pa), rank(to)
    return {a: scale(pr[a], len(pr)) for a in pr}, {a: scale(tr[a], len(tr)) for a in tr}


def game_value(s, oppo, f_off, f_stl):
    t = dict(s)
    for k in ("pts", "tpm", "ast", "fga", "fta"):
        t[k] = s[k] * f_off.get(oppo, 1)
    t["stl"] = s["stl"] * f_stl.get(oppo, 1)
    return build_value(t)


def candidates(lg, opp, days, tids):
    """本週剩下幾天有比賽、健康的自由球員,依剩下比賽的總價值排序。"""
    teams = {}
    for a, tid in tids.items():
        for x in (get_json(w.ROSTER.format(tid=tid)) or {}).get("athletes", []):
            for p in x.get("items") or [x]:
                teams[str(p["id"])] = a
    taken = espn_league.rostered_ids(lg)
    f_off, f_stl = opponent_factors(tids)
    out = []
    for p in lg.free_agents(size=120):
        team = teams.get(str(p.playerId))
        if str(p.playerId) in taken or (p.injuryStatus or "ACTIVE") not in ("ACTIVE", "NORMAL"):
            continue
        games = [(i, opp[i][team]) for i in days if team in opp[i]]
        s = season_stats(p.playerId, espn_league.YEAR, get_json) if games else None
        if not s or s["gp"] < 3 or s["min"] < 18:
            continue
        out.append((sum(game_value(s, o, f_off, f_stl) for _, o in games), p, games, s))
    out.sort(key=lambda r: -r[0])
    picks = []
    for row in out:
        if player_status(lg, row[1].playerId) == "FREEAGENT":
            picks.append(row)
        if len(picks) == TOP:
            break
    return picks


def scan():
    cfg = load_config()
    state = load_json(STATE_PATH, {})
    offers = state.get("fix_offers", {})
    team_id, roster = my_entries()
    out = {str(p["id"]): p for p in roster if p["status"] == "OUT" and p["slot"] != IR}
    fresh = [p for k, p in out.items() if k not in offers]
    keep = {k: v for k, v in offers.items() if k in out}  # 回到健康就清掉,下次受傷才會再推
    if not fresh:
        if keep != offers:
            state["fix_offers"] = keep
            save_json(STATE_PATH, state)
        return
    p = fresh[0]
    head, body = f"🚑 {p['name']} OUT", offer_text(p, team_id, roster)
    print(head, body, flush=True)
    notify(cfg, head, body, f"{p['name']} OUT")
    keep[str(p["id"])] = datetime.now(timezone.utc).isoformat()
    state["fix_offers"] = keep
    save_json(STATE_PATH, state)


def offer_text(p, team_id, roster):
    if sum(x["slot"] == IR for x in roster) >= IR_SLOTS:
        return "IR 已滿,要撿人就得放人,請告訴 Claude 要放誰。"
    lg = espn_league.connect()
    espn_league.refresh_build(lg)
    _, today, started, opp, tids = schedule()
    left = adds_left(lg, team_id, today)
    if left <= 0:
        return "這週撿人額度用完了。可以先移到 IR,但撿不了人。"
    first = today + 1 if started else today
    days = range(first, 7)
    note = "今天第一場已開打,現在撿隔天才生效。" if started else "今天第一場還沒開打,現在撿今天就能上。"
    picks = candidates(lg, opp, days, tids)
    if not picks:
        return f"本週剩下的日子沒有合適的自由球員。{note}"
    lines = [note, f"這週還能撿 {left} 次。點指令才會動手:移 {p['name']} 到 IR、撿人,不放任何人。", ""]
    for total, c, games, s in picks:
        lines.append(f"{c.name}({'/'.join(x for x in ('PG', 'SG', 'SF', 'PF', 'C') if x in c.eligibleSlots)})"
                     f" 剩 {'、'.join('一二三四五六日'[i] + o for i, o in games)}")
        lines.append(f"  {s['pts']:.1f}分 {s['reb']:.1f}板 {s['ast']:.1f}助 {s['stl']:.1f}抄 {s['blk']:.1f}帽 "
                     f"{s['tpm']:.1f}三分 {s['min']:.0f}分鐘 總價值{total:.2f}")
        lines.append(f"  /fix_{p['id']}_{c.playerId}")
    return "\n".join(lines)


def match(players, slots):
    """players 依優先順序;回傳 {playerId: 位置名稱},用二分配對讓前面的人一定排得進去。"""
    owner = [None] * len(slots)

    def place(i, seen):
        for j, slot in enumerate(slots):
            if slot in players[i].eligibleSlots and j not in seen:
                seen.add(j)
                if owner[j] is None or place(owner[j], seen):
                    owner[j] = i
                    return True
        return False

    for i in range(len(players)):
        place(i, set())
    return {players[i].playerId: slots[j] for j, i in enumerate(owner) if i is not None}


def set_lineup(lg, team_id, opp, today, tids):
    """把今天有比賽的人排進先發(只在第一場開打前呼叫,沒有人被鎖住)。"""
    teams = {}
    for a, tid in tids.items():
        for x in (get_json(w.ROSTER.format(tid=tid)) or {}).get("athletes", []):
            for p in x.get("items") or [x]:
                teams[str(p["id"])] = a
    me = next(t for t in espn_league.connect().teams if t.team_id == team_id)
    active = [p for p in me.roster if p.lineupSlot != "IR"]
    plays = lambda p: teams.get(str(p.playerId)) in opp[today] and (p.injuryStatus or "").upper() != "OUT"
    value = lambda p: build_value(season_stats(p.playerId, espn_league.YEAR, get_json) or {}) if plays(p) else 0
    order = sorted(active, key=lambda p: (not plays(p), -value(p)))
    want = match(order, w.LINEUP)
    want.update({p.playerId: "BE" for p in active if p.playerId not in want})
    items = [{"playerId": p.playerId, "type": "LINEUP", "fromLineupSlotId": SLOT_ID[p.lineupSlot],
              "toLineupSlotId": SLOT_ID[want[p.playerId]]} for p in active if want[p.playerId] != p.lineupSlot]
    if not items:
        return 200, ""
    return post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": lg.scoringPeriodId, "items": items})


def run(out_id, add_id):
    cfg = load_config()
    stop = lambda why: (print(why, flush=True), notify(cfg, "⚠️ 沒有換人", why, why[:120]))
    team_id, roster = my_entries()
    p = next((x for x in roster if x["id"] == out_id), None)
    if not p or p["status"] != "OUT" or p["slot"] == IR:
        return stop(f"{p['name'] if p else out_id} 現在不是 OUT 或已在 IR,沒有動作。")
    if sum(x["slot"] == IR for x in roster) >= IR_SLOTS:
        return stop("IR 已滿,沒有動作。")
    lg = espn_league.connect()
    name = lg.player_map.get(add_id, add_id)
    status = player_status(lg, add_id)
    if status != "FREEAGENT":
        return stop(f"{name} 現在不是自由球員({status}),沒有動作。")
    _, today, started, opp, tids = schedule()
    if adds_left(lg, team_id, today) <= 0:
        return stop("這週撿人額度用完了,沒有動作。")
    code, text = post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": lg.scoringPeriodId,
                       "items": [{"playerId": out_id, "type": "LINEUP", "fromLineupSlotId": p["slot"], "toLineupSlotId": IR}]})
    if code >= 300:
        return stop(f"移到 IR 失敗({code}):{text}\n沒有撿人。")
    code, text = post({"teamId": team_id, "type": "FREEAGENT", "scoringPeriodId": lg.scoringPeriodId,
                       "items": [{"playerId": add_id, "type": "ADD", "toTeamId": team_id}]})
    if code >= 300:
        return stop(f"{p['name']} 已移到 IR,但撿 {name} 失敗({code}):{text}")
    if started:
        msg = f"{p['name']} 已移到 IR。{name} 今天第一場開打後才撿,隔天才生效,先發要隔天再排。"
    else:
        code, text = set_lineup(lg, team_id, opp, today, tids)
        msg = f"{p['name']} 已移到 IR,今天有比賽的人已排進先發。" if code < 300 else \
            f"{p['name']} 已移到 IR,但排先發失敗({code}),請手動排 {name}。"
    print(msg, flush=True)
    notify(cfg, f"✅ 已撿 {name}", msg, msg[:120])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--run", nargs=2, type=int, metavar=("OUT_ID", "ADD_ID"))
    ap.add_argument("--preview", help="測試:假裝這位球員 OUT,只印出推播內容,不寄送也不動 ESPN")
    args = ap.parse_args()
    if args.preview:
        team_id, roster = my_entries()
        p = next(x for x in roster if x["name"] == args.preview)
        print(offer_text(p, team_id, roster))
    elif args.run:
        run(*args.run)
    elif args.scan:
        scan()


if __name__ == "__main__":
    main()
