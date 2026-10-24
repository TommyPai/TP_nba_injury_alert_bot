"""把今年和去年的數據混合成一份預估。

- 上場時間最快反映角色變化:今年打 K_MIN 場時,今年和去年各佔一半
- 每分鐘產量(得分、籃板、助攻、抄截、火鍋、3PM、失誤、出手數)穩定得慢:K_RATE 場各半
- 命中率(FG%、FT%)最不穩:K_PCT 場各半
- 沒有去年數據(新秀、整季傷缺)時,今年至少要打 MIN_GP_NO_PRIOR 場才採用
"""
K_MIN, K_RATE, K_PCT = 3, 10, 20
MIN_GP_NO_PRIOR = 3
RATES = ("pts", "reb", "ast", "stl", "blk", "tpm", "tov", "fga", "fta")


def blend(cur, prev):
    """cur、prev 是場均數據 dict(含 gp、min);回傳混合後的場均 dict,外加 cur_gp 欄位。"""
    gp = (cur or {}).get("gp", 0)
    if not prev or not prev.get("min"):
        return {**cur, "cur_gp": gp} if cur and gp >= MIN_GP_NO_PRIOR else None
    if not cur or not gp or not cur.get("min"):
        return {**prev, "cur_gp": 0}
    w = lambda k: gp / (gp + k)
    mins = w(K_MIN) * cur["min"] + (1 - w(K_MIN)) * prev["min"]
    out = {"gp": gp, "cur_gp": gp, "min": mins}
    for k in RATES:
        rate = w(K_RATE) * cur.get(k, 0) / cur["min"] + (1 - w(K_RATE)) * prev.get(k, 0) / prev["min"]
        out[k] = rate * mins
    for k in ("fgp", "ftp"):
        out[k] = w(K_PCT) * cur.get(k, 0) + (1 - w(K_PCT)) * prev.get(k, 0)
    return out


# ---------- 抓數據(兩邊共用;get_json 由呼叫端傳入,避免循環 import) ----------

STATS_URL = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
             "seasons/{year}/types/2/athletes/{pid}/statistics")
KEYS = {"gamesPlayed": "gp", "gamesStarted": "gs", "avgMinutes": "min", "avgPoints": "pts",
        "avgRebounds": "reb", "avgAssists": "ast", "avgSteals": "stl", "avgBlocks": "blk",
        "avgTurnovers": "tov", "avgThreePointFieldGoalsMade": "tpm", "avgFreeThrowsAttempted": "fta",
        "freeThrowPct": "ftp", "avgFieldGoalsAttempted": "fga", "fieldGoalPct": "fgp"}


def season_stats(pid, year, get_json):
    d = get_json(STATS_URL.format(year=year, pid=pid))
    if not d:
        return None
    s = {}
    for cat in d.get("splits", {}).get("categories", []):
        for x in cat.get("stats", []):
            if x["name"] in KEYS:
                s[KEYS[x["name"]]] = float(x["value"])
    for k in ("ftp", "fgp"):
        if s.get(k, 0) > 1:
            s[k] /= 100
    return s if s.get("gp") else None


def blended_stats(pid, year, get_json):
    """今年 + 去年混合後的場均;另外附上今年實際的場數、分鐘(cur_min)。"""
    cur = season_stats(pid, year, get_json)
    out = blend(cur, season_stats(pid, year - 1, get_json))
    if out is not None:
        out["cur_min"] = cur["min"] if cur else None
    return out


def cade_per_min(s):
    """Cade 打法(得分、3PM、罰球、助攻、抄截;不計失誤)的每分鐘價值。"""
    if not s or not s.get("min"):
        return 0.0
    ft = (s.get("ftp", 0.78) - 0.78) * s.get("fta", 0)
    v = (s.get("pts", 0) + 3.0 * s.get("tpm", 0) + 1.2 * s.get("ast", 0) + 2.5 * s.get("stl", 0)
         + 0.4 * s.get("reb", 0) + 1.0 * s.get("blk", 0) + 6 * ft)
    return v / s["min"]
