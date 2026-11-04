# CurrentTech Energy website

The static site for [currenttech.energy](https://currenttech.energy/). Plain HTML and one stylesheet, no build step. It shares its palette, type and icon with [ERCOT Bids](https://ercotbids.currenttech.energy/).

| File | Page |
|---|---|
| `index.html` | Home: the gas-parity numbers, services, process, who we work with, ERCOT Bids, about teaser |
| `about/index.html` | About Blair (keeps the old WordPress `/about/` URL) |
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

The home page compares hourly electricity prices with **delivered** gas in an 80% efficient boiler:

- ERCOT: 2025 real-time hub prices from `site/data/prices_years.json.gz` in the ERCOT Bids repository, against $0.40/therm ($4.00/MMBtu). Break-even is 4.00 / 0.80 × 3.412 ≈ $17/MWh for an electric boiler and twice that (≈ $34/MWh) for a heat pump at COP 2.
- Wisconsin (MISO): a utility real-time rate, July 2025 to June 2026, against $0.65/therm ($6.50/MMBtu). Break-even ≈ $28/MWh for an electric boiler, $55/MWh for a COP 2 heat pump.
