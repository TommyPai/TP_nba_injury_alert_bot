#!/usr/bin/env python3
"""本週串流建議(Tommy 11/23 定案:核心 + 拉鋸串流)。只印建議,不送任何交易。

1. 分級(選人分數 pick_value):本季 ≥0.50 核心;0.42–0.50 觀察(近 5 場 ≥0.50 就留);其餘串流位。
2. 本週實際累計 + 剩下幾天預估(傷兵依 ESPN 預計回歸日加入,先發依價值排進合法位置),找出拉鋸類別。
3. 每個「自由球員 × 串流位的人」組合重跑九類模擬,依「預期贏的類別數」E = Σ Φ(差距 / σ) 的增加量排序
   (Tommy 11/23 同意:取代拉鋸二分法)。這等於每類用 φ(z) 加權:越接近五五波的類別越重要,
   已經穩贏/穩輸的類別自動降權,場數(能排進先發的才算)和翻盤風險都已包含在內。
4. σ = 各隊每週成績扣掉該隊平均後的標準差(第 2–5 週回測)× √2(兩隊)× √(本週剩餘比例)。
限制:原始位置 C 最多 4 人(auto_fix.pos_full)、本週撿人額度、週一~週四留 1 次。
用法:py stream_week.py [--top 10] [--today 2026-11-24]
"""
import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import auto_fix as a
import preset
import espn_league as e
import stream_chain as S
import weekly_stream as w
from blend import pick_value, season_stats
from nba_injury_alert import get_json

CORE, WATCH = 0.50, 0.42
INJ = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
GAMELOG = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{pid}/gamelog"
ALL = ["PG", "SG", "SF", "PF", "C", "G", "F", "UT"]
SLOT_NAME = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 11: "UT"}
# 單隊每週成績的隊內標準差(第 2–5 週,扣掉各隊自己的平均;11/23 回測)。每 3–4 週重算一次,不要每週改。
SIGMA = {"pts": 64.9, "reb": 25.6, "ast": 17.0, "stl": 7.1, "blk": 6.3, "tpm": 10.4, "tov": 8.5,
         "fg": 0.022, "ft": 0.045}
CATS = ("pts", "reb", "ast", "stl", "blk", "tpm", "fg", "ft", "tov")
PHI = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))


def win_probs(me, opp, frac):
    """每類 (z, 贏的機率)。frac = 本週還沒打的比例。"""
    out = {}
    for k in CATS:
        d = (opp[k] - me[k]) if k == "tov" else (me[k] - opp[k])
        z = d / (SIGMA[k] * math.sqrt(2) * math.sqrt(max(frac, 0.02)))
        out[k] = (z, PHI(z))
    return out


def expected_wins(me, opp, frac):
    return sum(p for _, p in win_probs(me, opp, frac).values())


def last_n(pid, n=5):
    """近 n 場平均(例行賽)。"""
    d = get_json(GAMELOG.format(pid=pid)) or {}
    names, evs, rows = d.get("names") or [], d.get("events", {}), []
    for st in d.get("seasonTypes", []):
        if "Preseason" in st.get("displayName", ""):
            continue
        for cat in st.get("categories", []):
            for ev in cat.get("events", []):
                rows.append((evs.get(ev["eventId"], {}).get("gameDate", ""), dict(zip(names, ev["stats"]))))
    rows = sorted(rows, key=lambda x: x[0])[-n:]
    if not rows:
        return None
    t = dict.fromkeys(("min", "pts", "reb", "ast", "stl", "blk", "tpm", "fga", "fgm", "fta", "ftm", "tov"), 0.0)
    for _, s in rows:
        fg = s["fieldGoalsMade-fieldGoalsAttempted"].split("-")
        ft = s["freeThrowsMade-freeThrowsAttempted"].split("-")
        tp = s["threePointFieldGoalsMade-threePointFieldGoalsAttempted"].split("-")
        for k, v in (("min", s["minutes"]), ("pts", s["points"]), ("reb", s["totalRebounds"]), ("ast", s["assists"]),
                     ("stl", s["steals"]), ("blk", s["blocks"]), ("tov", s["turnovers"]), ("tpm", tp[0]),
                     ("fgm", fg[0]), ("fga", fg[1]), ("ftm", ft[0]), ("fta", ft[1])):
            t[k] += float(v or 0)
    g = len(rows)
    o = {k: v / g for k, v in t.items()}
    o["fgp"] = t["fgm"] / t["fga"] if t["fga"] else 0
    o["ftp"] = t["ftm"] / t["fta"] if t["fta"] else 0
    o["gp"] = g
    return o


def tier(s, r):
    """近 5 場比季分數高 0.10 以上時,要上場時間也多 3 分鐘以上才算角色變了(否則當雜訊)。"""
    v, vr = pick_value(s), pick_value(r) if r else 0
    real = vr >= CORE and (vr - v < 0.10 or (r and r["min"] >= s["min"] + 3))
    if v >= CORE:
        return "核心"
    if v >= WATCH:
        return "觀察(留)" if real else "觀察"
    return "觀察(留)" if real else "串流"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--today", type=date.fromisoformat)
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    us_today = args.today or (now - timedelta(hours=a.ROLLOVER)).date()
    W0, W1 = S.WEEK
    _, team_ids, days = w.games_this_week(W0)
    i0 = (us_today - W0).days
    ev = (get_json(w.SCOREBOARD.format(d=us_today.strftime("%Y%m%d"))) or {}).get("events", [])
    if ev and min(datetime.fromisoformat(x["date"].replace("Z", "+00:00")) for x in ev) <= now:
        i0 += 1  # 今天已開打:今天的數據算在實際累計裡(進行中的比賽只算一部分)
    with ThreadPoolExecutor(10) as ex:
        rs = list(ex.map(lambda t: get_json(w.ROSTER.format(tid=team_ids[t])) or {}, team_ids))
    pid_team = {str(p["id"]): r["team"]["abbreviation"] for r in rs for x in r.get("athletes", []) for p in (x.get("items") or [x])}
    ret = {}
    for t in (get_json(INJ) or {}).get("injuries", []):
        for i in t["injuries"]:
            ret[i["athlete"]["displayName"]] = (i.get("details") or {}).get("returnDate")

    lg = e.connect()
    e.refresh_build(lg)
    me = e.my_team(lg)
    opp = next(t for t in lg.teams if t.team_name == S.OPP_NAME)
    box = next(b for b in lg.box_scores(lg.currentMatchupPeriod) if me in (b.home_team, b.away_team))
    home = box.home_team == me
    act = lambda s: S.fin({k: float((s.get(S.MAP[k]) or {}).get("value", 0) or 0) for k in S.K})
    mine_now, opp_now = act(box.home_stats if home else box.away_stats), act(box.away_stats if home else box.home_stats)
    # ESPN 的本週累計要到換日後才更新;當天(或累計落後時)改用每日先發數據加總
    daily = [dict.fromkeys(S.K, 0.0), dict.fromkeys(S.K, 0.0)]
    for i in range(i0):
        sp_i = preset.BASE_SP + ((W0 + timedelta(days=i)) - preset.BASE_DAY).days
        bd = next(b for b in lg.box_scores(lg.currentMatchupPeriod, scoring_period=sp_i, matchup_total=False)
                  if me in (b.home_team, b.away_team))
        for j, lu in enumerate((bd.home_lineup, bd.away_lineup) if home else (bd.away_lineup, bd.home_lineup)):
            for x in lu:
                if x.slot_position in ("BE", "IR"):
                    continue
                st = x.points_breakdown or {}
                for k in S.K:
                    daily[j][k] += float(st.get(S.MAP[k], 0) or 0)
    if daily[0]["fga"] > mine_now["fga"]:
        mine_now = S.fin(daily[0])
    if daily[1]["fga"] > opp_now["fga"]:
        opp_now = S.fin(daily[1])

    def ent(p, name, elig, d_from=0):
        s = season_stats(p, e.YEAR, get_json)
        tm = pid_team.get(str(p))
        if not s or not tm:
            return None
        rd = ret.get(name)
        b = (date.fromisoformat(rd[:10]) - W0).days if rd else 0
        if b > 6:
            return None
        return {"id": p, "name": name, "tm": tm, "s": s, "v": w.cade_value(s), "el": set(elig), "b": max(b, d_from, i0)}

    def proj(pool, track=None):
        tot, starts = dict.fromkeys(S.K, 0.0), 0
        for i in range(i0, 7):
            for s in w.pick_starters([(x["v"], x["el"], x) for x in pool if x["tm"] in days[i] and x["b"] <= i], w.LINEUP):
                if track is not None and s is track:
                    starts += 1
                g = s["s"]
                for k in ("pts", "reb", "ast", "stl", "blk", "tpm", "tov", "fga", "fta"):
                    tot[k] += g.get(k, 0)
                tot["fgm"] += g["fga"] * g["fgp"]
                tot["ftm"] += g["fta"] * g.get("ftp", 0)
        return S.fin(tot), starts

    el = lambda p: [SLOT_NAME[x] for x in getattr(p, "eligibleSlots", []) if x in SLOT_NAME] or ALL
    with ThreadPoolExecutor(16) as ex:
        mine = [x for x in ex.map(lambda p: ent(p.playerId, p.name, el(p)), me.roster) if x]
        opool = [x for x in ex.map(lambda p: ent(p.playerId, p.name, el(p)), opp.roster) if x]
        rec = dict(zip([p.playerId for p in me.roster], ex.map(lambda p: last_n(p.playerId), me.roster)))
    rem_mine = proj(mine)[0]
    po = S.add(opp_now, proj(opool)[0])
    base = S.add(mine_now, rem_mine)
    frac = rem_mine["fga"] / max(base["fga"], 1)
    wp = win_probs(base, po, frac)
    e0 = sum(p for _, p in wp.values())
    print(f"{us_today} 從第 {i0 + 1} 天起預估(實際累計 + 剩下,本週還剩 {frac:.0%}):預期贏 {e0:.2f} 類")
    print("  類別   我/對手   z   贏的機率  重要度(φ)")
    for k in CATS:
        z, p = wp[k]
        print(f"  {w.ABBR[k]:4} {w.fmt(k, base[k]):>5}/{w.fmt(k, po[k]):<5} {z:+.2f}  {p:4.0%}   {math.exp(-z * z / 2):.2f}")

    by_id = {x["id"]: x for x in mine}
    streams = []
    print("分級:")
    for p in me.roster:
        s = season_stats(p.playerId, e.YEAR, get_json)
        if not s:
            continue
        tg = tier(s, rec.get(p.playerId))
        print(f"  {p.name:22} {tg:6} 季 {pick_value(s):.2f} 近5 {pick_value(rec[p.playerId]) if rec.get(p.playerId) else 0:.2f}")
        if tg == "串流" and p.lineupSlot != "IR" and p.name not in S.NEVER:
            streams.append(p)

    left = a.adds_left(lg, me.team_id, i0)
    print(f"本週還能撿 {left} 次" + ("(週一~週四留 1 次)" if i0 <= 3 else ""))
    r = lg.espn_request.league_get(params={"view": "kona_player_info"}, headers={"x-fantasy-filter": json.dumps(
        {"players": {"filterStatus": {"value": ["FREEAGENT"]}, "limit": 300, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}})})
    fa_meta = {p["id"]: p for p in r["players"] if (p["player"].get("injuryStatus") or "ACTIVE") != "OUT"}
    fa_players = {p.playerId: p for p in lg.free_agents(size=300)}
    with ThreadPoolExecutor(16) as ex:
        cands = [x for x in ex.map(lambda pid: ent(pid, fa_meta[pid]["player"]["fullName"],
                                                   [SLOT_NAME[s] for s in fa_meta[pid]["player"]["eligibleSlots"] if s in SLOT_NAME]),
                                   fa_meta) if x and x["s"]["gp"] >= 4 and x["s"]["min"] >= 12]
    rows = []
    for d in streams:
        dx = by_id.get(d.playerId)
        rest = [x for x in mine if x is not dx]
        for c in cands:
            fp = fa_players.get(c["id"])
            if fp and a.pos_full(lg, me.roster, fp, d):
                continue
            tot, starts = proj(rest + [c], track=c)
            if not starts:
                continue
            full = S.add(mine_now, tot)
            wp1 = win_probs(full, po, frac)
            rows.append((sum(p for _, p in wp1.values()) - e0, d.name, c["name"], c["tm"], starts, wp1))
    ok = sorted([r_ for r_ in rows if r_[0] > 0], key=lambda r_: -r_[0])
    print("建議(依預期贏的類別數增加量;已含場數與各類變化):")
    for de, dn, cn, tm, st, wp1 in ok[:args.top]:
        moves = sorted(((wp1[k][1] - wp[k][1], k) for k in CATS), reverse=True)
        txt = "、".join(f"{w.ABBR[k]}{d * 100:+.0f}%" for d, k in moves if abs(d) >= 0.02)
        print(f"  撿 {cn}({tm}) 放 {dn}:+{de:.2f} 類(先發 {st} 場){';' + txt if txt else ''}")
    if not ok:
        print("  沒有比不換更好的組合")


if __name__ == "__main__":
    main()
