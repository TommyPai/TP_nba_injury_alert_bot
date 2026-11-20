#!/usr/bin/env python3
"""一次性(Tommy 11/20):美國 11/23 ESPN 換日後、第一場開打前,放串流位(原 McCain,11/20 換成 Mykhailiuk)、撿 Nolan Traore(BKN,長期留),
再排好 11/23–11/29 的先發。只做一次(記在 state.json)。
安全條件:Traore 必須是 FREEAGENT 而且 ESPN 狀態 ACTIVE(DTD / OUT 都不動,只通知 Tommy)。"""
from datetime import date, datetime, timedelta, timezone

import auto_fix as a
import espn_league
import preset
from nba_injury_alert import STATE_PATH, load_config, load_json, notify, save_json

ADD_ID, ADD = 5279130, "Nolan Traore"
DROP = "Svi Mykhailiuk"
DAY = date(2026, 11, 23)
SP = preset.BASE_SP + (DAY - preset.BASE_DAY).days
START = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)  # ESPN 換到 11/23
TIP = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)  # 11/23 第一場開打
ACTIVE_MAX = 13
FLAG = "traore_add_done"


def main():
    now = datetime.now(timezone.utc)
    if not (START <= now < TIP):
        return
    state = load_json(STATE_PATH, {})
    if state.get(FLAG):
        return
    lg = espn_league.connect()
    if lg.scoringPeriodId != SP:
        return print("ESPN 還沒換到 11/23", flush=True)
    cfg = load_config()

    def done(head, body):
        state[FLAG] = True
        save_json(STATE_PATH, state)
        print(head, body, flush=True)
        notify(cfg, head, body, body[:120])

    st = a.player_status(lg, ADD_ID)
    if st != "FREEAGENT":
        return done(f"⚠️ {ADD} 沒撿", f"他現在是 {st}(可能被撿走或在 waiver),沒有動作,{DROP} 還在。")
    info = next((p for p in lg.free_agents(size=400) if p.playerId == ADD_ID), None)
    inj = (info.injuryStatus if info else None) or "UNKNOWN"
    if inj not in ("ACTIVE", "NORMAL"):
        return done(f"⚠️ {ADD} 沒撿", f"ESPN 狀態是 {inj},不是 ACTIVE,依規則不動。{DROP} 還在,要不要換請告訴 Claude。")
    team_id, roster = a.my_entries()
    if any(p["id"] == ADD_ID for p in roster):
        return done(f"ℹ️ {ADD} 已在名單", "沒有動作。")
    if any(p["slot"] == a.IR and p["status"] != "OUT" for p in roster):
        return done(f"⚠️ {ADD} 沒撿", "IR 裡有人不是 OUT,ESPN 不讓撿人。請手動處理。")
    room = sum(p["slot"] != a.IR for p in roster) < ACTIVE_MAX
    cut = next((p for p in roster if p["name"] == DROP), None)
    if not room and not cut:
        return done(f"⚠️ {ADD} 沒撿", f"名單滿了,也找不到 {DROP},為了安全不動任何人。")
    items = [{"playerId": ADD_ID, "type": "ADD", "toTeamId": team_id}]
    if not room:
        items.append({"playerId": cut["id"], "type": "DROP", "fromTeamId": team_id})
    code, text = a.post({"teamId": team_id, "type": "FREEAGENT", "scoringPeriodId": SP, "items": items})
    if code >= 300:
        return done(f"⚠️ {ADD} 沒撿", f"ESPN 回應 {code}:{text[:300]}\n{DROP} 還在,請手動處理。")
    days = [DAY + timedelta(i) for i in range(7)]
    bad = [f"{d:%m/%d}({c})" for d, _, c, *_ in preset.preset(days) if c >= 300]
    _, after = a.my_entries()
    ok = any(p["id"] == ADD_ID for p in after) and (room or not any(p["name"] == DROP for p in after))
    done(f"✅ 撿了 {ADD}" if ok else f"⚠️ {ADD} 撿人未確認",
         ("名單有空位,沒放人," if room else f"已放 {DROP}、") + f"撿 {ADD}(BKN 本週 11/23、25、27、28 有賽)。"
         + (f"排先發失敗:{'、'.join(bad)},請手動排。" if bad else "11/23–11/29 先發已重排。")
         + ("" if ok else " ESPN 名單上還沒看到變化,請到 ESPN 確認。"))


if __name__ == "__main__":
    main()
