#!/usr/bin/env python3
"""Automated pre-game checklist agent (PAPER MONEY ONLY).

Before each game it studies both teams, key players, recent games, head-to-head and the market,
scores a 16-point checklist, and places a fake bet only if >= 80% of applicable checks pass AND
the 3 mandatory gates pass. Runs from the cloud (GitHub Actions), no laptop needed.

  python3 agent.py run            settle old bets, scan upcoming games, place qualifying paper bets
  python3 agent.py run --dry      score every upcoming game, place nothing  (add --limit N)
  python3 agent.py report         rebuild state/latest_report.md only
"""
import os, sys, json, time, datetime, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
os.environ.setdefault("WALLET_PATH", os.path.join(STATE, "auto_wallet.json"))
sys.path.insert(0, HERE)
import server as S                                     # prices, ESPN research, wallet, settlement

# ------------------------------------------------------------------ settings (edit here)
LEAGUES = ["nba", "ncaam", "wnba", "cwbb", "nhl", "mlb", "nfl", "cfb", "epl", "laliga", "bundesliga", "seriea", "ligue1", "mls", "ucl", "uel",
           "championship", "eredivisie", "portugal", "turkey", "mexico", "brazil", "argentina"]
BASKETBALL = S.BASKETBALL   # full player-level checks; other sports use the checks that apply
PASS_THRESHOLD = 0.80        # share of applicable checks that must pass
MIN_EDGE = 0.04              # Polymarket price must be 4+ points under DraftKings' fair chance
PRACTICE_EDGE = 0.015        # preseason games only: smaller edge, bet tagged PRACTICE, not counted toward the 40-bet verdict
PRACTICE_STAKE = 1.0
PRACTICE_MAX_PER_DAY = 5
MIN_VOLUME = 5000            # dollars traded on the market
PRICE_RANGE = (0.20, 0.80)
STAKE = 2.0                  # dollars per bet (hard cap $5)
MAX_BETS_PER_DAY = 3
WINDOW_HOURS = (0.2, 30)     # only games starting between 12 minutes and 30 hours from now

MANDATORY = {"edge", "volume", "price", "season"}


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def team_by_name(d, outcome):
    ours = next((t for t in d["teams"] if outcome in t["name"] or t["name"].split()[-1] == outcome), None)
    opp = next((t for t in d["teams"] if t is not ours), None)
    return ours, opp


def winpct(t, n=5):
    f = t["form"][:n]
    return sum(1 for x in f if x["win"]) / len(f) if f else None


def margin(t, n=5):
    f = [(num(x["us"]), num(x["them"])) for x in t["form"][:n]]
    f = [a - b for a, b in f if a is not None and b is not None]
    return sum(f) / len(f) if f else None


def rec_pct(t):
    try:
        parts = [int(x) for x in t["record"].split("-")[:3]]
        if len(parts) == 3:                                  # soccer W-D-L
            return parts[0] / sum(parts) if sum(parts) else None
        w, l = parts[:2]
        return w / (w + l) if w + l else None
    except Exception:
        return None


def top5(t):
    return (t.get("key_players") or [])[:5]


def rest_days(t, start):
    if not t["form"]:
        return None
    try:
        last = datetime.date.fromisoformat(t["form"][0]["date"])
        return (start.date() - last).days
    except Exception:
        return None


def evaluate(d, outcome, price, fair, volume, start, practice=False):
    """Return list of checks: (id, group, label, result True/False/None, detail)."""
    ours, opp = team_by_name(d, outcome)
    C = []

    def add(cid, group, label, ok, detail):
        C.append(dict(id=cid, group=group, label=label, ok=ok, detail=detail))

    need = PRACTICE_EDGE if practice else MIN_EDGE
    edge = None if fair is None else fair - price
    add("edge", "Market", f"Price at least {need*100:.1f} pts below DraftKings fair chance", None if edge is None else edge >= need,
        "no DraftKings line" if edge is None else f"PM {price:.0%} vs DK fair {fair:.0%} = {edge*100:+.1f} pts")
    add("volume", "Market", f"Market has ${MIN_VOLUME:,}+ traded", volume >= MIN_VOLUME, f"${volume:,.0f} traded")
    add("price", "Market", f"Price between {PRICE_RANGE[0]:.0%} and {PRICE_RANGE[1]:.0%}", PRICE_RANGE[0] <= price <= PRICE_RANGE[1], f"{price:.0%}")
    st = d.get("season_type")
    add("season", "History", "Regular season or later (preseason = PRACTICE bet only)", True if st is None or st >= 2 or practice else False,
        {1: "preseason: PRACTICE bet", 2: "regular season", 3: "postseason"}.get(st, "season type not reported"))
    if not ours or not opp:
        add("teams", "Team", "Both teams found in research", False, "team lookup failed")
        return C
    L = d.get("lines") or {}
    try:
        ds = abs(float(str(L["pm_spread"]).split("(")[1].rstrip(")")) ) if L.get("pm_spread") else None
        dks = abs(float(str(L["dk_spread"]).split()[-1])) if L.get("dk_spread") else None
        ok = None if ds is None or dks is None else abs(ds - dks) <= 1.5
        det = "n/a" if ok is None else f"PM spread {ds} vs DK {dks}"
    except Exception:
        ok, det = None, "n/a"
    add("lines", "Market", "Polymarket and DraftKings spreads agree (within 1.5)", ok, det)

    a, b = winpct(ours), winpct(opp)
    add("form", "Team", "Last-5 win rate at least the opponent's", None if a is None or b is None else a >= b,
        f"{ours['name']} {a if a is None else format(a, '.0%')} vs {opp['name']} {b if b is None else format(b, '.0%')}")
    ma, mb = margin(ours), margin(opp)
    add("margin", "Team", "Last-5 average margin at least the opponent's", None if ma is None or mb is None else ma >= mb,
        "n/a" if ma is None or mb is None else f"{ma:+.1f} vs {mb:+.1f}")
    add("b2b", "Team", "Not on a back-to-back", not ours["back_to_back"], "played yesterday" if ours["back_to_back"] else "rested")
    ra, rb = rest_days(ours, start), rest_days(opp, start)
    add("rest", "Team", "Rest days at least the opponent's", None if ra is None or rb is None else ra >= rb, f"{ra} vs {rb} days")
    pa, pb = rec_pct(ours), rec_pct(opp)
    add("record", "Team", "Overall record at least the opponent's", None if pa is None or pb is None else pa >= pb,
        f"{ours['record']} vs {opp['record']}")

    ta, tb = top5(ours), top5(opp)
    out_a = [p["name"] for p in ta if p.get("injury") == "Out"]
    dtd_a = [p["name"] for p in ta if p.get("injury") and p["injury"] != "Out"]
    unav_b = [p for p in tb if p.get("injury")]
    add("star_out", "Players", "None of our top-5 scorers is OUT", None if not ta else not out_a, ", ".join(out_a) or "all available")
    add("star_dtd", "Players", "At most 1 top-5 scorer day-to-day", None if not ta else len(dtd_a) <= 1, ", ".join(dtd_a) or "none")
    add("opp_hurt", "Players", "Our top-5 availability at least as good as theirs", None if not ta or not tb else (len(out_a) + len(dtd_a)) <= len(unav_b),
        f"ours {len(out_a)+len(dtd_a)} vs theirs {len(unav_b)} unavailable")
    sa = sum(p["pts"] or 0 for p in ta)
    sb = sum(p["pts"] or 0 for p in tb)
    add("depth", "Players", "Top-5 scoring (last-5 avg) at least the opponent's", None if not ta or not tb else sa >= sb, f"{sa:.0f} vs {sb:.0f} pts")
    all_dtd = [i for i in ours["injuries"] if i["status"] != "Out"]
    add("lineup", "Players", "Lineup not uncertain (under 3 day-to-day players)", len(all_dtd) < 3 if d.get("injury_feed") and d.get("_league") in BASKETBALL else None,
        f"{len(all_dtd)} day-to-day" if d.get("_league") in BASKETBALL and d.get("injury_feed") else "basketball-only check")

    h = d.get("h2h") or []
    wins = 0
    for g in h:
        try:
            a_s, b_s = [num(x) for x in g["score"].split(" - ")]
        except Exception:
            continue
        first_home = g["name"].split(" at ")[-1]            # "A at B": B is home, score listed away-home order
        away_name = g["name"].split(" at ")[0]
        ours_is_home = ours["name"] == first_home
        wins += 1 if ((b_s > a_s) if ours_is_home else (a_s > b_s)) else 0
    add("h2h", "History", "Won at least half of recent meetings", None if not h else wins * 2 >= len(h), f"{wins} of {len(h)} meetings")
    return C


def score(checks):
    # The 80% is measured on the situational checks only (team, players, history, line agreement).
    # Mandatory market gates (edge, volume, price, season) must pass separately; they don't inflate the score.
    ap = [c for c in checks if c["ok"] is not None and c["id"] not in MANDATORY]
    passed = [c for c in ap if c["ok"]]
    pct = len(passed) / len(ap) if ap else 0
    mand_fail = [c["id"] for c in checks if c["id"] in MANDATORY and c["ok"] is False]
    mand_missing = [c["id"] for c in checks if c["id"] in MANDATORY and c["ok"] is None]
    return pct, len(passed), len(ap), mand_fail + mand_missing


def jdump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)


def load_json(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def notify(title, msg, tags="", priority="default"):
    """Push to your phone through the free ntfy app. Topic comes from the NTFY_TOPIC secret."""
    topic = os.environ.get("NTFY_TOPIC")
    print(f"[notify] {title} | {msg}")
    if not topic:
        return
    subprocess.run(["curl", "-s", "--max-time", "15", "-H", f"Title: {title}", "-H", f"Tags: {tags}", "-H", f"Priority: {priority}",
                    "-d", msg, f"https://ntfy.sh/{topic}"], capture_output=True)


def export_dashboard(decisions):
    """Write docs/data.json for the phone dashboard."""
    pf = S.portfolio()
    settled = [p for p in pf["positions"] if p["status"] in ("won", "lost", "sold")]

    def pl(p):
        return (p.get("payout") or 0) - p["stake"] - p.get("fee", 0)
    real = [p for p in settled if not p.get("practice")]
    prac = [p for p in settled if p.get("practice")]
    hist = []
    d = os.path.join(STATE, "decisions")
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d))[-4:]:
            for dc in load_json(os.path.join(d, fn), []):
                if dc["action"] in ("BET", "SKIP"):
                    hist.append(dc)
    out = dict(updated=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               wallet=dict(total=pf["total"], cash=pf["cash"], open_value=pf["open_value"], realized=pf["realized"], start=pf["start"]),
               official=dict(settled=len(real), goal=40, wins=sum(1 for p in real if pl(p) > 0), profit=round(sum(pl(p) for p in real), 2)),
               practice=dict(settled=len(prac), wins=sum(1 for p in prac if pl(p) > 0), profit=round(sum(pl(p) for p in prac), 2)),
               positions=[dict(id=p["id"], status=p["status"], label=", ".join(l["label"] for l in p["legs"]), game=p["legs"][0]["game"], stake=p["stake"],
                               price=p.get("price"), payout=p.get("payout"), value=p.get("value"), practice=bool(p.get("practice")), note=p.get("note", ""),
                               placed_at=p["placed_at"]) for p in pf["positions"][:60]],
               decisions=sorted(hist, key=lambda x: x.get("ts", 0), reverse=True)[:30],
               last_run=[dc for dc in decisions if dc["action"] != "NO EDGE"],
               no_edge=[dc["game"] for dc in decisions if dc["action"] == "NO EDGE"])
    jdump(os.path.join(HERE, "docs", "data.json"), out)


def run(dry=False, limit=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    before = {p["id"]: p["status"] for p in S.load_wallet()["positions"]}
    with S.wlock:
        w = S.refresh(S.load_wallet())
        if not dry:
            S.save_wallet(w)
    if not dry:
        for p in w["positions"]:
            was, now_ = before.get(p["id"]), p["status"]
            if was in ("open", "pending") and now_ in ("won", "lost"):
                prof = (p.get("payout") or 0) - p["stake"] - p.get("fee", 0)
                notify(f"{'WON' if now_ == 'won' else 'LOST'} {prof:+.2f}" + (" (practice)" if p.get("practice") else ""),
                       f"{p['legs'][0]['label']}: {p['legs'][0]['game']}", "white_check_mark" if now_ == "won" else "x")
            elif was == "pending" and now_ == "open":
                notify("Limit order filled", f"{p['legs'][0]['label']}: {p['legs'][0]['game']}", "ballot_box_with_check")
    decisions = []
    existing = {l["market_id"] for p in w["positions"] for l in p["legs"]}
    today = now.date().isoformat()
    placed_today = sum(1 for p in w["positions"] if datetime.datetime.fromtimestamp(p["placed_at"], datetime.timezone.utc).date().isoformat() == today)
    n_games = 0
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:                        # fetch every league's board in parallel
        boards = dict(zip(LEAGUES, ex.map(lambda l: S.games(l), LEAGUES)))
    for lg in LEAGUES:
        for g in boards[lg]:
            st = datetime.datetime.fromisoformat(g["start"])
            hrs = (st - now).total_seconds() / 3600
            if not (WINDOW_HOURS[0] <= hrs <= WINDOW_HOURS[1]) or not g.get("dk_fair"):
                continue
            ml = g["ml"]
            practice = g.get("season_type") == 1
            cap_day = PRACTICE_MAX_PER_DAY if practice else MAX_BETS_PER_DAY
            need = PRACTICE_EDGE if practice else MIN_EDGE
            cands = []
            for i, (name, price) in enumerate(zip(ml["outcomes"], ml["prices"])):
                fair = S.fair_lookup(g["dk_fair"], name)
                if fair is None or name == "Draw":              # never bet draws: no team research applies
                    continue
                if dry or fair - price >= need:
                    cands.append((i, name, price, fair))
            if not cands:
                decisions.append(dict(league=lg, game=g["title"], start=g["start"], action="NO EDGE", note="Polymarket price within 4 pts of DraftKings fair chance"))
                continue
            if limit and n_games >= limit:
                break
            n_games += 1
            d = S.dossier(lg, g["id"])
            d["_league"] = lg
            if not d.get("teams"):
                decisions.append(dict(league=lg, game=g["title"], start=g["start"], action="SKIP", note="no research data"))
                continue
            best = None
            for i, name, price, fair in cands:
                checks = evaluate(d, name, price, fair, g["volume"], st, practice)
                pct, np_, na, blockers = score(checks)
                rec = dict(i=i, name=name, price=price, fair=round(fair, 3), pct=round(pct, 3), passed=np_, applicable=na, blockers=blockers, checks=checks)
                if best is None or (not blockers, pct) > (not best["blockers"], best["pct"]):
                    best = rec
            action = "SKIP"
            reason = ""
            if best["blockers"]:
                reason = "mandatory gate failed: " + ", ".join(best["blockers"])
            elif best["pct"] < PASS_THRESHOLD:
                reason = f"only {best['pct']:.0%} of checks passed (need {PASS_THRESHOLD:.0%})"
            elif ml["mids"][best["i"]] in existing or any(m in existing for m in ml["mids"]):
                reason = "already have a bet on this game"
            elif placed_today >= cap_day:
                reason = f"daily limit of {cap_day} bets reached"
            else:
                action = "BET"
            dec = dict(league=lg, game=g["title"], start=g["start"], action=action, selection=best["name"], price=best["price"], fair=best["fair"],
                       pass_pct=best["pct"], passed=best["passed"], applicable=best["applicable"], reason=reason, checks=best["checks"], warnings=d.get("warnings", []))
            if action == "BET" and not dry:
                fails = [c["label"] for c in best["checks"] if c["ok"] is False]
                note = f"{'PRACTICE ' if practice else ''}AUTO {best['pct']:.0%} ({best['passed']}/{best['applicable']}). DK fair {best['fair']:.0%} vs PM {best['price']:.0%}." + (f" Failed: {'; '.join(fails)}" if fails else "")
                code, res = S._place(dict(legs=[dict(market_id=ml["mids"][best["i"]], idx=ml["idxs"][best["i"]], label=f"{best['name']} to win", game=g["title"], league=lg)],
                                          stake=PRACTICE_STAKE if practice else STAKE, order="market", note=note), False)
                dec["placed"] = res
                if code == 200:
                    notify(f"{'PRACTICE ' if practice else ''}BET: {best['name']} to win",
                           f"{g['title']} · ${(PRACTICE_STAKE if practice else STAKE):.0f} at {best['price']:.0%} (DK fair {best['fair']:.0%}) · checklist {best['passed']}/{best['applicable']}",
                           "basketball" if lg in BASKETBALL else "soccer", "high")
                    placed_today += 1
                    existing.update(ml["mids"])
                    with S.wlock:                                  # attach the checklist note to the position
                        w2 = S.load_wallet()
                        for p in w2["positions"]:
                            if p["id"] == res["id"]:
                                p["note"] = note
                                p["practice"] = practice
                        S.save_wallet(w2)
            decisions.append(dec)
    if not dry:
        log_path = os.path.join(STATE, "decisions", f"{today}.json")
        log = load_json(log_path, [])
        for dc in decisions:
            dc["ts"] = int(time.time())
        log += decisions
        jdump(log_path, log)
    write_report(decisions, dry)
    if not dry:
        export_dashboard(decisions)
    return decisions


def write_report(decisions, dry=False):
    pf = S.portfolio()
    lines = [f"# Paper agent report{' (DRY RUN)' if dry else ''}", f"_{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC_", "",
             f"**Wallet:** total ${pf['total']:.2f} (start $100) · cash ${pf['cash']:.2f} · open ${pf['open_value']:.2f} · realized ${pf['realized']:+.2f}", ""]
    settled = [p for p in pf["positions"] if p["status"] in ("won", "lost", "sold")]
    real = [p for p in settled if not p.get("practice")]
    prac = [p for p in settled if p.get("practice")]
    lines += [f"**Official test:** {len(real)} / 40 settled bets · wins {sum(1 for p in real if (p.get('payout') or 0) > p['stake'])} · profit ${sum((p.get('payout') or 0) - p['stake'] - p.get('fee', 0) for p in real):+.2f}",
              f"**Practice (preseason, not counted):** {len(prac)} settled · profit ${sum((p.get('payout') or 0) - p['stake'] - p.get('fee', 0) for p in prac):+.2f}", ""]
    bets = [d for d in decisions if d["action"] == "BET"]
    lines.append(f"## This run: {len(bets)} bet(s), {sum(1 for d in decisions if d['action']=='SKIP')} skipped, {sum(1 for d in decisions if d['action']=='NO EDGE')} no edge")
    for d in decisions:
        if d["action"] == "NO EDGE":
            lines.append(f"- {d['game']}: no edge. {d['note']}")
            continue
        lines += ["", f"### {d['action']}: {d['game']} → {d.get('selection','')}",
                  f"Price {d.get('price',0):.0%} · DK fair {d.get('fair',0):.0%} · checklist {d.get('passed','?')}/{d.get('applicable','?')} ({d.get('pass_pct',0):.0%})" + (f" · {d['reason']}" if d.get("reason") else "")]
        for w_ in d.get("warnings", []):
            lines.append(f"- ⚠ {w_}")
        for c in d.get("checks", []):
            mark = "✅" if c["ok"] else ("➖" if c["ok"] is None else "❌")
            lines.append(f"- {mark} **{c['group']}** {c['label']}: {c['detail']}")
    lines += ["", "## Open and recent positions"]
    for p in pf["positions"][:15]:
        lines.append(f"- #{p['id']}{' [PRACTICE]' if p.get('practice') else ''} {p['status'].upper()} {', '.join(l['label'] for l in p['legs'])} ({p['legs'][0]['game']}) ${p['stake']:.2f}" + (f" → ${p['payout']:.2f}" if p.get("payout") is not None else ""))
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "latest_report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ("run", "report"):
        sys.exit(__doc__)
    if a[0] == "report":
        write_report([])
    else:
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else None
        out = run(dry="--dry" in a, limit=lim)
        for d in out:
            print(d["action"], "|", d["game"], "|", d.get("selection", ""), "|", f"{d.get('pass_pct', 0):.0%}" if d.get("checks") else "", "|", d.get("reason") or d.get("note", ""))
