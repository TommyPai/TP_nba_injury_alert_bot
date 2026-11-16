#!/usr/bin/env python3
"""一次性(Tommy 11/17):Butler 11/15 被放掉後,等他過 waiver 變自由球員(11/17 08:00 UTC),
放 Caruso、撿回 Butler、放進 IR,再重排本週剩下的先發。只做一次(記在 state.json),11/22 後自動停止。"""
from datetime import date, datetime, timedelta, timezone

import auto_fix as a
import espn_league
import preset
import weekly_stream as w
from nba_injury_alert import STATE_PATH, get_json, load_config, load_json, notify, save_json

BUTLER = 6430
REAVES = "Austin Reaves"
GOODWIN = "Jordan Goodwin"
ACTIVE_MAX = 13  # 先發 10 + 板凳 3
DROP = "Alex Caruso"
START = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
WEEK_END = date(2026, 11, 22)


def main():
    now = datetime.now(timezone.utc)
    if now >= END:
        return
    state = load_json(STATE_PATH, {})
    goodwin_drop(state)
    # ESPN:IR 裡有不是 OUT 的人就不能撿人,所以 Goodwin 放掉後先把 Reaves 移出 IR,再撿 Butler
    if state.get("goodwin_drop_done"):
        reaves_back(now, state)
    if now < START or state.get("jb_back_done"):
        return
    cfg = load_config()

    def done(head, body):
        state["jb_back_done"] = True
        save_json(STATE_PATH, state)
        print(head, body, flush=True)
        notify(cfg, head, body, body[:120])

    lg = espn_league.connect()
    st = a.player_status(lg, BUTLER)
    if st == "WAIVERS":
        return print("Butler 還在 waiver", flush=True)
    if st != "FREEAGENT":
        return done("⚠️ Butler 沒撿回來", f"他現在是 {st}(可能被別隊撿走),沒有動作。Caruso 還在。")
    team_id, roster = a.my_entries()
    if any(p["id"] == BUTLER for p in roster):
        return done("ℹ️ Butler 已在名單", "沒有動作。")
    if sum(p["slot"] == a.IR for p in roster) >= a.IR_SLOTS:
        return done("⚠️ Butler 沒撿回來", "IR 已滿,沒有動作。請手動處理。")
    if any(p["slot"] == a.IR and p["status"] != "OUT" for p in roster):
        return print("IR 裡有人不是 OUT,ESPN 不讓撿人,等 Reaves 移出", flush=True)
    cut = next((p for p in roster if p["name"] == DROP), None)
    room = sum(p["slot"] != a.IR for p in roster) < ACTIVE_MAX  # 有空位就不用放 Caruso
    if not room and not cut:
        return done("⚠️ Butler 沒撿回來", f"名單滿了,也找不到 {DROP},為了安全不動任何人。")
    sp = lg.scoringPeriodId
    items = [{"playerId": BUTLER, "type": "ADD", "toTeamId": team_id}]
    if not room:
        items.append({"playerId": cut["id"], "type": "DROP", "fromTeamId": team_id})
    code, text = a.post({"teamId": team_id, "type": "FREEAGENT", "scoringPeriodId": sp, "items": items})
    if code >= 300:
        return done("⚠️ Butler 沒撿回來", f"ESPN 回應 {code}:{text[:300]}\n{DROP} 還在,請手動處理。")
    code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": sp, "items": [
        {"playerId": BUTLER, "type": "LINEUP", "fromLineupSlotId": a.BE, "toLineupSlotId": a.IR}]})
    ir = "Butler 已放進 IR" if code < 300 else f"⚠️ Butler 沒放進 IR(ESPN {code}),請手動移"
    us_today = (now - timedelta(hours=8)).date()
    days = [us_today + timedelta(i) for i in range((WEEK_END - us_today).days + 1)]
    bad = [f"{d:%m/%d}({c})" for d, _, c, *_ in preset.preset(days) if c >= 300]
    _, after = a.my_entries()
    ok = any(p["id"] == BUTLER for p in after) and (room or not any(p["name"] == DROP for p in after))
    done("✅ Butler 撿回來了" if ok else "⚠️ Butler 撿人未確認",
         ("名單有空位沒放人," if room else f"已放 {DROP}、") + f"撿回 Butler。{ir}。" + (f"排先發失敗:{'、'.join(bad)}。" if bad else "本週先發已重排。")
         + ("" if ok else " ESPN 名單上還沒看到變化,請到 ESPN 確認。"))


def goodwin_drop(state):
    """Tommy 11/17:Goodwin 打完美國 11/16 PHX@DEN 就放掉(他當天數據照算)。"""
    if state.get("goodwin_drop_done"):
        return
    ev = next((ev for ev in (get_json(w.SCOREBOARD.format(d="20261116")) or {}).get("events", [])
               if any(c["team"]["abbreviation"] == "PHX" for c in ev["competitions"][0]["competitors"])), None)
    if not ev or not ev["status"]["type"].get("completed"):
        return
    cfg = load_config()
    state["goodwin_drop_done"] = True
    save_json(STATE_PATH, state)
    team_id, roster = a.my_entries()
    g = next((p for p in roster if p["name"] == GOODWIN), None)
    if not g:
        return notify(cfg, "ℹ️ Goodwin 已不在名單", "沒有動作。", "Goodwin")
    lg = espn_league.connect()
    code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": lg.scoringPeriodId,
                         "items": [{"playerId": g["id"], "type": "DROP", "fromTeamId": team_id}]})
    _, after = a.my_entries()
    ok = code < 300 and not any(p["name"] == GOODWIN for p in after)
    body = "Goodwin 已放掉,名單空出一格(Reaves 回來 / 串流用)。" if ok else f"⚠️ ESPN 回應 {code}:{text[:300]} 請到 ESPN 確認。"
    print(body, flush=True)
    notify(cfg, "✅ 已放 Goodwin" if ok else "⚠️ Goodwin 沒放成", body, body[:120])


def reaves_back(now, state):
    """Reaves 不再是 OUT 就從 IR 移回名單(撿回 Butler 後名單剛好空一格);名單滿了才放 Caruso(串流位)。"""
    if state.get("reaves_back_done"):
        return
    team_id, roster = a.my_entries()
    r = next((p for p in roster if p["name"] == REAVES), None)
    if not r or r["slot"] != a.IR or r["status"] == "OUT":
        return
    cfg = load_config()
    state["reaves_back_done"] = True
    save_json(STATE_PATH, state)
    lg = espn_league.connect()
    sp = lg.scoringPeriodId
    msg = ""
    if sum(p["slot"] != a.IR for p in roster) >= ACTIVE_MAX:
        cut = next((p for p in roster if p["name"] == DROP), None)
        if not cut:
            return notify(cfg, "⚠️ Reaves 沒移出 IR", f"名單滿了,也找不到 {DROP},請手動處理。", "IR 沒動")
        code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": sp,
                             "items": [{"playerId": cut["id"], "type": "DROP", "fromTeamId": team_id}]})
        if code >= 300:
            return notify(cfg, "⚠️ Reaves 沒移出 IR", f"放 {DROP} 失敗:ESPN {code}:{text[:300]}", "IR 沒動")
        msg = f"已放 {DROP},"
    code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": sp, "items": [
        {"playerId": r["id"], "type": "LINEUP", "fromLineupSlotId": a.IR, "toLineupSlotId": a.BE}]})
    if code >= 300:
        return notify(cfg, "⚠️ Reaves 沒移出 IR", f"ESPN 回應 {code}:{text[:300]} 請手動處理。", "IR 沒動")
    us_today = (now - timedelta(hours=8)).date()
    days = [us_today + timedelta(i) for i in range((WEEK_END - us_today).days + 1)]
    bad = [f"{d:%m/%d}({c})" for d, _, c, *_ in preset.preset(days) if c >= 300]
    _, after = a.my_entries()
    ok = any(p["name"] == REAVES and p["slot"] != a.IR for p in after)
    body = msg + ("Reaves 已移回名單" if ok else "⚠️ ESPN 上 Reaves 還在 IR") + (f"。排先發失敗:{'、'.join(bad)}。" if bad else "。本週先發已重排。")
    print(body, flush=True)
    notify(cfg, "🔄 Reaves 回來", body, body[:120])


if __name__ == "__main__":
    main()
