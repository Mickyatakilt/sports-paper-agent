#!/usr/bin/env python3
"""Paper Market: a local mock of Polymarket sports trading with FAKE money.
Live prices from Polymarket's public data, research from ESPN. Run: python3 server.py
Open http://localhost:8765
"""
import json, os, re, subprocess, threading, time, urllib.parse, datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
WALLET = os.environ.get("WALLET_PATH", os.path.join(HERE, "wallet.json"))
START_CASH = 100.0
MAX_STAKE = 5.0
FEE_RATE = 0.05          # taker fee estimate: 5% * p * (1-p) per share
SPREAD_COST = 0.01       # selling early gets mid price minus 1 cent
PORT = 8765
REVIEW_MIN_SECONDS = int(os.environ.get("REVIEW_MIN_SECONDS", "1800"))   # 30 minutes per game
MIN_EDGE = 0.04          # your estimate must beat the price by 4+ points
MIN_REASON_CHARS = 60
REVIEWS = os.path.join(HERE, "reviews.json")
rlock = threading.Lock()


def load_reviews():
    if os.path.exists(REVIEWS):
        with open(REVIEWS) as f:
            return json.load(f)
    return {"starts": {}, "reviews": {}}


def save_reviews(r):
    with open(REVIEWS, "w") as f:
        json.dump(r, f, indent=1)


LEAGUES = {
    "nba": dict(name="NBA", pm=10345, espn="basketball/nba"),
    "wnba": dict(name="WNBA", pm=10105, espn="basketball/wnba"),
    "ncaam": dict(name="College (CBB)", pm=10470, espn="basketball/mens-college-basketball"),
    "euroleague": dict(name="EuroLeague", pm=10371, espn=None),
    "nfl": dict(name="NFL", pm=12185, espn="football/nfl"),
    "epl": dict(name="EPL", pm=10188, espn="soccer/eng.1"),
    "laliga": dict(name="La Liga", pm=10193, espn="soccer/esp.1"),
    "bundesliga": dict(name="Bundesliga", pm=10194, espn="soccer/ger.1"),
    "seriea": dict(name="Serie A", pm=10203, espn="soccer/ita.1"),
    "ligue1": dict(name="Ligue 1", pm=10195, espn="soccer/fra.1"),
    "mls": dict(name="MLS", pm=10189, espn="soccer/usa.1"),
    "ucl": dict(name="Champions League", pm=10204, espn="soccer/uefa.champions"),
}
BASKETBALL = ("nba", "wnba", "ncaam")

_cache, _lock = {}, threading.Lock()


def fetch(url, ttl=25):
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    out = subprocess.run(["curl", "-s", "--max-time", "25", url], capture_output=True, text=True).stdout
    try:
        data = json.loads(out)
    except Exception:
        data = None
    with _lock:
        _cache[url] = (now, data)
    return data


def jl(x):
    return json.loads(x) if isinstance(x, str) else (x or [])


# ---------------------------------------------------------------- wallet
def load_wallet():
    if os.path.exists(WALLET):
        with open(WALLET) as f:
            return json.load(f)
    return {"cash": START_CASH, "next_id": 1, "positions": []}


def save_wallet(w):
    os.makedirs(os.path.dirname(WALLET), exist_ok=True)
    with open(WALLET, "w") as f:
        json.dump(w, f, indent=1)


wlock = threading.Lock()


# ---------------------------------------------------------------- polymarket
def pm_events(league):
    sid = LEAGUES[league]["pm"]
    d = fetch(f"https://gamma-api.polymarket.com/events?series_id={sid}&active=true&closed=false&limit=200", 20)
    return d if isinstance(d, list) else []


def market_view(m):
    outs, prices = jl(m.get("outcomes")), [float(x) for x in jl(m.get("outcomePrices"))]
    return dict(id=str(m["id"]), type=m.get("sportsMarketType") or "", question=m.get("question", ""), group=m.get("groupItemTitle") or "",
                outcomes=outs, prices=prices, closed=bool(m.get("closed")),
                line=_line(m.get("question", ""), m.get("sportsMarketType")))


def _line(q, t):
    mt = re.search(r"\(([+-]?\d+\.?\d*)\)", q) if t == "spreads" else re.search(r"O/U (\d+\.?\d*)", q)
    return float(mt.group(1)) if mt else None


def start_time(e):
    for m in e.get("markets", []):
        if m.get("gameStartTime"):
            return m["gameStartTime"].replace(" ", "T").replace("+00", "+00:00") if "+00:" not in m["gameStartTime"] else m["gameStartTime"].replace(" ", "T")
    return e.get("startTime") or e.get("startDate") or ""


def event_view(e, full=False):
    ms = [market_view(m) for m in e.get("markets", [])]
    mls = [m for m in ms if m["type"] == "moneyline"]
    ml = None
    if len(mls) == 1:
        ml = dict(mls[0], mids=[mls[0]["id"]] * len(mls[0]["outcomes"]), idxs=list(range(len(mls[0]["outcomes"]))))
    elif len(mls) >= 2 and all(m["outcomes"][:1] == ["Yes"] for m in mls):      # soccer: one Yes/No market per outcome
        mls = sorted(mls, key=lambda m: ("draw" in (m["group"] or m["question"]).lower(), m["id"]))
        names = [(m["group"] or re.sub(r"^Will | win.*$", "", m["question"])).strip() for m in mls]
        names = ["Draw" if re.match(r"(?i)^(draw|tie)\b", n) else n for n in names]
        ml = dict(id="multi", type="moneyline", question=e["title"], group="", outcomes=names, prices=[m["prices"][0] for m in mls],
                  closed=False, line=None, mids=[m["id"] for m in mls], idxs=[0] * len(mls))
    teams = [t.strip() for t in re.split(r"\s+vs\.?\s+", e["title"])] if " vs" in e["title"] else [e["title"]]
    v = dict(id=str(e["id"]), title=e["title"], teams=teams, start=start_time(e),
             volume=float(e.get("volume") or 0), ml=ml, n_markets=len(ms), ml_markets=[m for m in ms if m["type"] == "moneyline"])
    if full:
        v["spreads"] = sorted([m for m in ms if m["type"] == "spreads"], key=lambda m: m["line"] or 0)
        v["totals"] = sorted([m for m in ms if m["type"] == "totals"], key=lambda m: m["line"] or 0)
        v["other"] = [m for m in ms if m["type"] in ("basketball_team_to_score_first", "basketball_odd_even",
                                                    "first_half_moneyline", "soccer_halftime_result")]
    return v


# ---------------------------------------------------------------- espn
def espn_board(league, date):
    p = LEAGUES[league]["espn"]
    if not p:
        return []
    d = fetch(f"https://site.api.espn.com/apis/site/v2/sports/{p}/scoreboard?dates={date}", 60)
    return (d or {}).get("events", [])


def ml_prob(s):
    try:
        o = float(s)
    except (TypeError, ValueError):
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def fair_lookup(fair, name):
    """Match a Polymarket outcome name to a DraftKings fair price. Draws never match team names."""
    if re.match(r"(?i)^(draw|tie)\b", name):
        return fair.get("Draw")
    for k, v in fair.items():
        if k != "Draw" and (k in name or name in k):
            return v
    return None


def dk_fair(ev):
    """DraftKings no-vig win chances by team nickname (and 'Draw' for soccer), from ESPN."""
    c = ev["competitions"][0]
    o = (c.get("odds") or [{}])[0]
    t = {x["homeAway"]: x["team"]["name"] for x in c["competitors"]}
    ml = o.get("moneyline", {})
    ph = ml_prob(ml.get("home", {}).get("close", {}).get("odds"))
    pa = ml_prob(ml.get("away", {}).get("close", {}).get("odds"))
    pd = ml_prob(ml.get("draw", {}).get("close", {}).get("odds"))
    if not (ph and pa):
        ph = ml_prob((o.get("homeTeamOdds") or {}).get("moneyLine"))
        pa = ml_prob((o.get("awayTeamOdds") or {}).get("moneyLine"))
        pd = ml_prob((o.get("drawOdds") or {}).get("moneyLine"))
    if not (ph and pa):
        return {}
    probs = {t["home"]: ph, t["away"]: pa}
    if pd:
        probs["Draw"] = pd
    tot = sum(probs.values())
    return {k: v / tot for k, v in probs.items()}


def match_espn(league, ev_view):
    """Find the ESPN event for a Polymarket event by team nicknames and date."""
    day = ev_view["start"][:10].replace("-", "")
    if not day:
        return None
    for d in {day, (datetime.datetime.strptime(day, "%Y%m%d") - datetime.timedelta(days=1)).strftime("%Y%m%d")}:
        for e in espn_board(league, d):
            names = [x["team"]["name"] for x in e["competitions"][0]["competitors"]]
            if all(n in ev_view["title"] for n in names):
                return e
    return None


def games(league):
    out = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for e in pm_events(league):
        if " - " in e["title"]:                      # "- Total Corners", "- More Markets" side events
            continue
        v = event_view(e)
        if not v["ml"] or not v["start"]:
            continue
        try:
            st = datetime.datetime.fromisoformat(v["start"])
        except Exception:
            continue
        if st < now - datetime.timedelta(hours=4) or st > now + datetime.timedelta(days=5):
            continue
        ee = match_espn(league, v)
        v["espn_id"] = ee["id"] if ee else None
        v["season_type"] = (ee.get("season") or {}).get("type") if ee else None
        v["status"] = ee["status"]["type"]["name"] if ee else ""
        if ee:
            comp = ee["competitions"][0]["competitors"]
            v["score"] = {x["team"]["name"]: x.get("score") for x in comp}
            fair = dk_fair(ee)
            v["dk_fair"] = fair
            v["value"] = []
            if v["volume"] >= 1000:
                for n, p in zip(v["ml"]["outcomes"], v["ml"]["prices"]):
                    f = fair_lookup(fair, n)
                    if f is not None and f - p >= 0.04:
                        v["value"].append(dict(team=n, price=p, fair=round(f, 3), edge=round(f - p, 3)))
        out.append(v)
    out.sort(key=lambda g: g["start"])
    return out


def find_event(league, eid):
    for e in pm_events(league):
        if str(e["id"]) == eid:
            return e
    return None


def research(league, eid):
    e = find_event(league, eid)
    if not e:
        return {}
    v = event_view(e)
    ee = match_espn(league, v)
    res = {"teams": []}
    if ee:
        res["season_type"] = (ee.get("season") or {}).get("type")
    if not ee:
        res["note"] = "No ESPN research available for this league."
        return res
    base = LEAGUES[league]["espn"]
    inj = fetch(f"https://site.api.espn.com/apis/site/v2/sports/{base}/injuries", 600) or {}
    injmap = {t["displayName"]: t["injuries"] for t in inj.get("injuries", [])}
    res["injury_feed"] = bool(injmap)
    for x in ee["competitions"][0]["competitors"]:
        t = x["team"]
        info = dict(tid=t["id"], name=t["displayName"], logo=t.get("logo"), record=(x.get("records") or [{}])[0].get("summary", ""),
                    homeAway=x["homeAway"], form=[], news=[], injuries=[])
        sch = (fetch(f"https://site.api.espn.com/apis/site/v2/sports/{base}/teams/{t['id']}/schedule", 300) or {}).get("events", [])
        done = [s for s in sch if s["competitions"][0]["status"]["type"].get("completed")]
        for s in done[-6:][::-1]:
            c = s["competitions"][0]["competitors"]
            me = next(y for y in c if str(y.get("id")) == str(t["id"]))
            op = next(y for y in c if str(y.get("id")) != str(t["id"]))
            sc = lambda y: (y.get("score") or {}).get("displayValue", y.get("score")) if isinstance(y.get("score"), dict) else y.get("score")
            info["form"].append(dict(date=s["date"][:10], opp=op["team"]["displayName"], home=me["homeAway"] == "home",
                                     us=sc(me), them=sc(op), win=bool(me.get("winner"))))
        nw = fetch(f"https://site.api.espn.com/apis/site/v2/sports/{base}/news?team={t['id']}&limit=5", 600) or {}
        info["news"] = [dict(h=a["headline"], url=(a.get("links", {}).get("web", {}) or {}).get("href", ""),
                             pub=a.get("published", "")[:10]) for a in nw.get("articles", [])]
        for i in injmap.get(t["displayName"], []):
            info["injuries"].append(dict(id=str(i["athlete"].get("id") or (i["athlete"].get("links") or [{}])[0].get("href", "").rstrip("/").split("/")[-2:][0] if False else i["athlete"].get("id", "")), name=i["athlete"]["displayName"], status=i["status"],
                                         note=i.get("shortComment", "")[:160]))
        ro = (fetch(f"https://site.api.espn.com/apis/site/v2/sports/{base}/teams/{t['id']}/roster", 600) or {}).get("athletes", [])
        info["roster"] = [dict(id=a["id"], name=a["displayName"], pos=(a.get("position") or {}).get("abbreviation", ""))
                          for a in ro if isinstance(a, dict) and "id" in a][:18]
        # back-to-back
        day = ee["date"][:10]
        yday = (datetime.datetime.fromisoformat(day) - datetime.timedelta(days=1)).date().isoformat()
        info["back_to_back"] = any(s["date"][:10] == yday for s in done)
        res["teams"].append(info)
    res["dk"] = dict(fair=dk_fair(ee), details=(ee["competitions"][0].get("odds") or [{}])[0].get("details"),
                     total=(ee["competitions"][0].get("odds") or [{}])[0].get("overUnder"))
    return res


def player(league, pid):
    base = LEAGUES[league]["espn"]
    gl = fetch(f"https://site.web.api.espn.com/apis/common/v3/sports/{base}/athletes/{pid}/gamelog", 600) or {}
    names, evs = gl.get("names", []), gl.get("events", {})
    rows = []
    for st in gl.get("seasonTypes", []):
        for cat in st.get("categories", []):
            for r in cat.get("events", []):
                meta = evs.get(r["eventId"], {})
                s = dict(zip(names, r["stats"]))
                rows.append(dict(date=meta.get("gameDate", "")[:10], opp=(meta.get("opponent") or {}).get("abbreviation", ""),
                                 result=meta.get("gameResult", ""), min=s.get("minutes"), pts=s.get("points"),
                                 reb=s.get("totalRebounds"), ast=s.get("assists")))
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows = rows[:8]
    def avg(k):
        vals = [float(r[k]) for r in rows[:5] if r[k] not in (None, "")]
        return round(sum(vals) / len(vals), 1) if vals else None
    return dict(games=rows, avg=dict(pts=avg("pts"), reb=avg("reb"), ast=avg("ast"), min=avg("min")))


def dossier(league, eid):
    """Everything to read before a bet: research + key players + lines + head-to-head + warnings."""
    from concurrent.futures import ThreadPoolExecutor
    r = research(league, eid)
    e = find_event(league, eid)
    if not e or not r.get("teams"):
        return r
    v = event_view(e, True)
    base = LEAGUES[league]["espn"]
    # key players: last-5 averages for up to 10 rostered players per team, in parallel
    def pl(a):
        try:
            g = player(league, a["id"])
        except Exception:
            return None
        return dict(id=a["id"], name=a["name"], pos=a["pos"], **g["avg"], last=g["games"][:5])
    with ThreadPoolExecutor(20) as ex:
        futs = [[ex.submit(pl, a) for a in (t["roster"][:10] if league in BASKETBALL else [])] for t in r["teams"]]
        results = [[f.result() for f in fs] for fs in futs]
    for t, res in zip(r["teams"], results):
        rows = [x for x in res if x and x.get("pts") is not None]
        hurt = {i["name"]: i for i in t["injuries"]}
        for x in rows:
            x["injury"] = hurt.get(x["name"], {}).get("status")
        t["key_players"] = sorted(rows, key=lambda x: -(x["pts"] or 0))[:8]
    # line comparison
    def mid(ms):
        return min(ms, key=lambda m: abs(m["prices"][0] - 0.5)) if ms else None
    ms, mt = mid(v["spreads"]), mid(v["totals"])
    r["lines"] = dict(pm_moneyline={n: p for n, p in zip(v["ml"]["outcomes"], v["ml"]["prices"])} if v["ml"] else {},
                      pm_spread=ms["question"] if ms else None, pm_total=mt["line"] if mt else None,
                      dk_spread=r["dk"].get("details"), dk_total=r["dk"].get("total"), dk_fair=r["dk"].get("fair"))
    # head to head (last season + this season)
    names = [t["name"] for t in r["teams"]]
    h2h = []
    for t in r["teams"]:
        for season in ("", "?season=2026"):
            sch = (fetch(f"https://site.api.espn.com/apis/site/v2/sports/{base}/teams/{t['tid']}/schedule{season}", 600) or {}).get("events", []) if t.get("tid") else []
            for sv in sch:
                c = sv["competitions"][0]
                if c["status"]["type"].get("completed") and all(n in sv["name"] for n in [x.split()[-1] for x in names]):
                    h2h.append(dict(date=sv["date"][:10], name=sv["name"], score=" - ".join(str((x.get("score") or {}).get("displayValue", x.get("score"))) for x in c["competitors"])))
    seen, uniq = set(), []
    for x in h2h:
        if x["date"] not in seen:
            seen.add(x["date"]); uniq.append(x)
    r["h2h"] = sorted(uniq, key=lambda x: x["date"], reverse=True)[:6]
    # warnings
    w = []
    if v["volume"] < 1000:
        w.append("THIN market: under $1,000 traded, so the price is not trustworthy.")
    for t in r["teams"]:
        if t["back_to_back"]:
            w.append(f"{t['name']} played yesterday (back-to-back).")
        out = [i["name"] for i in t["injuries"] if i["status"] == "Out"]
        dtd = [i["name"] for i in t["injuries"] if i["status"] != "Out"]
        if out:
            w.append(f"{t['name']} OUT: {', '.join(out[:5])}")
        if len(dtd) >= 3:
            w.append(f"{t['name']} has {len(dtd)} day-to-day players (rest/injury): lineup uncertain.")
    now = datetime.datetime.now(datetime.timezone.utc)
    if league == "nba" and r.get("season_type") == 1:
        w.append("NBA PRESEASON: starters often rest and play few minutes. Results are noisy.")
    r["warnings"] = w
    r["event_id"] = eid
    return r


def review_start(league, eid):
    with rlock:
        d = load_reviews()
        d["starts"].setdefault(f"{league}:{eid}", int(time.time()))
        save_reviews(d)
        return {"started": d["starts"][f"{league}:{eid}"], "min_seconds": REVIEW_MIN_SECONDS, "now": int(time.time())}


def review_save(b):
    """Save a pre-bet review for one selection. Enforces time spent, checklist, reason, estimate."""
    key = f"{b['market_id']}:{int(b['idx'])}"
    skey = f"{b['league']}:{b['event_id']}"
    with rlock:
        d = load_reviews()
        started = d["starts"].get(skey)
        if not started:
            return 400, {"error": "Start the review for this game first."}
        spent = int(time.time()) - started
        if spent < REVIEW_MIN_SECONDS:
            return 400, {"error": f"Review time too short: {spent // 60} of {REVIEW_MIN_SECONDS // 60} minutes. Keep reading the dossier."}
        if len(b.get("checks", [])) < 7:
            return 400, {"error": "Tick all 7 checklist items."}
        if len((b.get("reason") or "").strip()) < MIN_REASON_CHARS:
            return 400, {"error": f"Write your reason (at least {MIN_REASON_CHARS} characters)."}
        est = float(b["estimate"]) / 100
        if not (0.02 <= est <= 0.98):
            return 400, {"error": "Estimate must be between 2% and 98%."}
        m = market_now(b["market_id"])
        price = float(jl(m.get("outcomePrices"))[int(b["idx"])]) if m else None
        if price is None or est - price < MIN_EDGE:
            return 400, {"error": f"No edge: your estimate {est:.0%} vs price {0 if price is None else price:.0%}. Need {MIN_EDGE*100:.0f}+ points higher. Skip this bet."}
        d["reviews"][key] = dict(estimate=est, reason=b["reason"].strip(), checks=b["checks"], price=price,
                                 minutes=spent // 60, saved_at=int(time.time()))
        save_reviews(d)
    return 200, {"ok": True, "edge": round(est - price, 3), "minutes": spent // 60}


# ---------------------------------------------------------------- trading
def market_now(mid):
    m = fetch(f"https://gamma-api.polymarket.com/markets/{mid}", 10)
    return m if isinstance(m, dict) else None


def leg_price(leg):
    m = market_now(leg["market_id"])
    if not m:
        return None, None
    prices = [float(x) for x in jl(m.get("outcomePrices"))]
    return prices[leg["idx"]], m


def resolve_leg(leg):
    """Return 'win' | 'loss' | None (still open) using Polymarket's own settlement."""
    m = market_now(leg["market_id"])
    if not m or not m.get("closed"):
        return None
    prices = [float(x) for x in jl(m.get("outcomePrices"))]
    if max(prices) < 0.99:
        return None
    return "win" if prices[leg["idx"]] >= 0.99 else "loss"


def current_value(p):
    if p["kind"] == "single":
        pr, _ = leg_price(p["legs"][0])
        return None if pr is None else max(0.0, p["shares"] * (pr - SPREAD_COST)), pr
    prod = 1.0
    for l in p["legs"]:
        pr, _ = leg_price(l)
        if pr is None:
            return None, None
        prod *= pr
    return max(0.0, p["shares"] * (prod - SPREAD_COST)), prod


def refresh(w):
    """Fill limit orders, settle finished positions."""
    for p in w["positions"]:
        if p["status"] == "pending":
            pr, _ = leg_price(p["legs"][0])
            if pr is not None and pr <= p["limit"]:
                p["status"] = "open"
                p["price"] = p["limit"]
                p["shares"] = round(p["stake"] / p["limit"], 4)
                p["fee"] = 0.0
        if p["status"] == "open":
            res = [resolve_leg(l) for l in p["legs"]]
            if "loss" in res:
                p["status"], p["payout"] = "lost", 0.0
            elif all(r == "win" for r in res):
                p["status"], p["payout"] = "won", round(p["shares"], 2)
                w["cash"] += p["payout"]
            if p["status"] in ("won", "lost"):
                p["closed_at"] = int(time.time())
    return w


def portfolio():
    with wlock:
        w = refresh(load_wallet())
        save_wallet(w)
    positions, open_val, staked = [], 0.0, 0.0
    realized = 0.0
    for p in w["positions"]:
        q = dict(p)
        if p["status"] == "open":
            cv, pr = current_value(p)
            q["value"], q["now_price"] = (round(cv, 2) if cv is not None else None), pr
            open_val += cv or 0
        elif p["status"] == "pending":
            staked += p["stake"]
        if p["status"] in ("won", "lost", "sold"):
            realized += (p.get("payout", 0) or 0) - p["stake"] - p.get("fee", 0)
        positions.append(q)
    pending = sum(p["stake"] for p in w["positions"] if p["status"] == "pending")
    return dict(cash=round(w["cash"], 2), open_value=round(open_val, 2), pending=round(pending, 2),
                total=round(w["cash"] + open_val + pending, 2), start=START_CASH,
                realized=round(realized, 2), positions=positions[::-1])


def place(body):
    return _place(body, True)


def _place(body, require_review):
    legs_in = body["legs"]
    stake = float(body["stake"])
    order = body.get("order", "market")
    if not (0 < stake <= MAX_STAKE):
        return 400, {"error": f"Rule: stake must be above $0 and at most ${MAX_STAKE:.0f}"}
    legs = []
    for l in legs_in:
        m = market_now(l["market_id"])
        if not m or m.get("closed"):
            return 400, {"error": "That market is closed"}
        pr = float(jl(m.get("outcomePrices"))[int(l["idx"])])
        if pr <= 0.01 or pr >= 0.99:
            return 400, {"error": "Price too extreme (under 1c or over 99c)"}
        legs.append(dict(market_id=str(l["market_id"]), idx=int(l["idx"]), label=l["label"], game=l["game"],
                         league=l.get("league", ""), price=pr, question=m.get("question", "")))
    if len({l["market_id"] for l in legs}) != len(legs):
        return 400, {"error": "Combo can't use the same market twice"}
    revs = load_reviews()["reviews"] if require_review else {}
    for l in (legs if require_review else []):
        rv = revs.get(f"{l['market_id']}:{l['idx']}")
        if not rv:
            return 400, {"error": f"No saved pre-bet review for: {l['label']}. Do the full review first."}
        if time.time() - rv["saved_at"] > 6 * 3600:
            return 400, {"error": f"Review for {l['label']} is over 6 hours old. Redo it."}
        if rv["estimate"] - l["price"] < MIN_EDGE:
            return 400, {"error": f"Price moved: {l['label']} is now {l['price']:.0%}, your estimate was {rv['estimate']:.0%}. Edge is gone."}
    if require_review:
        body["note"] = " | ".join(f"{l['label']}: {revs[l['market_id'] + ':' + str(l['idx'])]['reason']}" for l in legs)
    kind = "single" if len(legs) == 1 else "parlay"
    prod = 1.0
    for l in legs:
        prod *= l["price"]
    with wlock:
        w = load_wallet()
        status, price, fee = "open", prod, 0.0
        if order == "limit" and kind == "single":
            lp = float(body["limit"])
            if lp <= 0.01 or lp >= 0.99:
                return 400, {"error": "Limit must be between 1c and 99c"}
            if lp >= prod:     # would fill immediately, as a taker
                price = prod
            else:
                status, price = "pending", lp
        if status == "open":
            shares = stake / price
            fee = round(FEE_RATE * shares * price * (1 - price), 4)
        else:
            shares, fee = stake / price, 0.0
        if w["cash"] < stake + fee:
            return 400, {"error": "Not enough paper cash"}
        w["cash"] = round(w["cash"] - stake - fee, 4)
        pid = w["next_id"]
        w["next_id"] += 1
        pos = dict(id=pid, kind=kind, legs=legs, stake=stake, shares=round(shares, 4), price=round(price, 4),
                   fee=fee, status=status, placed_at=int(time.time()), note=body.get("note", ""))
        if status == "pending":
            pos["limit"] = price
        w["positions"].append(pos)
        save_wallet(w)
    return 200, {"ok": True, "id": pid, "status": status, "shares": round(shares, 2), "price": round(price, 3), "fee": fee}


def sell(pid):
    with wlock:
        w = refresh(load_wallet())
        p = next((x for x in w["positions"] if x["id"] == pid), None)
        if not p or p["status"] not in ("open", "pending"):
            return 400, {"error": "Position not sellable"}
        if p["status"] == "pending":
            p["status"], p["payout"] = "sold", p["stake"]      # cancel: refund stake
            w["cash"] += p["stake"]
            save_wallet(w)
            return 200, {"ok": True, "cancelled": True}
        cv, _ = current_value(p)
        if cv is None:
            return 400, {"error": "No price available"}
        p["status"], p["payout"], p["closed_at"] = "sold", round(cv, 2), int(time.time())
        w["cash"] += cv
        save_wallet(w)
    return 200, {"ok": True, "received": round(cv, 2)}


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/":
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/leagues":
                return self.send(200, [dict(id=k, name=v["name"]) for k, v in LEAGUES.items()])
            if u.path == "/api/games":
                return self.send(200, games(q.get("league", "nba")))
            if u.path == "/api/game":
                e = find_event(q["league"], q["id"])
                return self.send(200, event_view(e, True) if e else {"error": "not found"})
            if u.path == "/api/research":
                return self.send(200, research(q["league"], q["id"]))
            if u.path == "/api/dossier":
                return self.send(200, dossier(q["league"], q["id"]))
            if u.path == "/api/review/start":
                return self.send(200, review_start(q["league"], q["id"]))
            if u.path == "/api/player":
                return self.send(200, player(q["league"], q["id"]))
            if u.path == "/api/portfolio":
                return self.send(200, portfolio())
            if u.path == "/api/alerts":
                out = []
                from concurrent.futures import ThreadPoolExecutor
                lgs = ("nba", "wnba", "ncaam", "epl", "ucl")
                with ThreadPoolExecutor(5) as ex:
                    res = list(ex.map(games, lgs))
                for lg, gl in zip(lgs, res):
                    for g in gl:
                        for v in g.get("value", []):
                            out.append(dict(league=lg, game_id=g["id"], game=g["title"], start=g["start"], **v))
                return self.send(200, out)
            return self.send(404, {"error": "not found"})
        except Exception as ex:
            return self.send(500, {"error": str(ex)})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/bet":
                return self.send(*place(body))
            if self.path == "/api/review":
                return self.send(*review_save(body))
            if self.path == "/api/sell":
                return self.send(*sell(int(body["id"])))
            if self.path == "/api/reset":
                with wlock:
                    save_wallet({"cash": START_CASH, "next_id": 1, "positions": []})
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except Exception as ex:
            return self.send(500, {"error": str(ex)})


if __name__ == "__main__":
    print(f"Paper Market running: http://localhost:{PORT}  (fake money only)")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
