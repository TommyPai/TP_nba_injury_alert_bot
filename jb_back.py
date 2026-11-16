#!/usr/bin/env python3
"""一次性(Tommy 11/17):Butler 11/15 被放掉後,等他過 waiver 變自由球員(11/17 08:00 UTC),
放 Caruso、撿回 Butler、放進 IR,再重排本週剩下的先發。只做一次(記在 state.json),11/22 後自動停止。"""
from datetime import date, datetime, timedelta, timezone

import auto_fix as a
import espn_league
import preset
from nba_injury_alert import STATE_PATH, load_config, load_json, notify, save_json

BUTLER = 6430
DROP = "Alex Caruso"
START = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
WEEK_END = date(2026, 11, 22)


def main():
    now = datetime.now(timezone.utc)
    if not START <= now < END:
        return
    state = load_json(STATE_PATH, {})
    if state.get("jb_back_done"):
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
    cut = next((p for p in roster if p["name"] == DROP), None)
    if not cut:
        return done("⚠️ Butler 沒撿回來", f"名單上找不到 {DROP},為了安全不動任何人。")
    sp = lg.scoringPeriodId
    code, text = a.post({"teamId": team_id, "type": "FREEAGENT", "scoringPeriodId": sp, "items": [
        {"playerId": BUTLER, "type": "ADD", "toTeamId": team_id},
        {"playerId": cut["id"], "type": "DROP", "fromTeamId": team_id}]})
    if code >= 300:
        return done("⚠️ Butler 沒撿回來", f"ESPN 回應 {code}:{text[:300]}\n{DROP} 還在,請手動處理。")
    code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": sp, "items": [
        {"playerId": BUTLER, "type": "LINEUP", "fromLineupSlotId": a.BE, "toLineupSlotId": a.IR}]})
    ir = "Butler 已放進 IR" if code < 300 else f"⚠️ Butler 沒放進 IR(ESPN {code}),請手動移"
    us_today = (now - timedelta(hours=8)).date()
    days = [us_today + timedelta(i) for i in range((WEEK_END - us_today).days + 1)]
    bad = [f"{d:%m/%d}({c})" for d, _, c, *_ in preset.preset(days) if c >= 300]
    _, after = a.my_entries()
    ok = any(p["id"] == BUTLER for p in after) and not any(p["name"] == DROP for p in after)
    done("✅ Butler 撿回來了" if ok else "⚠️ Butler 撿人未確認",
         f"已放 {DROP}、撿回 Butler。{ir}。" + (f"排先發失敗:{'、'.join(bad)}。" if bad else "本週先發已重排。")
         + ("" if ok else " ESPN 名單上還沒看到變化,請到 ESPN 確認。"))


if __name__ == "__main__":
    main()
