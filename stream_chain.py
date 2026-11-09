#!/usr/bin/env python3
"""串流位每天換人(Tommy 11/9 同意:放 Lopez 開串流位,串流打一場就丟換下一個,不用問)。
每個計分日在第一場開打前 WINDOW 分鐘內跑一次:
1. 讀 ESPN 本週實際累計 + 剩下幾天預估(我方不含串流位,對手 bbq 全隊)。
2. 當天有比賽、健康、FREEAGENT 的自由球員(只用本季數據,依對手失分/失誤排名 ±30%)和「留著現在的串流」比,
   選讓本週對戰「贏的類別數 − 輸的類別數」最多的;平手再看拉鋸類別推近多少。
3. 要換就送 ADD + DROP(只會放串流位的人:一開始是 Lopez,之後是上一個串流),再把今天到週日的陣容重排。
只動串流位;任何其他人都不放。到 11/16 自動停止。用 --dry 只印決定不送出。"""
import argparse
import json
from datetime import date, datetime, timedelta, timezone

import auto_fix as a
import espn_league as e
import preset
import weekly_stream as w
from blend import season_stats
from nba_injury_alert import STATE_PATH, get_json, load_config, load_json, notify, save_json

FIRST_SLOT = "Brook Lopez"          # 串流位一開始的人
NEVER = {"Jarrett Allen", "Moussa Diabate"}
OPP_NAME = "bbq chicken"
WEEK = (date(2026, 11, 9), date(2026, 11, 15))
PROPOSE_ONLY = True                 # True:只用 Telegram 推薦,不換人(Tommy 要每天自己選)
WINDOW = timedelta(minutes=120)     # 第一場開打前多久開始動作(越晚傷況越清楚,但 GitHub 排程會延遲)
K = ("pts", "reb", "ast", "stl", "blk", "tpm", "tov", "fga", "fgm", "fta", "ftm")
MAP = dict(zip(K, ("PTS", "REB", "AST", "STL", "BLK", "3PM", "TO", "FGA", "FGM", "FTA", "FTM")))
TH = {k: th for _, k, th in w.CATS}


def fin(t):
    return {**t, "fg": t["fgm"] / t["fga"] if t["fga"] else 0, "ft": t["ftm"] / t["fta"] if t["fta"] else 0}


def add(x, y):
    return fin({k: x[k] + y[k] for k in K})


def score(me, opp):
    r = w.compare(me, opp)
    net = sum(x[-1] == "win" for x in r) - sum(x[-1] == "lose" for x in r)
    push = 0.0
    for _, k, p, q, _ in r:
        d = (p - q) if k in ("fg", "ft") else (p - q) / max(q, 1)
        push += max(-1.0, min(1.0, (-d if k == "tov" else d) / TH[k]))
    return net, push


def game(s, oppo, f_off, f_stl):
    g = {k: s.get(k, 0) for k in ("pts", "reb", "ast", "stl", "blk", "tpm", "tov", "fga", "fta")}
    for k in ("pts", "tpm", "ast", "fga", "fta"):
        g[k] *= f_off.get(oppo, 1)
    g["stl"] *= f_stl.get(oppo, 1)
    g["fgm"], g["ftm"] = g["fga"] * s.get("fgp", 0), g["fta"] * s.get("ftp", 0)
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true", help="測試:不管時間窗和計分日")
    ap.add_argument("--today", type=date.fromisoformat, help="測試:假裝今天是這個美國日期")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    us_today = args.today or (now - timedelta(hours=a.ROLLOVER)).date()
    if not WEEK[0] <= us_today <= WEEK[1]:
        return
    state = load_json(STATE_PATH, {})
    sc = state.setdefault("stream_chain", {})
    key = us_today.isoformat()
    if sc.get(key) and not args.dry:
        return
    events = (get_json(w.SCOREBOARD.format(d=us_today.strftime("%Y%m%d"))) or {}).get("events", [])
    if not events:
        return
    tip = min(datetime.fromisoformat(ev["date"].replace("Z", "+00:00")) for ev in events)
    if not args.force and not (tip - WINDOW <= now < tip):
        return
    lg = e.connect()
    if not args.force and lg.scoringPeriodId != preset.BASE_SP + (us_today - preset.BASE_DAY).days:
        return  # ESPN 還沒換日
    e.refresh_build(lg)
    me = e.my_team(lg)
    team_id = me.team_id
    slot_name = sc.get("current") or FIRST_SLOT
    cur = next((p for p in me.roster if p.name == slot_name), None)
    if not cur or cur.name in NEVER:
        sc[key] = "error"
        save_json(STATE_PATH, state)
        return notify(load_config(), "⚠️ 串流沒換", f"名單上找不到串流位的 {slot_name},為了安全不動任何人。", "串流沒換")

    i0 = (us_today - WEEK[0]).days
    _, team_ids, days = w.games_this_week(WEEK[0])
    opp_of = [{} for _ in range(7)]
    for i in range(7):
        for ev in (get_json(w.SCOREBOARD.format(d=(WEEK[0] + timedelta(i)).strftime("%Y%m%d"))) or {}).get("events", []):
            cs = ev["competitions"][0]["competitors"]
            x, y = cs[0]["team"]["abbreviation"], cs[1]["team"]["abbreviation"]
            opp_of[i][x], opp_of[i][y] = y, x
    pid_team = {}
    for tid in team_ids.values():
        r = get_json(w.ROSTER.format(tid=tid)) or {}
        for x in r.get("athletes", []):
            for p in x.get("items") or [x]:
                pid_team[str(p["id"])] = r.get("team", {}).get("abbreviation")
    f_off, f_stl = a.opponent_factors(team_ids)

    opp_team = next(t for t in lg.teams if t.team_name == OPP_NAME)
    box = next(b for b in lg.box_scores(lg.currentMatchupPeriod, scoring_period=lg.scoringPeriodId, matchup_total=True)
               if me in (b.home_team, b.away_team))
    home = box.home_team == me
    act = lambda s: fin({k: float((s.get(MAP[k]) or {}).get("value", 0) or 0) for k in K})
    mine_now, opp_now = act(box.home_stats if home else box.away_stats), act(box.away_stats if home else box.home_stats)
    rest = days[i0:]
    base = add(mine_now, w.project([p for p in me.roster if p.playerId != cur.playerId], rest, pid_team, e.YEAR, 10))
    opp_tot = add(opp_now, w.project(opp_team.roster, rest, pid_team, e.YEAR, 10))

    options = []
    cs = season_stats(cur.playerId, e.YEAR, get_json)
    ct = pid_team.get(str(cur.playerId))
    if cs and ct in days[i0] and (cur.injuryStatus or "ACTIVE") != "OUT":
        options.append((score(add(base, game(cs, opp_of[i0][ct], f_off, f_stl)), opp_tot), cur, "keep"))
    else:
        options.append((score(base, opp_tot), cur, "keep"))
    taken = e.rostered_ids(lg)
    for p in lg.free_agents(size=150):
        tm = pid_team.get(str(p.playerId))
        if str(p.playerId) in taken or tm not in days[i0] or (p.injuryStatus or "ACTIVE") not in ("ACTIVE", "NORMAL"):
            continue
        s = season_stats(p.playerId, e.YEAR, get_json)
        if not s or s["gp"] < 3 or s["min"] < 18:
            continue
        options.append((score(add(base, game(s, opp_of[i0][tm], f_off, f_stl)), opp_tot), p, "add"))
    options.sort(key=lambda r: r[0], reverse=True)
    if PROPOSE_ONLY and not args.dry:  # Tommy 11/9:每天的人要他自己決定,只推薦前三名,不送出
        top = [r for r in options if r[2] == "keep" or a.player_status(lg, r[1].playerId) == "FREEAGENT"][:4]
        lines = [f"今天串流位是 {cur.name}。候選(預估本週 贏−輸 類別數,拉鋸推進):"]
        for (net, push), p, kind in top:
            if kind == "keep":
                lines.append(f"・留 {cur.name}:{net:+d} 類({push:.2f})")
                continue
            s = season_stats(p.playerId, e.YEAR, get_json)
            tm = pid_team.get(str(p.playerId))
            lines.append(f"・{p.name}({tm} 對 {opp_of[i0][tm]}):{net:+d} 類({push:.2f})"
                         f" {s['pts']:.1f}分 {s['reb']:.1f}板 {s['ast']:.1f}助 {s['stl']:.1f}抄 {s['blk']:.1f}帽 {s['tov']:.1f}失誤")
        lines.append("要換誰告訴 Claude。")
        sc[key] = "proposed"
        save_json(STATE_PATH, state)
        return notify(load_config(), "📋 今天的串流候選", "\n".join(lines), "串流候選")
    pick = None
    for sc_, p, kind in options:
        if kind == "keep" or a.player_status(lg, p.playerId) == "FREEAGENT":
            pick = (sc_, p, kind)
            break
    (net, push), p, kind = pick
    line = f"預估 {'+' if net >= 0 else ''}{net} 類(拉鋸推進 {push:.2f})"
    print(us_today, "串流位", cur.name, "→", p.name, kind, line, flush=True)
    if args.dry:
        for sc_, q, k in options[:8]:
            print("  ", q.name, k, sc_)
        return
    cfg = load_config()
    if kind == "keep":
        sc[key] = "keep"
        save_json(STATE_PATH, state)
        return notify(cfg, "ℹ️ 串流不換", f"今天留著 {cur.name}。{line}", "串流不換")
    if a.adds_left(lg, team_id, i0) <= 0:
        sc[key] = "no_adds"
        save_json(STATE_PATH, state)
        return notify(cfg, "⚠️ 串流沒換", "這週撿人額度用完了。", "串流沒換")
    code, text = a.post({"teamId": team_id, "type": "FREEAGENT", "scoringPeriodId": lg.scoringPeriodId,
                         "items": [{"playerId": p.playerId, "type": "ADD", "toTeamId": team_id},
                                   {"playerId": cur.playerId, "type": "DROP", "fromTeamId": team_id}]})
    if code >= 300:
        sc[key] = "error"
        save_json(STATE_PATH, state)
        return notify(cfg, "⚠️ 串流沒換", f"ESPN 回應 {code}:{text}\n沒有重試,{cur.name} 還在。", "串流沒換")
    sc[key], sc["current"] = p.name, p.name
    save_json(STATE_PATH, state)
    days_left = [us_today + timedelta(j) for j in range((WEEK[1] - us_today).days + 1)]
    bad = [f"{d:%m/%d}({c})" for d, _, c, *_ in preset.preset(days_left) if c >= 300]
    s = season_stats(p.playerId, e.YEAR, get_json)
    body = (f"放 {cur.name}、撿 {p.name}(今天對 {opp_of[i0][pid_team.get(str(p.playerId))]})。"
            f"本季 {s['pts']:.1f}分 {s['reb']:.1f}板 {s['ast']:.1f}助 {s['stl']:.1f}抄 {s['tov']:.1f}失誤。{line}"
            + (f"\n排先發失敗:{'、'.join(bad)},請手動排。" if bad else "\n今天到週日陣容已重排。"))
    notify(cfg, f"🔄 串流:{p.name}", body, body[:120])


if __name__ == "__main__":
    main()
