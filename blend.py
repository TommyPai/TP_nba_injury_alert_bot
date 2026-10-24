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
