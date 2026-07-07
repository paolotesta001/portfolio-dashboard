# How the Portfolio Dashboard works

A plain-language walkthrough of what this tool does, how the pieces fit together,
and the exact logic behind the "buy this month" recommendation.

---

## The one-sentence version

Every day it quietly downloads your IBKR positions (read-only), works out how far
each part of your portfolio has drifted from the mix you chose, and tells you which
ETFs to buy with this month's money so you drift *back* toward target — **without
ever selling** (so you never trigger the 26% Italian capital-gains tax).

---

## The moving parts

```
   IBKR (your account)
        │  Flex Web Service  (read-only, official IBKR feature)
        ▼
   app.py  ──►  fetches positions  ──►  caches them for the day (cache.json)
        │
        │  reads your target mix + monthly amount (config.json)
        ▼
   build_view()  ──►  computes drift + the buy plan
        │
        ▼
   dashboard.html  ──►  the web page you open at http://127.0.0.1:5000
```

There are only two files you ever touch:
- **`config.json`** — your token, your target %, your monthly amount. (Private, never uploaded.)
- **`dashboard.html`** — the page. You don't edit it; you just open it in a browser.

---

## Step 1 — Getting your positions (the IBKR Flex flow)

IBKR doesn't have an "always-on" API a website can call. Instead it has the **Flex Web
Service**, which works in two polite steps:

1. `app.py` sends your **token** + **query id** to IBKR and gets back a *reference code*.
2. It then asks for that reference code. IBKR may reply "still generating…" (error 1019),
   so the app waits a few seconds and asks again until the report is ready.

The report comes back as XML listing every position: ISIN, quantity, mark price, currency,
and the FX rate to your base currency (EUR). The app converts everything to **EUR** so a
US-dollar-priced fund (like the gold ETC) is compared fairly against your euro funds.

> **Why this is safe:** the Flex token is *read-only*. It can pull reports but cannot place
> a trade or move a cent. It's an official IBKR feature you enable yourself. Nothing here
> can touch your money.

---

## Step 2 — Caching (why it's instant, and why it's not "live")

IBKR only refreshes Flex data **once a day**, after market close. So there's no point
hammering it. The app saves each pull to `cache.json` tagged with the date:

- Open the page → if today's data is already cached, it loads **instantly**.
- If the cache is from yesterday (or you click **↻ Refresh**), it pulls a fresh copy.

This answers "does it update all the time?" — it updates **once per day, automatically,
whenever you open it**. Intraday second-by-second prices aren't provided by this data
source, and you don't need them: rebalancing is a monthly decision, not a live-trading one.

---

## Step 3 — Measuring drift

For each sleeve the app computes:

```
current %  =  (that sleeve's EUR value)  ÷  (total invested EUR)
drift      =  current %  −  target %
```

- **Negative drift** = you're *underweight* (own too little) → a candidate to buy.
- **Positive drift** = you're *overweight* (own too much) → leave it alone.

If any sleeve drifts more than **±5 percentage points** from target it gets a small badge,
so you can see at a glance what's most out of line. The table is sorted **most-underweight
first**, so the thing you should buy is always at the top.

---

## Step 4 — The buy plan (the important bit)

This is the "fill the most-underweight sleeves first" method. Given your monthly amount `C`:

1. Pretend the money is already in: `new_total = current_total + C`.
2. For each sleeve, compute its **target euro value** = `new_total × target %`.
3. Compute each sleeve's **deficit** = how far *below* that target it currently sits
   (never negative — overweight sleeves have a deficit of 0).
4. Pour `C` into the deficits:
   - If `C` is **smaller** than the total deficit → split it across the underweight sleeves
     in proportion to how underweight each one is (the most-underweight gets the most).
   - If `C` is **larger** than the total deficit → top every sleeve up to target, then
     spread the leftover across all sleeves by their target weight.
5. Convert each euro amount to **whole shares** (round down). The few euros left over from
   rounding "roll over" to next month.

**The key property: every number is a *buy*. It never says "sell."** That's deliberate —
selling in Italy realises a 26% capital-gains tax, so we rebalance purely by steering new
contributions. Over time your monthly buys quietly pull everything back to target.

### A worked example
Say Gold has slumped and sits at 1.8% when its target is 10%, while everything else is near
target. With €1,600 to invest, the whole €1,600 is directed to Gold — because that's the
biggest deficit — buying ~32 shares, with ~€16 rolling to next month. Next time Gold is back
in line, the money flows to whatever has fallen behind since.

---

## Step 5 — The page

`dashboard.html` shows:
- **Invested value**, your **monthly amount** (editable — change it and the plan recomputes
  instantly, no re-fetch needed), and the **roll-over cash**.
- **Buy this month** — the headline list: sleeve, euro amount, approximate shares.
- **Allocation vs target** — a table with a little bar per sleeve (fill = current, white tick
  = target) so you can *see* over/under at a glance.
- **Other holdings** — anything in your IBKR account that isn't one of your seven sleeves is
  listed separately and excluded from the math (so a stray position can't skew the plan).

---

## What you do each month

1. Open the page (data auto-refreshes).
2. Read the **Buy this month** list.
3. Place those buys at IBKR — **use limit orders** and confirm the exact share count there
   (the app's share numbers are approximate, based on end-of-day prices).
4. Done. No selling, no spreadsheet, no guesswork.

Once or twice a year, glance at the drift badges; if something is wildly overweight and new
money alone isn't fixing it, *then* consider a one-off sell — but that's the rare exception.

---

## Honest limitations

- **End-of-day, not real-time.** By design. Fine for monthly investing.
- **Share counts are approximate.** EUR-converted close prices; confirm at order time.
- **It assumes you buy what it says.** The drift math is only correct if your actual buys
  roughly follow the plan.
- **It only knows your seven sleeves.** Anything else is listed but ignored in the %.
- **It's a calculator, not advice.** It executes *your* chosen strategy; it doesn't judge it.
