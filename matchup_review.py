#!/usr/bin/env python3
"""本週(或上週)H2H 九類戰況分析,推播到 Telegram。

需要 ESPN_S2、ESPN_SWID(GitHub Secrets)。
    python matchup_review.py           本週到目前為止
    python matchup_review.py --last    上一週(週結束後檢討用)
    python matchup_review.py --dry     只印出
"""
import argparse
import html
import sys

import espn_league
from nba_injury_alert import load_config, notify

CATS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "FG%", "FT%", "TO"]
ZH = {"PTS": "得分", "REB": "籃板", "AST": "助攻", "STL": "抄截", "BLK": "火鍋", "3PM": "3PM",
      "FG%": "FG%", "FT%": "FT%", "TO": "失誤"}
MARK = {"WIN": "✅", "LOSS": "❌", "TIE": "⚖️"}


def side(box, me_id):
    home = getattr(box.home_team, "team_id", box.home_team)
    if home == me_id:
        return (box.home_stats, box.home_lineup, box.home_wins, box.home_losses, box.home_ties,
                box.away_team, box.away_stats, box.away_lineup)
    return (box.away_stats, box.away_lineup, box.away_wins, box.away_losses, box.away_ties,
            box.home_team, box.home_stats, box.home_lineup)


def val(stats, c):
    return (stats.get(c) or {}).get("value", 0) or 0


def fmt(c, v):
    return f"{v * 100:.1f}" if c in ("FG%", "FT%") else f"{v:.0f}"


def gp(p):
    return p.points_breakdown.get("GP", 0) or 0


def top(lineup, c, n=3):
    key = "FGA" if c == "FG%" else "FTA" if c == "FT%" else c
    ps = [p for p in lineup if (p.points_breakdown.get(key) or 0) > 0]
    ps.sort(key=lambda p: -(p.points_breakdown.get(key) or 0))
    return ps[:n]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    lg = espn_league.connect()
    if not lg:
        print("沒有 ESPN 登入資訊(ESPN_S2、ESPN_SWID),無法分析")
        return
    me = espn_league.my_team(lg)
    period = lg.currentMatchupPeriod - (1 if args.last else 0)
    boxes = lg.box_scores(matchup_period=period)
    box = next(b for b in boxes if me.team_id in (getattr(b.home_team, "team_id", b.home_team),
                                                   getattr(b.away_team, "team_id", b.away_team)))
    my_s, my_l, w, l, t, opp, opp_s, opp_l = side(box, me.team_id)

    head = f"🔍 第 {period} 週戰況:{me.team_name} vs {opp.team_name}"
    blocks = [(f"目前 {w} 勝 {l} 敗 {t} 和" + ("(本週還沒結束)" if not args.last else ""), False)]

    rows = ["CAT     YOU    OPP"]
    for c in CATS:
        r = (my_s.get(c) or {}).get("result", "")
        rows.append(f"{c:<4}{fmt(c, val(my_s, c)):>7}{fmt(c, val(opp_s, c)):>7} {MARK.get(r, '')}")
    blocks.append(("\n".join(rows), True))

    lost = [c for c in CATS if (my_s.get(c) or {}).get("result") == "LOSS"]
    notes = []

    # 1. 出賽場數
    my_gp, opp_gp = sum(gp(p) for p in my_l), sum(gp(p) for p in opp_l)
    if my_gp or opp_gp:
        diff = my_gp - opp_gp
        tag = "← 比對手少,這是計數類輸掉的主因" if diff <= -3 else "← 比對手多" if diff >= 3 else ""
        notes.append(f"🏀 出賽場數:你 {my_gp:.0f} 場 vs 對手 {opp_gp:.0f} 場 {tag}")

    # 2. 沒出賽的人
    idle = [p for p in my_l if gp(p) == 0]
    if idle:
        notes.append("😴 本週還沒出賽:" + "、".join(
            f"{p.name}({p.slot_position}{',' + p.injuryStatus if p.injuryStatus and p.injuryStatus != 'ACTIVE' else ''})"
            for p in idle))

    # 3. 每個輸掉的類別,雙方的主要貢獻者
    for c in lost:
        if c in ("FG%", "FT%"):
            made, att = ("FGM", "FGA") if c == "FG%" else ("FTM", "FTA")
            bad = [p for p in my_l if (p.points_breakdown.get(att) or 0) >= 5]
            bad.sort(key=lambda p: (p.points_breakdown.get(made, 0) / p.points_breakdown[att]))
            lines = [f"{p.name} {p.points_breakdown.get(made, 0):.0f}/{p.points_breakdown[att]:.0f}"
                     f"({p.points_breakdown.get(made, 0) / p.points_breakdown[att] * 100:.0f}%)" for p in bad[:3]]
            if lines:
                notes.append(f"❌ {ZH[c]} 拖累最多:" + "、".join(lines))
        else:
            mine = "、".join(f"{p.name} {p.points_breakdown.get(c, 0):.0f}" for p in top(my_l, c))
            theirs = "、".join(f"{p.name} {p.points_breakdown.get(c, 0):.0f}" for p in top(opp_l, c))
            gap = val(opp_s, c) - val(my_s, c)
            what = "多" if c == "TO" else "少"
            notes.append(f"❌ {ZH[c]} {what} {abs(gap):.0f}:你靠 {mine or '—'};對手靠 {theirs or '—'}")

    if notes:
        blocks.append(("", False))
        blocks += [(n, False) for n in notes]
    blocks.append(("", False))
    blocks.append(("✅贏 ❌輸 ⚖️和;FG%、FT% 是命中率,TO 越少越好", False))

    print(head + "\n" + "\n".join(t for t, _ in blocks))
    if not args.dry:
        body = "\n".join(f"<pre>{html.escape(t)}</pre>" if pre else html.escape(t) for t, pre in blocks)
        notify(load_config(), head, body, "", html=True)


if __name__ == "__main__":
    main()
