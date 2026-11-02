"""連到 ESPN Fantasy 私人聯盟。

需要兩個登入 cookie:環境變數 ESPN_S2、ESPN_SWID(雲端放在 GitHub Secrets),
本機也可以寫在 config.json 的 espn_s2、espn_swid。沒有設定就回傳 None,其他功能照常運作。
"""
import json
import os
from pathlib import Path

LEAGUE_ID = int(os.environ.get("ESPN_LEAGUE_ID") or 896214217)
TEAM_NAME = os.environ.get("ESPN_TEAM_NAME") or "LeBronBron"
YEAR = int(os.environ.get("ESPN_YEAR") or 2027)  # ESPN 以球季結束的年份命名:2026-27 = 2027


def _creds():
    s2 = (os.environ.get("ESPN_S2") or "").strip()
    swid = (os.environ.get("ESPN_SWID") or "").strip()
    if not (s2 and swid):
        try:
            cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        s2 = s2 or cfg.get("espn_s2", "").strip()
        swid = swid or cfg.get("espn_swid", "").strip()
    return s2, swid


def connect():
    s2, swid = _creds()
    if not (s2 and swid):
        return None
    try:
        from espn_api.basketball import League
        return League(league_id=LEAGUE_ID, year=YEAR, espn_s2=s2, swid=swid)
    except Exception as e:  # cookie 過期或 ESPN 暫時失敗時不要讓整個機器人掛掉
        print(f"ESPN 聯盟連線失敗:{e}", flush=True)
        return None


def my_team(lg):
    for t in lg.teams:
        if t.team_name.strip().lower() == TEAM_NAME.strip().lower():
            return t
    return None


def pending_adds(lg, hours=36):
    """最近已撿、但還沒生效的球員。ESPN 在當天陣容鎖定後的異動,要到下一個計分日才會出現在陣容裡。"""
    import time
    ids = set()
    try:
        for a in lg.recent_activity(size=50):
            if time.time() - a.date / 1000 > hours * 3600:
                break
            for act in a.actions:
                if "ADDED" not in str(act[1]) or len(act) < 3:
                    continue
                player = act[2]  # 異動紀錄裡只有球員名字,用聯盟的對照表換回 ID
                pid = getattr(player, "playerId", None) or lg.player_map.get(player)
                if pid is not None:
                    ids.add(str(pid))
    except Exception as e:  # 異動紀錄讀不到時,至少還有陣容資料可用
        print(f"讀不到聯盟異動紀錄:{e}", flush=True)
    return ids


def rostered_ids(lg):
    """聯盟裡所有已被選走的球員(ESPN 球員 ID,字串),包含已撿但明天才生效的人。"""
    return {str(p.playerId) for t in lg.teams for p in t.roster} | pending_adds(lg)


def opponent(lg, me, period=None):
    """指定週(預設本週)的對戰對手。"""
    for m in lg.scoreboard(period):
        home = getattr(m.home_team, "team_id", m.home_team)
        away = getattr(m.away_team, "team_id", m.away_team)
        if home == me.team_id:
            return m.away_team
        if away == me.team_id:
            return m.home_team
    return None


WEEK_CATS = {"PTS": "pts", "REB": "reb", "AST": "ast", "STL": "stl", "BLK": "blk", "3PM": "tpm", "TO": "tov",
             "FGA": "fga", "FTA": "fta", "FG%": "fg", "FT%": "ft"}


def refresh_build(lg):
    """用聯盟已打完各週的真實成績,更新 blend 的週平均、標準差,並依我隊最近一週的強弱調整權重。
    回傳 {類別: (我隊離平均幾個標準差, 權重)};讀不到就保留預設並回傳 {}。"""
    import statistics as stt
    import blend
    try:
        rows, mine = [], None
        me = my_team(lg)
        for mp in range(1, lg.currentMatchupPeriod):
            scale = 7 / len(lg.matchup_ids[mp])  # 每週天數不同(開季週較短),累計類別換算成 7 天
            for b in lg.box_scores(matchup_period=mp):
                for team, st in ((b.home_team, b.home_stats), (b.away_team, b.away_stats)):
                    row = {v: st[k]["value"] * (1 if "%" in k else scale) for k, v in WEEK_CATS.items()}
                    rows.append(row)
                    if me and getattr(team, "team_id", team) == me.team_id:
                        mine = row  # 越後面的週會覆蓋前面,留下最近一週
        if len(rows) < 4:
            return {}
        mu = {k: stt.mean(r[k] for r in rows) for k in rows[0]}
        sd = {k: stt.pstdev(r[k] for r in rows) for k in rows[0] if k not in ("fga", "fta")}
        weights, report = dict(blend.BASE_WEIGHTS), {}
        for k in weights:
            if not mine or not sd[k]:
                continue
            z = (mine[k] - mu[k]) / sd[k] * (-1 if k == "tov" else 1)
            if weights[k] and z >= blend.STRONG_SD:
                weights[k] *= blend.STRONG_FACTOR
            elif z <= blend.HOPELESS_SD:
                weights[k] = 0.0
            report[k] = (z, weights[k])
        blend.set_build(mu, sd, weights)
        return report
    except Exception as e:  # ESPN 格式變了或暫時讀不到時,沿用預設值
        print(f"讀不到聯盟週成績,沿用預設權重:{e}", flush=True)
        return {}


def league_sets():
    """(已被選走的球員 ID, 我的球員 ID);連不上就回傳兩個空集合。順便用聯盟週成績更新打法權重。"""
    lg = connect()
    if not lg:
        return frozenset(), frozenset()
    refresh_build(lg)
    me = my_team(lg)
    mine = {str(p.playerId) for p in me.roster} if me else set()
    return frozenset(rostered_ids(lg)), frozenset(mine)
