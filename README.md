# CurrentTech Energy website

The static site for [currenttech.energy](https://currenttech.energy/). Plain HTML and one stylesheet, no build step. It shares its palette, type and icon with [ERCOT Bids](https://ercotbids.currenttech.energy/).

| File | Page |
|---|---|
| `index.html` | Home: hero, three headline gas-parity numbers, keep your gas boilers, who we work with, about teaser |
| `tariff-analysis/index.html` | Tariff analysis: utility rates (WPS, OG&E, ERCOT transmission-connected), then wholesale MISO, SPP and ERCOT, ERCOT Bids |
| `markets/index.html` | Redirect to `tariff-analysis/` (old address) |
| `how-we-work/index.html` | Redirect to the home page (page folded into home) |
| `about/index.html` | Redirect to the home page bio (keeps the old WordPress `/about/` URL) |
| `404.html` | Not found |
| `style.css` | All styles, light and dark |

## Preview locally

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000.

## Publish with GitHub Pages

1. **Settings → Pages**: Source "Deploy from a branch", branch `main`, folder `/ (root)`. The site appears at `https://currenttechenergy.github.io/CurrentTech_website/` (links are relative, so it works there).
2. When it looks right, set **Custom domain** to `currenttech.energy` (this adds a `CNAME` file) and tick **Enforce HTTPS**.
3. At the DNS provider, point the apex domain at GitHub Pages: A records `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`, and `www` as a CNAME to `currenttechenergy.github.io`. Leave the `ercotbids` record alone.
4. Once the domain serves this site, the WordPress hosting can be cancelled.

## Updating the numbers

The home page numbers update themselves. Every day the **Update market prices** workflow (`.github/workflows/update-prices.yml`) runs:

1. `scripts/prices/fetch.py` pulls hourly prices into `data/prices/` (only the days not already cached):
   - ERCOT real-time hub prices: ERCOT reports NP6-905-CD (last ~8 days) and NP6-785-ER (yearly, to fill gaps).
   - MISO real-time final and day-ahead ex-post LMPs: daily CSVs from docs.misoenergy.org.
   - SPP real-time (RTBM) LMPs at SPP North and South hubs, and day-ahead LMPs for the OG&E zone: daily CSVs from portal.spp.org.
   - Delivered natural gas: EIA monthly industrial and commercial price by state (not Henry Hub).
2. `scripts/prices/stats.py` computes, for the last 365 days, the share of hours an electric boiler and a COP 2 heat pump beat an 80% efficient gas boiler, writes `data/market-stats.json`, and rewrites every element in the HTML that has a `data-stat` attribute. It also redraws the home page's ERCOT West price duration curve (`svg#dc`) from the same 365 days: the curve, the gas break-even line, the crossing, the label positions (elements with `data-geom`), and the hover data in the page's inline script.

Break-even is gas $/MMBtu / 0.80 × 3.412 for an electric boiler, times 2 for a heat pump. Gas is the EIA state industrial price averaged over the window's months; months EIA hasn't published yet use the same month a year earlier.

- ERCOT: West Hub and the hub average, against Texas industrial gas.
- Wisconsin: the Wisconsin Public Service real-time rate, modeled as max(1.053 × MISO day-ahead LMP at WPS.AZ + $0.53, $7)/MWh. That fit reproduces Blair's July 2025 to June 2026 bill data (correlation 0.999). Against Wisconsin industrial gas. The raw MISO real-time LMP at WPS.AZ is also kept in the JSON for comparison (`wisconsin_rt_lmp`).
- SPP: North and South hub real-time prices, against Nebraska and Oklahoma industrial gas.
- MISO: real-time LMP at WPS.AZ (`wisconsin_rt_lmp`), shown as the MISO wholesale row.
- OG&E Day-Ahead Pricing (Oklahoma schedule DAP, sheet 33.03): price = marginal supply cost × loss factor + 0.5¢/kWh. Modeled as SPP day-ahead LMP at `OKGE_OKGE` × 1.053 + $5/MWh, with marginal capacity cost assumed zero in the low-price hours. The loss factor is assumed (same as the WPS fit); OG&E's actual hourly prices are confidential.

The page shows wholesale real-time prices by ISO first (ERCOT, MISO, SPP), then utility hourly rates (WPS, OG&E).

Assumptions (window, efficiencies, nodes, gas states) live in `scripts/prices/config.json`. To refresh by hand: `pip install openpyxl xlrd`, then `python3 scripts/prices/fetch.py && python3 scripts/prices/stats.py`, or run the workflow from the Actions tab.

When editing copy in `#numbers` or the ERCOT Bids chart, keep the `data-stat` and `data-geom` attributes; the words around them are free to change.
