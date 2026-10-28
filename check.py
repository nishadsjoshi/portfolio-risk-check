#!/usr/bin/env python3
"""Daily portfolio risk check on Nishad's linked Robinhood account.

Compares current holdings vs the previous snapshot and prints:
  ALERT: lines for things worth surfacing to Nishad
  OK:    when nothing crosses a threshold

Flags (all factual observations, never buy/sell advice):
  - NEW options position: days to expiry, premium as % of portfolio
  - Options expiring within 7 days
  - Options down >=50% from cost basis (theta/decay reality check)
  - Single position moving >=10% since last check (positions >= $200)
  - Portfolio total moving >=5% since last check

State lives in portfolio_state.json next to this script.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "portfolio_state.json")

MOVE_PCT = 10.0          # single-position move worth flagging
MOVE_MIN_VALUE = 200.0   # ignore dust
PORTFOLIO_MOVE_PCT = 5.0
EXPIRY_WARN_DAYS = 7
OPTION_DECAY_PCT = 50.0  # option value vs cost basis
NEW_OPTION_PORTFOLIO_PCT = 3.0  # premium this size of portfolio gets flagged
ALERT_COOLDOWN_HOURS = 24

OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def parse_occ(symbol):
    m = OCC.match(symbol or "")
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    exp = date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    return {
        "root": root,
        "expiry": exp.isoformat(),
        "kind": "call" if cp == "C" else "put",
        "strike": int(strike) / 1000.0,
        "days_to_expiry": (exp - date.today()).days,
    }


def fetch_holdings():
    """Read holdings via the plaid CLI.

    The CLI reads every linked institution and exits non-zero when any one
    of them fails a product (e.g. the linked Bank of America banking item
    fails the investments-holdings product it never supported). The response
    envelope still carries the complete Robinhood body in that case, so use
    it whenever holdings are present instead of discarding good data.
    Only raise when there is genuinely no holdings data to work with.
    """
    out = subprocess.run(["plaid", "investments-holdings"],
                         capture_output=True, text=True, timeout=60)
    try:
        envelope = json.loads(out.stdout)
    except (json.JSONDecodeError, TypeError):
        envelope = None
    if isinstance(envelope, dict):
        body = envelope.get("body")
        if isinstance(body, dict) and body.get("holdings") is not None:
            return body
    raise RuntimeError(f"plaid investments-holdings failed: {out.stderr[:300]}")


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"positions": {}, "alerted": {}, "total": None}


def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_PATH)


def main():
    now = time.time()
    try:
        body = fetch_holdings()
    except Exception as e:
        print(f"ERROR: holdings fetch failed: {e}")
        return 2

    secs = {s["security_id"]: s for s in body.get("securities", [])}
    state = load_state()
    prev = state.get("positions", {})
    alerted = state.get("alerted", {})
    alerts = []
    notes = []

    # Aggregate by ticker across accounts
    agg = {}
    for h in body.get("holdings", []):
        s = secs.get(h["security_id"], {})
        ticker = s.get("ticker_symbol") or s.get("name") or h["security_id"][:8]
        stype = s.get("type", "?")
        v = h.get("institution_value") or 0
        q = h.get("quantity") or 0
        px = h.get("institution_price")
        cb = h.get("cost_basis")
        a = agg.setdefault(ticker, {"type": stype, "v": 0.0, "q": 0.0,
                                    "px": px, "cb_sum": 0.0, "cb_n": 0})
        a["v"] += v
        a["q"] += q
        if cb:
            a["cb_sum"] += cb
            a["cb_n"] += 1

    total = sum(a["v"] for a in agg.values())
    prev_total = state.get("total")

    def cooled(key):
        return now - alerted.get(key, 0) < ALERT_COOLDOWN_HOURS * 3600

    def mark(key):
        alerted[key] = now

    for ticker, a in sorted(agg.items(), key=lambda kv: -kv[1]["v"]):
        p = prev.get(ticker)
        is_new = p is None
        occ = parse_occ(ticker) if a["type"] == "derivative" else None

        # 1) New options position -> risk facts
        if is_new and occ and a["v"] >= 50:
            dte = occ["days_to_expiry"]
            pct_pf = (a["v"] / total * 100) if total else 0
            facts = [f"{occ['root']} {occ['kind']} ${occ['strike']:g} exp {occ['expiry']}"]
            if dte <= 7:
                facts.append(f"expires in {dte} day(s)")
            if pct_pf >= NEW_OPTION_PORTFOLIO_PCT:
                facts.append(f"premium is {pct_pf:.1f}% of portfolio")
            key = f"new_opt:{ticker}"
            if not cooled(key):
                alerts.append("NEW OPTIONS POSITION: " + "; ".join(facts)
                              + f" (value ${a['v']:,.0f})")
                mark(key)
            continue

        if p is None:
            continue

        # 2) Existing options: expiry warning + decay check
        if occ:
            dte = occ["days_to_expiry"]
            if 0 <= dte <= EXPIRY_WARN_DAYS and a["v"] >= 50:
                key = f"expiry:{ticker}"
                if not cooled(key):
                    alerts.append(
                        f"EXPIRING SOON: {occ['root']} {occ['kind']} "
                        f"${occ['strike']:g} expires {occ['expiry']} "
                        f"({dte}d), current value ${a['v']:,.0f}")
                    mark(key)
            if a["cb_n"] and a["cb_sum"] > 0:
                decay = (a["v"] - a["cb_sum"]) / a["cb_sum"] * 100
                if decay <= -OPTION_DECAY_PCT:
                    key = f"decay:{ticker}"
                    if not cooled(key):
                        alerts.append(
                            f"OPTION DOWN {decay:.0f}%: {ticker} worth "
                            f"${a['v']:,.0f} vs ${a['cb_sum']:,.0f} paid")
                        mark(key)
            # option up a lot = opportunity fact
            if p.get("v") and p["v"] > 0:
                up = (a["v"] - p["v"]) / p["v"] * 100
                if up >= 50 and a["v"] >= 200:
                    key = f"opt_up:{ticker}"
                    if not cooled(key):
                        alerts.append(
                            f"OPTION UP {up:.0f}%: {ticker} now ${a['v']:,.0f}")
                        mark(key)

        # 3) Big single-position moves
        if a["px"] and p.get("px") and a["v"] >= MOVE_MIN_VALUE:
            move = (a["px"] - p["px"]) / p["px"] * 100
            if abs(move) >= MOVE_PCT:
                key = f"move:{ticker}:{'up' if move > 0 else 'dn'}"
                if not cooled(key):
                    alerts.append(
                        f"{ticker} moved {move:+.1f}% since last check — "
                        f"now ${a['px']:,.2f} (position ${a['v']:,.0f})")
                    mark(key)

    # 4) Portfolio-level move
    if prev_total:
        pmove = (total - prev_total) / prev_total * 100
        if abs(pmove) >= PORTFOLIO_MOVE_PCT:
            key = f"pf:{'up' if pmove > 0 else 'dn'}"
            if not cooled(key):
                alerts.append(f"PORTFOLIO {pmove:+.1f}% since last check — "
                              f"now ${total:,.0f}")
                mark(key)

    # Save new snapshot
    state["positions"] = {t: {"v": a["v"], "px": a["px"], "q": a["q"]}
                          for t, a in agg.items()}
    state["alerted"] = alerted
    state["total"] = total
    state["checked_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)

    for a_ in alerts:
        print("ALERT:", a_)
    if not alerts:
        print(f"OK: portfolio ${total:,.0f}, no thresholds crossed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
