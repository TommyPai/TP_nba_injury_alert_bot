"""把帳號底下所有 ESPN 籃球聯盟的我方陣容 + 每人本季場均,存成 fantasy-snapshot.md 和 fantasy-snapshot.html。

聯盟清單從 ESPN 帳號抓(之後加入新聯盟會自動出現);場均用 blend.season_stats,和機器人其他地方同一個來源。
用法:py snapshot.py [輸出資料夾]
"""
import sys
import html
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from espn_api.basketball import League

import espn_league
from blend import season_stats
from nba_injury_alert import get_json

FAN_URL = "https://fan.api.espn.com/apis/v2/fans/{swid}"
POS = ("PG", "SG", "SF", "PF", "C")
COLS = ["球員", "NBA", "位置", "陣容", "傷況", "出賽", "PTS", "REB", "AST", "STL", "BLK", "FG%", "FT%", "3PM", "TO"]
MEL = timezone(timedelta(hours=11))  # 墨爾本夏令時間


def my_leagues(s2, swid):
    """回傳 [(league_id, 聯盟名, team_id, 隊名)],只取本季的 NBA 聯盟。"""
    r = requests.get(FAN_URL.format(swid=swid), params={"displayHiddenPrefs": "true", "context": "fantasy"},
                     cookies={"espn_s2": s2, "SWID": swid}, timeout=30)
    r.raise_for_status()
    out = []
    for p in r.json().get("preferences", []):
        ent = (p.get("metaData") or {}).get("entry") or {}
        if ent.get("abbrev") != "FBA" or ent.get("seasonId") != espn_league.YEAR:
            continue
        for g in ent.get("groups", []):
            out.append((g["groupId"], g.get("groupName", ""), ent.get("entryId"),
                        (ent.get("entryMetadata") or {}).get("teamName", "")))
    return out or [(espn_league.LEAGUE_ID, "", None, espn_league.TEAM_NAME)]


def row(p):
    s = season_stats(p.playerId, espn_league.YEAR, get_json) or {}
    pos = "/".join(x for x in POS if x in (p.eligibleSlots or []))
    base = [p.name, p.proTeam, pos, p.lineupSlot, p.injuryStatus or "-"]
    if not s:
        return base + ["0"] + ["-"] * 9
    return base + [f"{int(s['gp'])}", f"{s['pts']:.1f}", f"{s['reb']:.1f}", f"{s['ast']:.1f}", f"{s['stl']:.1f}",
                   f"{s['blk']:.1f}", f"{s['fgp'] * 100:.1f}", f"{s['ftp'] * 100:.1f}", f"{s['tpm']:.1f}", f"{s['tov']:.1f}"]


def snapshot():
    s2, swid = espn_league._creds()
    blocks = []
    for lid, lname, tid, tname in my_leagues(s2, swid):
        lg = League(league_id=lid, year=espn_league.YEAR, espn_s2=s2, swid=swid)
        team = next((t for t in lg.teams if t.team_id == tid), None) or next(
            t for t in lg.teams if t.team_name.strip().lower() == tname.strip().lower())
        with ThreadPoolExecutor(8) as ex:
            rows = list(ex.map(row, team.roster))
        blocks.append((lname or lg.settings.name, team.team_name, rows))
    return blocks


def to_md(blocks, stamp):
    out = [f"# fantasy-snapshot", "", f"更新時間:{stamp}(本季場均,資料來源 ESPN)", ""]
    for lname, tname, rows in blocks:
        out += [f"## {lname} — {tname}", "", "| " + " | ".join(COLS) + " |", "|" + "---|" * len(COLS)]
        out += ["| " + " | ".join(r) + " |" for r in rows] + [""]
    return "\n".join(out)


def to_html(blocks, stamp):
    esc = html.escape
    out = [f"<html><body><h1>fantasy-snapshot</h1><p>更新時間:{esc(stamp)}(本季場均,資料來源 ESPN)</p>"]
    for lname, tname, rows in blocks:
        out.append(f"<h2>{esc(lname)} — {esc(tname)}</h2><table border='1'><tr>"
                   + "".join(f"<th>{c}</th>" for c in COLS) + "</tr>")
        out += ["<tr>" + "".join(f"<td>{esc(v)}</td>" for v in r) + "</tr>" for r in rows]
        out.append("</table>")
    return "".join(out) + "</body></html>"


if __name__ == "__main__":
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    blocks = snapshot()
    stamp = datetime.now(MEL).strftime("%Y-%m-%d %H:%M 墨爾本")
    (dest / "fantasy-snapshot.md").write_text(to_md(blocks, stamp), encoding="utf-8")
    (dest / "fantasy-snapshot.html").write_text(to_html(blocks, stamp), encoding="utf-8")
    print(f"寫入 {sum(len(b[2]) for b in blocks)} 位球員、{len(blocks)} 個聯盟")
