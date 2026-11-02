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


# 球隊打法:命中率、阻攻、籃板、抄截、助攻、失誤是主力(1),得分、三分其次(0.5),放棄罰球(0)。
BASE_WEIGHTS = {"fg": 1.0, "blk": 1.0, "reb": 1.0, "stl": 1.0, "ast": 1.0, "tov": 1.0,
                "pts": 0.5, "tpm": 0.5, "ft": 0.0}
BUILD_NAME = "主力命中率、阻攻、籃板、抄截、助攻、失誤(放棄罰球)"
# 單一類別不要太強:我隊最近一週比聯盟平均高 STRONG_SD 個標準差以上 → 權重 × STRONG_FACTOR;
# 低 HOPELESS_SD 個標準差以上 → 追不上,權重歸 0。由 espn_league.refresh_build() 套用。
STRONG_SD, STRONG_FACTOR, HOPELESS_SD = 1.5, 0.8, -1.5
WEIGHTS = dict(BASE_WEIGHTS)

# 聯盟每隊每週(換算成 7 天)的平均與標準差,用來把各類數據換成同一把尺。
# 預設是第 1–2 週的真實成績;espn_league.refresh_build() 會換成最新的。
WEEK_MU = {"pts": 643.7, "reb": 230.6, "ast": 147.0, "stl": 44.6, "blk": 26.2, "tpm": 67.1, "tov": 87.2,
           "fga": 494.1, "fta": 151.9, "fg": 0.4632, "ft": 0.7863}
WEEK_SD = {"pts": 79.6, "reb": 26.5, "ast": 27.5, "stl": 8.2, "blk": 6.6, "tpm": 15.3, "tov": 16.2,
           "fg": 0.0232, "ft": 0.0392}


def set_build(mu, sd, weights):
    """換上新的聯盟週平均、標準差與權重(就地更新,已 import 的模組也會看到)。"""
    WEEK_MU.update(mu)
    WEEK_SD.update(sd)
    WEIGHTS.update(weights)


def build_z(s):
    """單場數據能把隊伍週成績推動幾個聯盟標準差(失誤越少越好;命中率看拉高或拉低整週命中率多少)。"""
    z = {k: s.get(k, 0) / WEEK_SD[k] for k in ("pts", "reb", "ast", "stl", "blk", "tpm")}
    z["tov"] = -s.get("tov", 0) / WEEK_SD["tov"]
    for k, made, att in (("fg", "fgp", "fga"), ("ft", "ftp", "fta")):
        z[k] = (s.get(made, WEEK_MU[k]) - WEEK_MU[k]) * s.get(att, 0) / WEEK_MU[att] / WEEK_SD[k]
    return z


def build_value(s, boost=()):
    """單場價值 = Σ 打法權重 × 推動幾個標準差。boost 是本週拉鋸中的類別,權重加倍。"""
    return sum(WEIGHTS[k] * (2.0 if k in boost else 1.0) * v for k, v in build_z(s).items())


def build_per_min(s):
    """每分鐘價值(依 WEIGHTS 的打法)。"""
    if not s or not s.get("min"):
        return 0.0
    return build_value(s) / s["min"]
