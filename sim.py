#!/usr/bin/env python3
"""Paper-money sports betting simulator. Free ESPN data, no keys, no real money.

  python3 sim.py games [YYYYMMDD] [league]      list games + lines (default today, nba)
  python3 sim.py daily                           morning run: settle + report + value + injuries
  python3 sim.py brief [YYYYMMDD] [league]      injuries + back-to-back for each game
  python3 sim.py value [YYYYMMDD] [league]      DraftKings vs Polymarket side by side, flags value
  python3 sim.py bet <gameid> <type> <side> <line> <odds> [stake] [reason...]
        type: spread | total | ml     side: team abbr (spread/ml) or over/under (total)
        e.g.  sim.py bet 401810000 spread BOS -3.5 -110 5 "rest edge"
  python3 sim.py settle                          grade finished games automatically
  python3 sim.py report                          bankroll, win rate, profit by bet type
"""
import csv, json, sys, os, datetime, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "paper_bets.csv")
START = 100.0
MAX_STAKE = 5.0
FIELDS = ["id", "date", "league", "game_id", "game", "bet_type", "side", "line", "odds",
          "stake", "reason", "status", "result", "profit"]
LEAGUES = {"euroleague": "basketball/euroleague", "nba": "basketball/nba", "wnba": "basketball/wnba",
           "ncaam": "basketball/mens-college-basketball",
           "epl": "soccer/eng.1", "laliga": "soccer/esp.1", "ucl": "soccer/uefa.champions"}


def get(league, date):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUES[league]}/scoreboard?dates={date}"
    out = subprocess.run(["curl", "-s", "--max-time", "20", url], capture_output=True, text=True).stdout
    return json.loads(out)


def load():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("id")]
    return rows


def save(rows):
    with open(LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def payout(stake, odds):
    return stake * (odds / 100 if odds > 0 else 100 / -odds)


PM_SERIES = {"nba": 10345, "wnba": 10105, "ncaam": 10470, "epl": 10188, "ucl": 10204, "euroleague": 10371}


def pm_events(league):
    """Polymarket public read-only prices (no account needed)."""
    sid = PM_SERIES.get(league)
    if not sid:
        return []
    url = f"https://gamma-api.polymarket.com/events?series_id={sid}&active=true&closed=false&limit=100"
    out = subprocess.run(["curl", "-s", "--max-time", "30", url], capture_output=True, text=True).stdout
    return json.loads(out) if out else []


def ml_prob(s):
    try:
        o = float(s)
    except (TypeError, ValueError):
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def cmd_value(date=None, league="nba"):
    """Side-by-side: DraftKings fair win chance vs Polymarket price. Flags value."""
    date = date or datetime.date.today().strftime("%Y%m%d")
    pm = pm_events(league)
    for e in get(league, date)["events"]:
        c = e["competitions"][0]
        teams = {x["homeAway"]: x["team"] for x in c["competitors"]}
        o = (c.get("odds") or [{}])[0].get("moneyline", {})
        ph = ml_prob(o.get("home", {}).get("close", {}).get("odds"))
        pa = ml_prob(o.get("away", {}).get("close", {}).get("odds"))
        fair = {}
        if ph and pa:
            fair = {teams["home"]["name"]: ph / (ph + pa), teams["away"]["name"]: pa / (ph + pa)}
        names = [t["name"] for t in teams.values()]
        ev = next((x for x in pm if all(n in x["title"] for n in names)
                   and abs((datetime.datetime.fromisoformat(
                       (x["markets"][0].get("gameStartTime") or x["startDate"]).replace("Z", "+00:00").replace(" ", "T")[:19]
                       + "+00:00").date() - datetime.datetime.strptime(date, "%Y%m%d").date()).days) <= 1), None)
        print(f'\n{e["id"]}  {names[1]} @ {names[0]}  [{e["status"]["type"]["name"]}]')
        if not ev:
            print("   Polymarket: no market found"); 
        else:
            m = ev["markets"][0]
            outs, prices = json.loads(m["outcomes"]), [float(x) for x in json.loads(m["outcomePrices"])]
            vol = float(ev.get("volume") or 0)
            thin = "  THIN (under $1,000 traded: price not trustworthy)" if vol < 1000 else ""
            for n, pr in zip(outs, prices):
                f = next((v for k, v in fair.items() if k in n or n in k), None)
                edge = f"  fair {f:.0%}  edge {f - pr:+.0%}" + ("  <-- VALUE" if f - pr >= 0.04 and not thin else "") if f else "  (no DraftKings moneyline yet)"
                print(f"   {n}: Polymarket {pr:.0%}{edge}")
            print(f"   volume ${vol:,.0f}{thin}")


def cmd_brief(date=None, league="nba"):
    """Injuries + back-to-back check for every game: the info the market may miss."""
    date = date or datetime.date.today().strftime("%Y%m%d")
    prev = (datetime.datetime.strptime(date, "%Y%m%d") - datetime.timedelta(days=1)).strftime("%Y%m%d")
    played = {x["team"]["abbreviation"] for e in get(league, prev)["events"]
              for x in e["competitions"][0]["competitors"]}
    inj = {}
    out = subprocess.run(["curl", "-s", "--max-time", "20",
                          f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUES[league]}/injuries"],
                         capture_output=True, text=True).stdout
    for t in (json.loads(out).get("injuries", []) if out else []):
        inj[t["displayName"]] = [f'{i["athlete"]["displayName"]} ({i["status"]})' for i in t["injuries"]]
    for e in get(league, date)["events"]:
        print(f'\n{e["name"]}')
        for x in e["competitions"][0]["competitors"]:
            t = x["team"]
            b2b = "  BACK-TO-BACK" if t["abbreviation"] in played else ""
            hurt = inj.get(t["displayName"], [])
            print(f'   {t["displayName"]}{b2b}: ' + (", ".join(hurt[:6]) if hurt else "no injuries listed"))


def cmd_daily(league="nba"):
    """One-shot morning run: grade yesterday's bets, show value, show injuries."""
    cmd_settle()
    cmd_report()
    print("\n=== VALUE ==="); cmd_value(None, league)
    print("\n=== INJURIES / REST ==="); cmd_brief(None, league)


def cmd_games(date=None, league="nba"):
    date = date or datetime.date.today().strftime("%Y%m%d")
    for e in get(league, date)["events"]:
        c = e["competitions"][0]
        o = (c.get("odds") or [{}])[0]
        t = {x["homeAway"]: x["team"]["abbreviation"] for x in c["competitors"]}
        print(f'{e["id"]}  {t.get("away")} @ {t.get("home")}  [{e["status"]["type"]["name"]}]  '
              f'spread {o.get("details","n/a")}  total {o.get("overUnder","n/a")}')


def cmd_bet(gid, btype, side, line, odds, stake=str(MAX_STAKE), *reason):
    stake = float(stake)
    if stake > MAX_STAKE:
        sys.exit(f"Rule: max stake is ${MAX_STAKE:.0f}")
    rows = load()
    today = datetime.date.today()
    if sum(1 for r in rows if r["date"] == str(today)) >= 3:
        sys.exit("Rule: max 3 bets per day")
    league = next((l for l in LEAGUES if _find(l, gid)), None)
    if not league:
        sys.exit("Game not found on today's/tomorrow's board; use games first")
    game = _find(league, gid)
    rows.append(dict(id=len(rows) + 1, date=today, league=league, game_id=gid, game=game,
                     bet_type=btype, side=side, line=line, odds=odds, stake=stake,
                     reason=" ".join(reason), status="open", result="", profit=""))
    save(rows)
    print(f"Logged #{len(rows)}: {game} | {btype} {side} {line} @ {odds} | ${stake:.2f}")


def _find(league, gid):
    for d in (datetime.date.today(), datetime.date.today() + datetime.timedelta(days=1)):
        try:
            for e in get(league, d.strftime("%Y%m%d"))["events"]:
                if e["id"] == gid:
                    return e["name"]
        except Exception:
            pass
    return None


def cmd_settle():
    rows = load()
    cache = {}
    n = 0
    for r in rows:
        if r["status"] != "open":
            continue
        d0 = datetime.date.fromisoformat(r["date"])
        for off in (0, 1, 2):
            d = (d0 + datetime.timedelta(days=off)).strftime("%Y%m%d")
            key = (r["league"], d)
            cache.setdefault(key, get(*key)["events"])
            ev = next((e for e in cache[key] if e["id"] == r["game_id"]), None)
            if ev:
                break
        if not ev or ev["status"]["type"]["name"] != "STATUS_FINAL":
            continue
        sc = {x["team"]["abbreviation"]: float(x["score"]) for x in ev["competitions"][0]["competitors"]}
        stake, odds = float(r["stake"]), float(r["odds"])
        if r["bet_type"] == "total":
            tot, line = sum(sc.values()), float(r["line"])
            diff = (tot - line) if r["side"].lower() == "over" else (line - tot)
        else:
            me = sc[r["side"]]
            opp = sum(v for k, v in sc.items() if k != r["side"])
            diff = (me - opp + float(r["line"])) if r["bet_type"] == "spread" else (me - opp)
        res = "win" if diff > 0 else "push" if diff == 0 else "loss"
        r["status"], r["result"] = "settled", res
        r["profit"] = round(payout(stake, odds) if res == "win" else 0 if res == "push" else -stake, 2)
        n += 1
    save(rows)
    print(f"Settled {n} bet(s)")


def cmd_report():
    rows = [r for r in load()]
    done = [r for r in rows if r["status"] == "settled"]
    profit = sum(float(r["profit"]) for r in done)
    wins = sum(r["result"] == "win" for r in done)
    losses = sum(r["result"] == "loss" for r in done)
    print(f"Bankroll ${START + profit:.2f} (start ${START:.0f}) | profit ${profit:+.2f}")
    print(f"Settled {len(done)} ({wins}W-{losses}L) | open {len(rows) - len(done)} | goal 40 bets: "
          f"[{'#' * min(len(done), 40)}{'.' * max(0, 40 - len(done))}] {len(done)}/40")
    for k in sorted({r["bet_type"] for r in done}):
        sub = [r for r in done if r["bet_type"] == k]
        print(f"  {k:7s} {len(sub):3d} bets  profit ${sum(float(r['profit']) for r in sub):+.2f}")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmds = {"daily": cmd_daily, "brief": cmd_brief, "value": cmd_value, "games": cmd_games, "bet": cmd_bet, "settle": cmd_settle, "report": cmd_report}
    if not a or a[0] not in cmds:
        sys.exit(__doc__)
    cmds[a[0]](*a[1:])
