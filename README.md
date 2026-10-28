# portfolio-risk-check

A daily Python script that reads linked brokerage holdings and flags what actually
matters — facts only, never buy/sell advice.

## What it flags

- **New options positions** — days to expiry, premium as a % of the portfolio
- **Options expiring within 7 days**
- **Options down 50%+ from cost basis** (theta/decay reality check)
- **Single positions moving 10%+** since the last check (positions ≥ $200)
- **Portfolio-level moves of 5%+** since the last check

Alerts are rate-limited to one per position per 24 hours so a noisy day doesn't
spam you.

## How it works

`check.py` reads holdings through the Plaid CLI (`plaid investments-holdings`),
compares them against the previous snapshot in `portfolio_state.json`, and prints
`ALERT:` lines for anything crossing a threshold — or a single `OK:` line when
nothing does.

One quirk worth knowing: the Plaid CLI exits non-zero when any linked
institution fails a product it never supported (e.g. a banking item failing the
investments-holdings product). The response envelope still carries the complete
brokerage body in that case, so the script uses it whenever holdings are
present instead of discarding good data.

## Usage

```bash
python3 check.py
```

State lives in `portfolio_state.json` next to the script. That file contains
personal position data — it is git-ignored by design and should never be
committed.

## Built with

Python 3 (stdlib only) + Plaid. Built with Mica, my personal AI agent.
