#!/usr/bin/env python3
"""Fetch one closing price per instrument and write docs/prices.json.

Runs on GitHub Actions once a day. The app then reads that file from its own
domain, so there is no API key in the browser and no cross-origin request.

Which instruments to price is read from docs/instruments.json:

    { "IE00BD4TXV59": "UETW.DE", "IE00B0M62X26": "IBCI.AS" }

The value is the Yahoo Finance symbol (.DE = Xetra, .AS = Euronext Amsterdam,
.MI = Borsa Italiana, .L = London). Leave it as null and the script looks the
symbol up from the ISIN, preferring a euro listing.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTRUMENTS = os.path.join(ROOT, 'docs', 'instruments.json')
OUTPUT = os.path.join(ROOT, 'docs', 'prices.json')
HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                         '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
PREFERRED_EXCHANGES = ('GER', 'AMS', 'MIL', 'EBS', 'PAR')


def fetch(url, attempts=3):
    last = None
    for n in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode('utf-8', 'replace'))
        except Exception as exc:            # noqa: BLE001 - retry anything transient
            last = exc
            time.sleep(2 * (n + 1))
    raise last


def resolve_symbol(isin):
    """Find a Yahoo symbol for an ISIN, preferring a European/euro listing."""
    url = ('https://query2.finance.yahoo.com/v1/finance/search?quotesCount=8&q='
           + urllib.parse.quote(isin))
    quotes = fetch(url).get('quotes', [])
    for wanted in PREFERRED_EXCHANGES:
        for q in quotes:
            if q.get('exchange') == wanted and q.get('symbol'):
                return q['symbol']
    return quotes[0]['symbol'] if quotes and quotes[0].get('symbol') else None


def quote(symbol):
    """Latest close for one symbol, as (price, currency, YYYY-MM-DD)."""
    url = ('https://query1.finance.yahoo.com/v8/finance/chart/'
           + urllib.parse.quote(symbol) + '?range=5d&interval=1d')
    meta = fetch(url)['chart']['result'][0]['meta']
    price = meta.get('regularMarketPrice') or meta.get('chartPreviousClose')
    if not price:
        raise ValueError('no price in response')
    stamp = meta.get('regularMarketTime')
    day = (datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d')
           if stamp else datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    return float(price), meta.get('currency', ''), day


def main():
    with open(INSTRUMENTS, encoding='utf-8') as fh:
        wanted = json.load(fh)

    previous = {}
    if os.path.exists(OUTPUT):
        try:
            with open(OUTPUT, encoding='utf-8') as fh:
                previous = json.load(fh).get('prices', {})
        except Exception:                    # noqa: BLE001 - a corrupt file is not fatal
            previous = {}

    prices, failed, days = {}, [], []
    for isin, symbol in wanted.items():
        try:
            symbol = symbol or resolve_symbol(isin)
            if not symbol:
                raise ValueError('no symbol found for this ISIN')
            price, currency, day = quote(symbol)
            if currency != 'EUR':
                # A non-euro listing would silently corrupt the allocation maths.
                raise ValueError(f'{symbol} quotes in {currency}, not EUR')
            prices[isin] = {'price': round(price, 4), 'currency': currency,
                            'symbol': symbol, 'date': day}
            days.append(day)
            print(f'{isin}  {symbol:10} {price:>10.4f} {currency}  {day}')
        except Exception as exc:             # noqa: BLE001 - one bad symbol must not stop the rest
            failed.append(f'{isin}: {exc}')
            if isin in previous:             # keep yesterday's number rather than dropping it
                prices[isin] = previous[isin]
                print(f'{isin}  FAILED ({exc}) - kept {previous[isin]["date"]}')
            else:
                print(f'{isin}  FAILED ({exc})')

    if not prices:
        print('No prices at all - leaving the existing file untouched.', file=sys.stderr)
        return 1

    payload = {
        'as_of': max(days) if days else datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'updated': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'source': 'Yahoo Finance - closing price on the listed exchange',
        'currency': 'EUR',
        'failed': failed,
        'prices': prices,
    }
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write('\n')
    print(f'\nwrote {OUTPUT}: {len(prices)} price(s), as of {payload["as_of"]}'
          + (f', {len(failed)} failure(s)' if failed else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
