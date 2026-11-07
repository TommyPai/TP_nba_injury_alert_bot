#!/usr/bin/env python3
"""一次性:週六(計分日 19)一開始就把 Allen 移回先發、Diabate 進 IR,不放任何人。
Diabate 週六週日沒比賽,Allen 週六有。ESPN 不准預排 IR,只能等換日後做。
動完 Allen 就不在 IR,之後每次執行都會直接結束。"""
from datetime import datetime, timezone

import auto_fix as a
from nba_injury_alert import STATE_PATH, load_config, load_json, notify, save_json

ALLEN, DIABATE = 4066328, 4433249
OPEN = datetime(2026, 11, 7, 8, 30, tzinfo=timezone.utc)   # 週六計分日開始後
TIP = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)     # NY @ CLE 開打


def main():
    if not OPEN <= datetime.now(timezone.utc) < TIP:
        return
    team_id, roster = a.my_entries()
    by_id = {p["id"]: p for p in roster}
    allen, diabate = by_id.get(ALLEN), by_id.get(DIABATE)
    if not allen or not diabate or allen["slot"] != a.IR:
        return
    state = load_json(STATE_PATH, {})

    def warn(head, body):  # 問題只通知一次,不要每 5 分鐘重複
        if not state.get("allen_swap_warned"):
            state["allen_swap_warned"] = True
            save_json(STATE_PATH, state)
            notify(load_config(), head, body, body[:120])

    if diabate["status"] != "OUT":
        return warn("⚠️ Allen 沒換回來", "Diabate 不是 OUT,不能進 IR,要先決定怎麼排。沒有動任何人。")
    target = 12 if any(p["slot"] == 4 for p in roster) else 4
    code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": 19, "items": [
        {"playerId": DIABATE, "type": "LINEUP", "fromLineupSlotId": diabate["slot"], "toLineupSlotId": a.IR},
        {"playerId": ALLEN, "type": "LINEUP", "fromLineupSlotId": a.IR, "toLineupSlotId": target}]})
    if code >= 300:
        return warn("⚠️ Allen 對調失敗", f"ESPN 回應 {code}:{text}\n沒有放任何人,請手動處理。")
    body = f"Allen 狀態 {allen['status']},已移回{'先發 C' if target == 4 else '板凳'},Diabate 進 IR。"
    notify(load_config(), "✅ Allen 回陣容", body, body[:120])


if __name__ == "__main__":
    main()
