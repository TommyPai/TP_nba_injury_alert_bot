"""開賽前先發守門(11/21 Zion、OG 臨時 OUT 後加的)。

每 5 分鐘跑一次:今天先發裡有人被 ESPN 標 OUT(或今天沒比賽),而板凳上有今天有比賽、還沒開打、不是 OUT 的人,
就重排今天先發,把能上場的人排進去。已經開打(鎖住)的球員一律不動。只排先發,不撿人、不放人。
只依 ESPN 官方 OUT 判斷,DTD/GTD 不動(見 auto-trigger 規則)。
"""
from datetime import datetime, timedelta, timezone

import auto_fix as a
import preset
from nba_injury_alert import get_json, load_config, notify
import weekly_stream as w


def games_today(us_day):
    """回傳 {proTeamId(str): 開賽時間 UTC 或 None(已開打)}。"""
    now = datetime.now(timezone.utc)
    out = {}
    for ev in (get_json(w.SCOREBOARD.format(d=us_day.strftime("%Y%m%d"))) or {}).get("events", []):
        start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        pre = ev["status"]["type"].get("state") == "pre" and start > now
        for c in ev["competitions"][0]["competitors"]:
            out[str(c["team"]["id"])] = start if pre else None
    return out


def plan(entries, games):
    pl = lambda e: e["playerPoolEntry"]["player"]
    team = lambda e: str(pl(e)["proTeamId"])
    locked = lambda e: team(e) in games and games[team(e)] is None
    plays = lambda e: team(e) in games and (pl(e).get("injuryStatus") or "") != "OUT"
    act = [e for e in entries if e["lineupSlotId"] != a.IR]
    # 鎖住的先發佔住原位
    free = list(preset.STARTERS)
    for e in act:
        if locked(e) and e["lineupSlotId"] in free:
            free.remove(e["lineupSlotId"])
    movable = [e for e in act if not locked(e)]
    order = sorted(movable, key=lambda e: (not plays(e), -(preset.value(e["playerId"]) if plays(e) else 0)))
    owner = [None] * len(free)
    slots = lambda e: pl(e)["eligibleSlots"]

    def place(i, seen):
        for j, s in enumerate(free):
            if s in slots(order[i]) and j not in seen:
                seen.add(j)
                if owner[j] is None or place(owner[j], seen):
                    owner[j] = i
                    return True
        return False

    for i in range(len(order)):
        if plays(order[i]):
            place(i, set())
    want = {order[i]["playerId"]: free[j] for j, i in enumerate(owner) if i is not None}
    # 沒比賽或 OUT 的人補空位(不影響上場人數)
    for e in order:
        if e["playerId"] not in want:
            want[e["playerId"]] = a.BE
    now_on = sum(1 for e in movable if plays(e) and e["lineupSlotId"] not in (a.BE, a.IR))
    new_on = sum(1 for e in movable if plays(e) and want[e["playerId"]] != a.BE)
    items = [{"playerId": e["playerId"], "type": "LINEUP", "fromLineupSlotId": e["lineupSlotId"],
              "toLineupSlotId": want[e["playerId"]]}
             for e in movable if want[e["playerId"]] != e["lineupSlotId"]]
    ins = [pl(e)["fullName"] for e in movable if plays(e) and want[e["playerId"]] != a.BE and e["lineupSlotId"] == a.BE]
    outs = [pl(e)["fullName"] for e in movable if not plays(e) and e["lineupSlotId"] not in (a.BE, a.IR)]
    return now_on, new_on, items, ins, outs


def main():
    sp, team_id, entries = preset.roster_at()
    us_day = preset.BASE_DAY + timedelta(days=sp - preset.BASE_SP)
    games = games_today(us_day)
    now_on, new_on, items, ins, outs = plan(entries, games)
    print(us_day, f"sp{sp}", "上場人數", now_on, "->", new_on, "換上", ins, "換下", outs)
    if new_on <= now_on or not items:
        return
    code, text = a.post({"teamId": team_id, "type": "ROSTER", "scoringPeriodId": sp, "items": items})
    _, _, after = preset.roster_at(sp)
    ok_now, _, _, _, _ = plan(after, games)
    msg = (f"{us_day:%m/%d} 先發調整:換上 {'、'.join(ins) or '無'};換下 {'、'.join(outs) or '無'}。"
           f"\nESPN 回應 {code};重讀後今天上場 {ok_now} 人(原本 {now_on})。")
    if code >= 300:
        msg += f"\n失敗:{text[:200]}"
    print(msg)
    notify(load_config(), "🔁 先發守門", msg, "先發守門")


if __name__ == "__main__":
    main()
