# Portfolio Dashboard (IBKR Flex)

A small local dashboard that reads your IBKR positions **read-only**, compares them to your
target allocation, and tells you exactly **how much of each ETF to buy this month** — filling
the most-underweight sleeves first, so your contributions do the rebalancing and you avoid
selling (no 26% Italian capital-gains event).

- **Read-only & safe.** The Flex token can only pull reports. It cannot trade or move money.
  Flex Web Service is an official IBKR feature — using it does not violate the Terms of Service.
- **No software to babysit.** It pulls once a day and caches. Open the page → fresh data. No
  gateway running, no live login.
- **Data is end-of-day.** IBKR regenerates the report once daily after market close — perfect
  for monthly rebalancing (intraday real-time is neither needed nor provided by this source).

---

## 1. One-time setup in IBKR (about 5 minutes)

### a) Turn on the Flex Web Service and get your **token**
1. Log in to **IBKR Client Portal**.
2. Go to **Settings → Account Settings** (or **Performance & Reports → Flex Queries**).
3. Find **Flex Web Service** → **Enable** → **Generate** a token.
4. Copy the token string. (It expires ~1 year — regenerate when it does.)

### b) Create the **Activity Flex Query** and get its **Query ID**
1. **Performance & Reports → Flex Queries → Activity Flex Query → (+) Create**.
2. Give it a name, e.g. `Positions`.
3. Under **Sections**, enable **Open Positions** and tick these fields (at minimum):
   - `Symbol`, `ISIN`, `Position` (quantity), `Mark Price`, `Position Value`,
     `Currency`, `FX Rate to Base`, `Asset Class`, `Level of Detail`
4. Set **Level of Detail = Summary** (avoids double-counting lots).
5. **Models/Format:** Format **XML**, Period **As of / Last business day**,
   Base currency **EUR**.
6. **Save**. Open the saved query and note its **Query ID** (a number).

---

## 2. Configure the app

1. Copy `config.example.json` to `config.json`.
2. Paste your **token** and **query_id** into the `ibkr` block.
3. Adjust `monthly_contribution` (default 1600) and the `targets` if you ever change the plan.

`config.json` and `cache.json` are git-ignored, so your token and positions never get committed.

> Prefer not to store the token in a file? Leave it as-is and instead set environment variables
> `IBKR_FLEX_TOKEN` and `IBKR_FLEX_QUERY_ID` — they override the file.

---

## 3. Run it

```bash
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000** in your browser.

- Change the **"This month I invest"** amount → the buy plan updates instantly.
- Click **↻ Refresh** to force a new pull from IBKR (otherwise it uses today's cache).

### Optional: auto pre-fetch each morning
Windows Task Scheduler → new daily task running:
```
python "C:\Users\Utente\Desktop\portfolio dashboard\app.py" --refresh
```
This warms the cache so the page is instant when you open it. Purely optional — the page
fetches on its own if the cache is stale.

---

## How the buy plan works

1. Reads your position values (converted to EUR).
2. Adds this month's contribution to the total, computes each sleeve's target € value.
3. Finds how far **below** target each sleeve is (the "deficit").
4. Allocates your contribution to cover the biggest deficits first. If the contribution is
   larger than all deficits combined, it tops everyone to target then spreads the rest by weight.
5. Converts € amounts to whole shares (rounding down; leftover rolls to next month).

It **never tells you to sell** — that's deliberate, to keep you clear of capital-gains tax.
If a sleeve drifts more than ±5pp above target it gets a badge, but the fix is simply that new
money goes elsewhere until it comes back into line.

---

## Troubleshooting

- **"IBKR token / query_id not set"** — you haven't filled in `config.json` (step 2).
- **"statement not ready after several retries"** — IBKR was still generating the report;
  wait a minute and hit Refresh. (Don't spam Refresh — IBKR soft-throttles very frequent pulls.)
- **A sleeve shows 0% / no shares** — you don't hold it yet, or the ISIN in `config.json`
  doesn't match the one IBKR reports. Check the ISIN.
- **Numbers look off by a currency** — make sure the Flex query includes `FX Rate to Base`
  and Base currency is EUR.
