#!/usr/bin/env python3
"""一次性:美國週日(計分日 20)一開始,Allen(OUT)進 IR、Diabate(day-to-day)回板凳,不放任何人。
兩人週日都沒比賽;這樣 IR 合法,週一 Diabate 可以直接上。只在週日第一場開打前動作,動完就自動結束。"""
from datetime import datetime, timezone

import auto_fix as a
import espn_league
from nba_injury_alert import STATE_PATH, load_config, load_json, notify, save_json

ALLEN, DIABATE = 4066328, 4433249
PERIOD = 20
OPEN = datetime(2026, 11, 8, 8, 30, tzinfo=timezone.utc)   # 墨爾本週日 19:30
TIP = datetime(2026, 11, 8, 20, 30, tzinfo=timezone.utc)   # 美國週日第一場 PHI @ CHI


def main():
    if not OPEN <= datetime.now(timezone.utc) < TIP:
        return
    team_id, roster = a.my_entries()
    by_id = {p["id"]: p for p in roster}
    allen, diabate = by_id.get(ALLEN), by_id.get(DIABATE)
    if not allen or not diabate or diabate["slot"] != a.IR or allen["slot"] == a.IR:
        return  # 已經換好,或名單變了
    state = load_json(STATE_PATH, {})
    if state.get("allen_swap2_done"):
        return

    def finish(head, body):
        state["allen_swap2_done"] = True
        save_json(STATE_PATH, state)
        notify(load_config(), head, body, body[:120])

    if allen["status"] != "OUT":
        return finish("ℹ️ Allen 沒進 IR", f"Allen 已不是 OUT({allen['status']}),不能進 IR,所以沒換。週一兩人都回來時再決定要不要放 Ellis。")
    lg = espn_league.connect()
    if lg.scoringPeriodId != PERIOD:
        return  # ESPN 還沒換日,下次再試
    code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": PERIOD, "items": [
        {"playerId": DIABATE, "type": "LINEUP", "fromLineupSlotId": a.IR, "toLineupSlotId": a.BE},
        {"playerId": ALLEN, "type": "LINEUP", "fromLineupSlotId": allen["slot"], "toLineupSlotId": a.IR}]})
    if code >= 300:
        return finish("⚠️ Allen/Diabate 沒換成", f"ESPN 回應 {code}:{text}\n沒有放任何人,請手動處理。")
    finish("✅ Allen 進 IR、Diabate 回板凳", "沒有放人。Diabate 週一可以直接上。")


if __name__ == "__main__":
    main()
