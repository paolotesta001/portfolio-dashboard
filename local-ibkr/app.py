"""
Portfolio Dashboard - IBKR Flex Web Service edition.

Reads your IBKR positions (READ-ONLY) via the official Flex Web Service,
compares them to your target allocation, and tells you exactly how much of
each ETF to buy with this month's contribution. It fills the most-underweight
sleeves first, so your contributions do the rebalancing and you rarely need
to sell (which keeps you clear of the 26% Italian capital-gains tax).

Run:
    python app.py            -> serves the dashboard at http://127.0.0.1:5000
    python app.py --refresh  -> pulls fresh data into cache and exits
                                (use this from Windows Task Scheduler for a
                                 daily morning pre-fetch; optional)

The Flex token is READ-ONLY: it can pull reports but cannot trade or move
money. This is an official IBKR feature - using it does not violate the ToS.
"""

import os
import sys
import json
import time
import math
import datetime as dt
import xml.etree.ElementTree as ET

import requests
from flask import Flask, jsonify, request, render_template

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CACHE_PATH = os.path.join(HERE, "cache.json")

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_VERSION = 3

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError(
            "Missing config.json. Copy config.example.json to config.json and fill in "
            "your IBKR Flex token + query id and your target allocation (see README.md)."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Environment variables override the file (handy for keeping secrets out of config)
    cfg["ibkr"]["token"] = os.environ.get("IBKR_FLEX_TOKEN", cfg["ibkr"].get("token", ""))
    cfg["ibkr"]["query_id"] = os.environ.get("IBKR_FLEX_QUERY_ID", cfg["ibkr"].get("query_id", ""))
    return cfg


# ---------------------------------------------------------------------------
# IBKR Flex Web Service (two-step: request -> poll for statement)
# ---------------------------------------------------------------------------
def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def flex_send_request(token, query_id):
    r = requests.get(
        f"{FLEX_BASE}/SendRequest",
        params={"t": token, "q": query_id, "v": FLEX_VERSION},
        timeout=30,
    )
    r.raise_for_status()
    root = ET.fromstring(r.text.strip())
    if (root.findtext("Status") or "").strip() != "Success":
        raise RuntimeError(
            f"IBKR SendRequest failed: {root.findtext('ErrorCode')} "
            f"{root.findtext('ErrorMessage')}"
        )
    return root.findtext("ReferenceCode"), root.findtext("Url")


def flex_get_statement(token, url, reference_code, retries=10, delay=6):
    """Poll until the report is generated. IBKR returns error 1019 while it works."""
    for _ in range(retries):
        r = requests.get(
            url,
            params={"t": token, "q": reference_code, "v": FLEX_VERSION},
            timeout=60,
        )
        r.raise_for_status()
        text = r.text.strip()
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return text
        if root.tag == "FlexQueryResponse":
            return text  # this is the actual data
        if root.tag == "FlexStatementResponse":
            code = (root.findtext("ErrorCode") or "").strip()
            if code == "1019":  # statement still generating -> wait and retry
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"IBKR statement error: {code} {root.findtext('ErrorMessage')}"
            )
        return text
    raise RuntimeError(
        "IBKR statement not ready after several retries. Wait a minute and refresh."
    )


def parse_positions(xml_text):
    """Turn the Flex XML into {isin: {...}} using base-currency (EUR) values."""
    root = ET.fromstring(xml_text)
    positions = {}
    for op in root.iter("OpenPosition"):
        # A Flex report can include per-lot AND summary rows; keep summary to avoid
        # double counting. If the attribute is absent, we keep the row.
        lod = (op.get("levelOfDetail") or "").upper()
        if lod and lod != "SUMMARY":
            continue
        isin = (op.get("isin") or "").strip()
        if not isin:
            continue
        qty = _f(op.get("position"))
        mark = _f(op.get("markPrice"))
        fx = _f(op.get("fxRateToBase"), 1.0)
        pv = op.get("positionValue")
        value_ccy = _f(pv) if pv not in (None, "") else qty * mark
        value_base = value_ccy * fx        # convert to EUR (base currency)
        price_base = mark * fx             # approx EUR price per share
        rec = positions.get(isin)
        if rec:
            rec["qty"] += qty
            rec["value_base"] += value_base
        else:
            positions[isin] = {
                "isin": isin,
                "symbol": op.get("symbol") or "",
                "qty": qty,
                "price_base": price_base,
                "value_base": value_base,
                "currency": op.get("currency") or "",
            }
    return positions


# ---------------------------------------------------------------------------
# Rebalancing logic: "fill the most-underweight sleeves first"
# ---------------------------------------------------------------------------
def build_view(cfg, positions, contribution):
    targets = cfg["targets"]
    target_isins = {t["isin"] for t in targets}

    total = sum(positions.get(t["isin"], {}).get("value_base", 0.0) for t in targets)

    sleeves = []
    for t in targets:
        p = positions.get(t["isin"], {})
        value = p.get("value_base", 0.0)
        sleeves.append({
            "sleeve": t["sleeve"],
            "name": t["name"],
            "isin": t["isin"],
            "target_pct": t["target_pct"],
            "current_pct": (value / total) if total else 0.0,
            "value_base": value,
            "price_base": p.get("price_base") or None,
            "qty": p.get("qty", 0.0),
        })

    # How far below target (in EUR) is each sleeve after adding the contribution?
    new_total = total + contribution
    deficits = [max(0.0, new_total * s["target_pct"] - s["value_base"]) for s in sleeves]
    sum_def = sum(deficits)

    alloc = [0.0] * len(sleeves)
    if contribution > 0:
        if sum_def <= 0:
            # Everything already at/above target -> just split by target weight
            for i, s in enumerate(sleeves):
                alloc[i] = contribution * s["target_pct"]
        elif contribution <= sum_def:
            # Not enough to fully rebalance -> pour into the most-underweight, pro-rata
            for i in range(len(sleeves)):
                alloc[i] = contribution * (deficits[i] / sum_def)
        else:
            # Enough to top everyone up; spread the remainder by target weight
            remainder = contribution - sum_def
            for i, s in enumerate(sleeves):
                alloc[i] = deficits[i] + remainder * s["target_pct"]

    band = cfg.get("rebalance_band_pct", 0.05)
    total_spent = 0.0
    for i, s in enumerate(sleeves):
        s["drift"] = s["current_pct"] - s["target_pct"]
        s["out_of_band"] = abs(s["drift"]) > band
        s["buy_eur"] = round(alloc[i], 2)
        price = s["price_base"]
        if price and price > 0 and alloc[i] > 0:
            shares = math.floor(alloc[i] / price)
            s["buy_shares"] = shares
            s["actual_spend"] = round(shares * price, 2)
        else:
            s["buy_shares"] = None
            s["actual_spend"] = 0.0
        total_spent += s["actual_spend"]

    sleeves_sorted = sorted(sleeves, key=lambda s: s["drift"])  # most underweight first

    unmapped = [
        {"isin": isin, "symbol": p["symbol"], "value_base": round(p["value_base"], 2)}
        for isin, p in positions.items()
        if isin not in target_isins and p["value_base"] > 0
    ]

    return {
        "base_currency": cfg.get("base_currency", "EUR"),
        "total_value": round(total, 2),
        "monthly_contribution": contribution,
        "sleeves": sleeves_sorted,
        "buy_summary": [
            {
                "sleeve": s["sleeve"], "name": s["name"], "isin": s["isin"],
                "buy_eur": s["buy_eur"], "buy_shares": s["buy_shares"],
                "actual_spend": s["actual_spend"], "price_base": s["price_base"],
            }
            for s in sleeves_sorted if s["buy_eur"] > 0
        ],
        "leftover_cash": round(contribution - total_spent, 2) if contribution > 0 else 0.0,
        "unmapped": unmapped,
    }


# ---------------------------------------------------------------------------
# Fetch + daily cache
# ---------------------------------------------------------------------------
def fetch_positions(cfg):
    token = cfg["ibkr"]["token"]
    query_id = cfg["ibkr"]["query_id"]
    if not token or not query_id or "PUT_YOUR" in token or "PUT_YOUR" in query_id:
        raise RuntimeError(
            "IBKR token / query_id not set. Edit config.json (see README.md)."
        )
    ref, url = flex_send_request(token, query_id)
    return parse_positions(flex_get_statement(token, url, ref))


def save_cache(positions):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "as_of": dt.date.today().isoformat(),
                "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                "positions": positions,
            },
            f, indent=2,
        )


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_positions(cfg, force_refresh=False):
    """Return (positions, cache_meta, was_cached). Cache is valid for the current day."""
    cache = load_cache()
    today = dt.date.today().isoformat()
    if not force_refresh and cache and cache.get("as_of") == today:
        return cache["positions"], cache, True
    positions = fetch_positions(cfg)
    save_cache(positions)
    return positions, load_cache(), False


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/portfolio")
def api_portfolio():
    force = request.args.get("refresh") == "1"
    try:
        cfg = load_config()
        try:
            contribution = float(request.args.get("contribution", cfg.get("monthly_contribution", 0)))
        except ValueError:
            contribution = cfg.get("monthly_contribution", 0)
        positions, cache, cached = get_positions(cfg, force_refresh=force)
    except Exception as e:  # surface a friendly message to the UI
        return jsonify({"error": str(e)}), 502
    view = build_view(cfg, positions, contribution)
    view["as_of"] = cache.get("as_of")
    view["fetched_at"] = cache.get("fetched_at")
    view["cached"] = cached
    view["default_contribution"] = cfg.get("monthly_contribution", 0)
    view["rebalance_band_pct"] = cfg.get("rebalance_band_pct", 0.05)
    return jsonify(view)


def main():
    if "--refresh" in sys.argv:
        cfg = load_config()
        positions = fetch_positions(cfg)
        save_cache(positions)
        print(f"Refreshed {len(positions)} positions into {CACHE_PATH}")
        return
    print("Portfolio Dashboard running at http://127.0.0.1:5000  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
