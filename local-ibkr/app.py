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
import csv
import io
import re
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

DEFAULT_COMMISSION = {
    "rate": 0.0005,
    "minimum_eur": 3.00,
    "maximum_eur": 29.00,
    "warning_pct": 0.003,
}
DEFAULT_INSTRUMENTS = {
    "IE00BD4TXV59": {"ticker": "UETW", "exchange": "IBIS"},
    "IE00BL25JP72": {"ticker": "XDEM", "exchange": "IBIS2"},
    "IE00BL25JL35": {"ticker": "XDEQ", "exchange": "IBIS2"},
    "IE00BKM4GZ66": {"ticker": "IS3N", "exchange": "IBIS2"},
    "IE00BDBRDM35": {"ticker": "VAGF", "exchange": "IBIS2"},
    "IE00B0M62X26": {"ticker": "IBCI", "exchange": "AEB"},
    "IE00B579F325": {"ticker": "8PSG", "exchange": "IBIS2"},
}


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


def order_ticket_config(cfg):
    ticket = cfg.get("order_ticket", {})
    commission = dict(DEFAULT_COMMISSION)
    commission.update(ticket.get("commission", {}))
    instruments = {isin: dict(values) for isin, values in DEFAULT_INSTRUMENTS.items()}
    for isin, values in ticket.get("instruments", {}).items():
        instruments[isin] = {**instruments.get(isin, {}), **values}
    return {
        "max_positions": max(1, int(ticket.get("max_positions", 2))),
        "commission": commission,
        "instruments": instruments,
    }


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
        currency = (op.get("currency") or "").strip().upper()
        fx_raw = op.get("fxRateToBase")
        fx = _f(fx_raw, 0.0)
        if currency == "EUR" and fx <= 0:
            fx = 1.0
        if currency != "EUR" and fx <= 0:
            raise RuntimeError(
                f"Position {isin} is in {currency or 'an unknown currency'} but the "
                "report has no FX rate to EUR. Holdings were not updated."
            )
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
                "currency": currency,
                "fx_rate_to_eur": fx,
            }
    return positions


# ---------------------------------------------------------------------------
# Rebalancing logic: "fill the most-underweight sleeves first"
# ---------------------------------------------------------------------------
def build_view(cfg, positions, contribution, position_count=None, skipped=None):
    targets = cfg["targets"]
    target_isins = {t["isin"] for t in targets}
    ticket_cfg = order_ticket_config(cfg)
    skipped = set(skipped or ())
    if position_count is None:
        position_count = ticket_cfg["max_positions"]
    position_count = max(1, min(int(position_count), len(targets)))

    total = sum(positions.get(t["isin"], {}).get("value_base", 0.0) for t in targets)

    sleeves = []
    for t in targets:
        p = positions.get(t["isin"], {})
        value = p.get("value_base", 0.0)
        execution = ticket_cfg["instruments"].get(t["isin"], {})
        sleeves.append({
            "sleeve": t["sleeve"],
            "name": t["name"],
            "isin": t["isin"],
            "ticker": execution.get("ticker") or t.get("ticker") or t["isin"],
            "exchange": execution.get("exchange") or t.get("exchange") or "",
            "target_pct": t["target_pct"],
            "current_pct": (value / total) if total else 0.0,
            "value_base": value,
            "price_base": p.get("price_base") or None,
            "qty": p.get("qty", 0.0),
        })

    # Rank by percentage-point drift, then allocate only across the requested
    # number of most-underweight, non-skipped sleeves. This remains buy-only.
    new_total = total + contribution
    band = cfg.get("rebalance_band_pct", 0.05)
    for s in sleeves:
        s["drift"] = s["current_pct"] - s["target_pct"]
        s["out_of_band"] = abs(s["drift"]) > band
        s["deficit_eur"] = max(0.0, new_total * s["target_pct"] - s["value_base"])
        s["buy_eur"] = 0.0
        s["buy_shares"] = None  # sized client-side from the live ask
        s["selected"] = False
    sleeves_sorted = sorted(sleeves, key=lambda s: s["drift"])  # most underweight first
    eligible = [s for s in sleeves_sorted if s["isin"] not in skipped and s["target_pct"] > 0]
    selected = eligible[:position_count]
    for rank, sleeve in enumerate(selected, 1):
        sleeve["selected"] = True
        sleeve["priority"] = rank

    sum_def = sum(s["deficit_eur"] for s in selected)
    if contribution > 0 and selected:
        if sum_def > 0 and contribution <= sum_def:
            for s in selected:
                s["buy_eur"] = contribution * s["deficit_eur"] / sum_def
        else:
            remainder = max(0.0, contribution - sum_def)
            weight_sum = sum(s["target_pct"] for s in selected) or 1.0
            for s in selected:
                s["buy_eur"] = (s["deficit_eur"] if sum_def > 0 else 0.0) + (
                    remainder * s["target_pct"] / weight_sum
                )
    for s in selected:
        s["buy_eur"] = round(s["buy_eur"], 2)
    if selected:
        rounding_gap = round(contribution - sum(s["buy_eur"] for s in selected), 2)
        selected[-1]["buy_eur"] = round(selected[-1]["buy_eur"] + rounding_gap, 2)

    unmapped = [
        {
            "isin": isin,
            "symbol": p.get("symbol", ""),
            "value_base": round(p["value_base"], 2),
            "currency": p.get("currency", ""),
        }
        for isin, p in positions.items()
        if isin not in target_isins and p["value_base"] > 0
    ]

    buy_summary = [
        {
            "sleeve": s["sleeve"], "name": s["name"], "isin": s["isin"],
            "ticker": s["ticker"], "exchange": s["exchange"],
            "priority": s.get("priority"), "buy_eur": s["buy_eur"],
            "price_base": s["price_base"],
        }
        for s in selected if s["buy_eur"] > 0
    ]

    return {
        "base_currency": cfg.get("base_currency", "EUR"),
        "total_value": round(total, 2),
        "monthly_contribution": contribution,
        "position_count": position_count,
        "available_position_count": len(targets),
        "sleeves": sleeves_sorted,
        "buy_summary": buy_summary,
        # Rollover is an execution result, not unallocated contribution. The
        # browser fills this only after every selected live ask is present.
        "leftover_cash": 0.0 if contribution <= 0 else None,
        "pricing_error": (
            "Enter a live ask price for every selected order. Missing prices are not rollover cash."
            if buy_summary else None
        ),
        "allocation_error": (
            "No eligible underweight sleeves remain. Undo a skipped sleeve."
            if contribution > 0 and not buy_summary else None
        ),
        "commission": ticket_cfg["commission"],
        "skipped": sorted(skipped),
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


def save_cache(positions, as_of=None, source="flex"):
    temp_path = CACHE_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "as_of": as_of or dt.date.today().isoformat(),
                "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                "source": source,
                "positions": positions,
            },
            f, indent=2,
        )
    os.replace(temp_path, CACHE_PATH)


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_positions(cfg, force_refresh=False):
    """Return (positions, cache_meta, was_cached). Cache is valid for the current day."""
    cache = load_cache()
    today = dt.date.today().isoformat()
    # An explicitly uploaded report remains authoritative until the user asks
    # for a fresh Flex pull; its own as-of date must not be rewritten as today.
    if not force_refresh and cache and (
        cache.get("as_of") == today or cache.get("source") == "upload"
    ):
        return cache["positions"], cache, True
    positions = fetch_positions(cfg)
    save_cache(positions)
    return positions, load_cache(), False


# ---------------------------------------------------------------------------
# User-supplied IBKR XML / CSV reports
# ---------------------------------------------------------------------------
def _header_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _report_number(value):
    text = str(value or "").strip().replace("€", "").replace("$", "").replace("£", "")
    text = re.sub(r"\s+", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        tail = text.rsplit(",", 1)[-1]
        text = text.replace(",", ".") if len(tail) == 2 else text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _date_candidates(value):
    text = str(value or "")
    found = []
    for year, month, day in re.findall(r"\b(20\d{2})[-/.]?(\d{2})[-/.]?(\d{2})\b", text):
        try:
            found.append(dt.date(int(year), int(month), int(day)).isoformat())
        except ValueError:
            pass
    for first, second, year in re.findall(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b", text):
        first, second = int(first), int(second)
        month, day = (second, first) if first > 12 else (first, second)
        try:
            found.append(dt.date(int(year), month, day).isoformat())
        except ValueError:
            pass
    month_pattern = re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),?\s+(20\d{2})\b",
        re.IGNORECASE,
    )
    for month, day, year in month_pattern.findall(text):
        try:
            parsed = dt.datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
            found.append(parsed.isoformat())
        except ValueError:
            pass
    return found


def _xml_attr(element, *names):
    wanted = {_header_key(name) for name in names}
    for key, value in element.attrib.items():
        if _header_key(key) in wanted:
            return value
    return ""


def _make_uploaded_position(fields):
    isin = str(fields.get("isin") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
        return None, "Missing or invalid ISIN"
    currency = str(fields.get("currency") or "").strip().upper()
    if not currency:
        return None, f"{isin}: currency is missing"
    qty = _report_number(fields.get("qty"))
    mark = _report_number(fields.get("mark"))
    reported_value = _report_number(fields.get("value"))
    fx = _report_number(fields.get("fx"))
    if currency == "EUR" and not fx:
        fx = 1.0
    if currency != "EUR" and (fx is None or fx <= 0):
        return None, f"{isin}: {currency} position has no FX rate to EUR"
    if qty is None:
        return None, f"{isin}: quantity is missing"
    if mark is None and reported_value is None:
        return None, f"{isin}: mark price and position value are missing"
    value_ccy = reported_value if reported_value is not None else qty * mark
    value_eur = value_ccy * fx
    price_eur = mark * fx if mark is not None else (value_eur / qty if qty else 0.0)
    return {
        "isin": isin,
        "symbol": str(fields.get("symbol") or ""),
        "qty": qty,
        "price_base": price_eur,
        "value_base": value_eur,
        "currency": currency,
        "fx_rate_to_eur": fx,
    }, None


def _finish_uploaded_report(valid_rows, rows_parsed, ignored, warnings, date_values):
    dates = sorted({d for value in date_values for d in _date_candidates(value)})
    if not dates:
        raise ValueError(
            "No report date was found. Include an as-of/report date so the cache is not stamped with today's date."
        )
    if not valid_rows:
        raise ValueError(warnings[0] if warnings else "No usable positions were found in the report.")
    positions = {}
    for row in valid_rows:
        current = positions.get(row["isin"])
        if current:
            current["qty"] += row["qty"]
            current["value_base"] += row["value_base"]
            if current["qty"]:
                current["price_base"] = current["value_base"] / current["qty"]
        else:
            positions[row["isin"]] = dict(row)
    return {
        "positions": positions,
        "row_isins": [row["isin"] for row in valid_rows],
        "rows_parsed": rows_parsed,
        "rows_ignored": ignored,
        "warnings": warnings,
        "report_date": dates[-1],
        "total_eur": round(sum(p["value_base"] for p in positions.values()), 2),
    }


def parse_uploaded_xml(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML: {exc}") from exc
    open_positions = list(root.iter("OpenPosition"))
    if not open_positions:
        raise ValueError(
            "The XML contains no OpenPosition rows. Export the Open Positions section from IBKR."
        )
    summary_isins = {
        _xml_attr(row, "isin").upper()
        for row in open_positions
        if _xml_attr(row, "levelOfDetail").upper() == "SUMMARY"
    }
    valid_rows, warnings = [], []
    ignored = 0
    for row in open_positions:
        isin = _xml_attr(row, "isin").upper()
        detail = _xml_attr(row, "levelOfDetail").upper()
        if isin in summary_isins and detail and detail != "SUMMARY":
            ignored += 1
            continue
        parsed, error = _make_uploaded_position({
            "isin": isin,
            "qty": _xml_attr(row, "position", "quantity"),
            "mark": _xml_attr(row, "markPrice", "closePrice"),
            "value": _xml_attr(row, "positionValue", "marketValue"),
            "currency": _xml_attr(row, "currency"),
            "fx": _xml_attr(row, "fxRateToBase", "fxRateToEUR", "fxRate"),
            "symbol": _xml_attr(row, "symbol"),
        })
        if error:
            ignored += 1
            warnings.append(error)
        else:
            valid_rows.append(parsed)
    primary_dates, generated_dates = [], []
    for element in root.iter():
        for key, value in element.attrib.items():
            normalized = _header_key(key)
            if normalized in {"todate", "reportdate", "asofdate", "statementdate", "period"}:
                primary_dates.append(value)
            elif normalized == "whengenerated":
                generated_dates.append(value)
    return _finish_uploaded_report(
        valid_rows, len(open_positions), ignored, warnings,
        primary_dates or generated_dates,
    )


CSV_ALIASES = {
    "isin": {"isin", "securityid"},
    "qty": {"position", "quantity", "qty"},
    "mark": {"markprice", "closeprice", "price"},
    "value": {"positionvalue", "marketvalue", "value"},
    "currency": {"currency", "currencyprimary"},
    "fx": {"fxratetobase", "fxratetoeur", "fxrate"},
    "symbol": {"symbol", "ticker"},
    "date": {"reportdate", "asofdate", "todate", "statementdate", "date", "period"},
}


def _csv_field(headers, row, field):
    for index, header in enumerate(headers):
        if header in CSV_ALIASES[field] and index < len(row) and str(row[index]).strip():
            return row[index]
    return ""


def parse_uploaded_csv(text):
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    headers = None
    valid_rows, warnings, date_values = [], [], []
    ignored = 0
    position_rows = 0
    for row in rows:
        normalized = [_header_key(cell) for cell in row]
        has_isin = any(cell in CSV_ALIASES["isin"] for cell in normalized)
        has_position = any(cell in CSV_ALIASES["qty"] | CSV_ALIASES["value"] for cell in normalized)
        if has_isin and has_position:
            headers = normalized
            continue
        if _header_key(row[0] if row else "") == "statement" or any(
            cell in {"period", "reportdate", "asofdate", "todate", "statementdate"}
            for cell in normalized
        ):
            date_values.append(" ".join(row))
        if headers is None:
            continue
        isin = _csv_field(headers, row, "isin")
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", str(isin).strip().upper()):
            joined = " ".join(row)
            match = re.search(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", joined)
            isin = match.group(0) if match else ""
        if not isin:
            continue
        position_rows += 1
        date_value = _csv_field(headers, row, "date")
        if date_value:
            date_values.append(date_value)
        parsed, error = _make_uploaded_position({
            "isin": isin,
            "qty": _csv_field(headers, row, "qty"),
            "mark": _csv_field(headers, row, "mark"),
            "value": _csv_field(headers, row, "value"),
            "currency": _csv_field(headers, row, "currency"),
            "fx": _csv_field(headers, row, "fx"),
            "symbol": _csv_field(headers, row, "symbol"),
        })
        if error:
            ignored += 1
            warnings.append(error)
        else:
            valid_rows.append(parsed)
    if not position_rows:
        raise ValueError(
            "No CSV position rows with an ISIN were found. Include ISIN, quantity, mark price, currency, and FX rate to EUR."
        )
    return _finish_uploaded_report(
        valid_rows, position_rows, ignored, warnings, date_values
    )


def parse_uploaded_report(file_storage):
    raw = file_storage.read()
    if not raw:
        raise ValueError("The selected file is empty.")
    text = None
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("The report encoding is not supported; export it as UTF-8 CSV or XML.")
    text = text.strip()
    return parse_uploaded_xml(text) if text.startswith("<") else parse_uploaded_csv(text)


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
        try:
            position_count = int(request.args.get(
                "position_count", order_ticket_config(cfg)["max_positions"]
            ))
        except ValueError:
            position_count = order_ticket_config(cfg)["max_positions"]
        skipped = {
            value for value in request.args.get("skipped", "").split(",") if value
        }
        positions, cache, cached = get_positions(cfg, force_refresh=force)
    except Exception as e:  # surface a friendly message to the UI
        return jsonify({"error": str(e)}), 502
    view = build_view(cfg, positions, contribution, position_count, skipped)
    view["as_of"] = cache.get("as_of")
    view["fetched_at"] = cache.get("fetched_at")
    view["source"] = cache.get("source", "flex")
    view["cached"] = cached
    view["default_contribution"] = cfg.get("monthly_contribution", 0)
    view["rebalance_band_pct"] = cfg.get("rebalance_band_pct", 0.05)
    return jsonify(view)


@app.route("/api/import", methods=["POST"])
def api_import():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "Choose an XML or CSV report file."}), 400
    try:
        cfg = load_config()
        parsed = parse_uploaded_report(upload)
        target_isins = {target["isin"] for target in cfg["targets"]}
        matched = sum(1 for isin in parsed["row_isins"] if isin in target_isins)
        unmatched = len(parsed["row_isins"]) - matched
        summary = {
            "rows_parsed": parsed["rows_parsed"],
            "rows_matched": matched,
            "rows_unmatched": unmatched,
            "rows_ignored": parsed["rows_ignored"],
            "total_eur": parsed["total_eur"],
            "report_date": parsed["report_date"],
        }
        if request.form.get("confirm") == "1":
            # Parsing completes successfully before this atomic replacement;
            # every failure above leaves the old cache untouched.
            save_cache(
                parsed["positions"],
                as_of=parsed["report_date"],
                source="upload",
            )
            return jsonify({
                "committed": True,
                "summary": summary,
                "warnings": parsed["warnings"],
            })
        return jsonify({
            "committed": False,
            "summary": summary,
            "warnings": parsed["warnings"],
        })
    except Exception as exc:
        return jsonify({
            "error": str(exc),
            "holdings_unchanged": True,
        }), 400


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
