# Portfolio Rebalancer

A two-part tool for running a fixed-allocation ETF portfolio: it shows how far each
sleeve has **drifted from target** and tells you **exactly what to buy this month** —
always by buying the most-underweight sleeves, never by selling (so you avoid the 26%
Italian capital-gains event).

There are two ways to use it, sharing the same buy-plan logic:

| | 📱 **PWA (this repo, hosted)** | 🖥️ **Local Python app** (`local-ibkr/`) |
|---|---|---|
| Where | Phone / any browser, installable | Your PC |
| Data in | You paste/enter your positions | **Auto-pulls** from IBKR (read-only) |
| Connection | None — 100% offline, on-device | IBKR Flex Web Service |
| Best for | Quick check anywhere | Hands-off daily refresh |

> **Why two versions?** A hosted web page can't talk to IBKR (no browser-callable API,
> and a token must never live in a public page). So the phone app is a *snapshot
> calculator* you feed your positions to, and the auto-IBKR pull lives in the local
> Python app on your PC. Both compute the identical recommendation.

---

## 📱 The PWA (installable app)

**Live:** `https://paolotesta001.github.io/portfolio-dashboard/`  ← install it from here

- Set your target % per sleeve (defaults to a 65/35 all-weather mix; edit via ⚙︎).
- Enter each sleeve's current value, **or paste your IBKR positions export** and it
  reads the ISINs and values automatically.
- Type this month's amount → it shows drift, the buy list, and approximate shares.
- Everything is saved **in your browser only** — nothing is uploaded, no account linked.
- Installable & offline: "Add to Home Screen" on iOS, "Install app" on Android/desktop.

Source lives at the repo root; `docs/` is the copy GitHub Pages serves.

---

## 🖥️ The local Python app (auto-IBKR)

In [`local-ibkr/`](local-ibkr/). Pulls your positions once a day via IBKR's official,
**read-only** Flex Web Service (it cannot trade or move money), caches them, and serves
the same dashboard at `http://127.0.0.1:5000`.

```bash
cd local-ibkr
pip install -r requirements.txt
# copy config.example.json -> config.json and add your Flex token + query id
python app.py
```

Full setup (getting the IBKR token & Flex query) is in [`local-ibkr/README.md`](local-ibkr/README.md).

---

## How the recommendation works

See **[HOW-IT-WORKS.md](HOW-IT-WORKS.md)** for the full walkthrough. In short: it adds your
contribution to the total, finds how far *below* target each sleeve sits, and pours the
money into the biggest gaps first — so your monthly buys quietly pull everything back to
target and you never have to sell.

---

*Educational tool, not financial advice. Your token and positions are never committed to
this repo (`config.json` and `cache.json` are git-ignored; the PWA keeps data on-device).*
