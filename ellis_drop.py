#!/usr/bin/env python3
"""一次性(Tommy 11/8 指示):Keon Ellis 打完美國週日 11/8 BKN vs DAL 就放掉,不撿人。
放掉之後,Allen 一旦不是 OUT 就從 IR 移回來;如果當天第一場還沒開打,順便把當天有比賽的人排進先發。
每一步只做一次(記在 state.json),到 11/16 自動停止。"""
from datetime import datetime, timezone

import auto_fix as a
import espn_league
import weekly_stream as w
from nba_injury_alert import STATE_PATH, get_json, load_config, load_json, notify, save_json

ELLIS_GAME_DAY = "20261108"
ALLEN = 4066328
END = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
ACTIVE_MAX = 13  # 先發 10 + 板凳 3


def ellis_game_over():
    for ev in (get_json(w.SCOREBOARD.format(d=ELLIS_GAME_DAY)) or {}).get("events", []):
        if any(c["team"]["abbreviation"] == "BKN" for c in ev["competitions"][0]["competitors"]):
            return bool(ev["status"]["type"].get("completed"))
    return False


def main():
    if datetime.now(timezone.utc) >= END:
        return
    state = load_json(STATE_PATH, {})
    cfg = load_config()

    def done(key, head, body):
        state[key] = True
        save_json(STATE_PATH, state)
        print(head, body, flush=True)
        notify(cfg, head, body, body[:120])

    team_id, roster = a.my_entries()
    ellis = next((p for p in roster if p["name"] == "Keon Ellis"), None)

    if not state.get("ellis_drop_done"):
        if not ellis:
            return done("ellis_drop_done", "ℹ️ Ellis 已不在名單", "沒有動作。")
        if not ellis_game_over():
            return
        lg = espn_league.connect()
        code, text = a.post({"teamId": team_id, "type": "FREEAGENT", "scoringPeriodId": lg.scoringPeriodId,
                             "items": [{"playerId": ellis["id"], "type": "DROP", "fromTeamId": team_id}]})
        if code >= 300:
            return done("ellis_drop_done", "⚠️ Ellis 沒放成", f"ESPN 回應 {code}:{text}\n沒有重試,請手動放。")
        return done("ellis_drop_done", "✅ 已放掉 Keon Ellis",
                    "他週日的數據照算。Allen 不再是 OUT 時會自動從 IR 移回來。")

    if state.get("allen_back_done") or ellis:  # Ellis 還在名單 = 放人還沒生效,等 ESPN 換日
        return
    allen = next((p for p in roster if p["id"] == ALLEN), None)
    if not allen or allen["slot"] != a.IR or allen["status"] == "OUT":
        return
    if sum(p["slot"] != a.IR for p in roster) >= ACTIVE_MAX:
        return
    lg = espn_league.connect()
    _, today, started, opp, tids = a.schedule()
    code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": lg.scoringPeriodId,
                         "items": [{"playerId": ALLEN, "type": "LINEUP", "fromLineupSlotId": a.IR, "toLineupSlotId": a.BE}]})
    if code >= 300:
        return done("allen_back_done", "⚠️ Allen 沒移出 IR", f"ESPN 回應 {code}:{text}\n請手動處理。")
    if started:
        return done("allen_back_done", "✅ Allen 已移出 IR",
                    f"Allen 現在是 {allen['status']}。今天第一場已開打,明天才生效,明天的先發要再排。")
    code, text = a.set_lineup(espn_league.connect(), team_id, opp, today, tids)
    msg = "今天有比賽的人已排進先發。" if code < 300 else f"但排先發失敗({code}),請手動排。"
    done("allen_back_done", "✅ Allen 已移出 IR", f"Allen 現在是 {allen['status']}。{msg}")


if __name__ == "__main__":
    main()
