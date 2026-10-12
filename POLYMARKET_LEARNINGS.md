# What I learned from Polymarket US (read-only, 2026-10-12)

## Layout
- Top nav: Sports / Politics / Crypto / Weather / Tech / Culture / Econ. Left sidebar by sport and league (NBA, EuroLeague, ACB, EPL, ...).
- Sports home: "Live" games with price + live score; per league: Games and Futures tabs, grouped Today / Tomorrow / dates.
- Game card: both teams, payout multiple (e.g. 2.78x) and a % button (price in cents).

## Game page
- Price chart (1D / 1W / 1M / All), countdown to start, moneyline row.
- Extra markets per game (7-12): Spread (pick a line: ATL wins by over 5.5), Totals (Over/Under line), first-to-score, odd/even.
- Collapsible: Market rules, Timeline & Payout, FAQ.
- Live chat beside the ticket, showing users' parlay "legs" and amounts.

## How trading works
- 1 share pays $1 if right, $0 if wrong. Price 36c = 36% chance = 2.78x payout.
- Buy and Sell tabs. You can sell any time before the game ends (cash out).
- Order types: Market (fills now at the best price) and Limit (your price, "Good til canceled").
- Quick amounts +$5 / +$10 / +$25. "To win" box shows the payout.
- Build a Combo (parlay): add legs, shows combined payout (1 leg at 66% = 1.48x).
- Settlement: winner incl. overtime; postponed over 2 days = settles at last fair price. Market closes end of day, payout within ~25 min.

## Costs (third-party sources, verify in app)
- Maker (resting limit) orders ~free; takers pay about 5% x price x (1 - price) per share, max about $1.25 per 100 shares at 50c. Biggest at 50/50 games.
- Prices on both sides add to more than 100% (e.g. 36 + 66 = 102): that gap is the spread you cross.

## What worked / didn't (for our mock)
- Works: price chart, multi-market game pages, sell-anytime, limit orders, combo builder.
- Gotcha: sidebar/ticket click targets shift on scroll; I accidentally selected a side and added a combo leg (nothing was submitted).
- Public API (gamma-api.polymarket.com) shows prices without login; real US game prices here match DraftKings within 1-2%.

## Mock site plan
Paper wallet $100 -> sports list -> game page (moneyline, spread, totals) -> ticket (market/limit, sell) -> combo -> portfolio with P&L -> auto-settle from ESPN scores.
