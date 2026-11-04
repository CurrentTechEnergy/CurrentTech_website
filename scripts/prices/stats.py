#!/usr/bin/env python3
"""Recompute "hours cheaper than gas" stats and write them into the site.

Reads data/prices/*.csv (from fetch.py), writes data/market-stats.json, and
updates every element in the HTML pages that carries a data-stat attribute:

  data-stat="region.key"        text content is replaced with the formatted value
  data-stat-width="region.key"  style width is set to the value as a percent
  data-stat-svgw="region.key"   SVG width is set to the value as a share of 300
  data-stat-svgx="region.key"   SVG x is set to 110 + share of 300 (bar labels)

Method, per region, over the trailing window (default 365 days, ending with
the last full local day in the data):
  - delivered gas = EIA monthly state price for the configured sector, averaged
    over the window's months. A month EIA has not published yet uses the same
    month a year earlier, so the average keeps its winter weighting.
  - break-even for an electric boiler = gas $/MMBtu / boiler efficiency
    * 3.412 MMBtu/MWh * electric boiler efficiency; a heat pump multiplies by COP.
  - an hour "beats gas" when its hourly price is below the break-even.

Usage: stats.py [--check]   (--check prints the stats and leaves files alone)
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DATA = os.path.join(ROOT, "data", "prices")
CONFIG = json.load(open(os.path.join(HERE, "config.json")))
UTC = dt.timezone.utc
MMBTU_PER_MWH = 3.412


def load_series(file, market, node):
    out = {}
    with open(os.path.join(DATA, f"{file}.csv")) as f:
        for r in csv.DictReader(f):
            if r["market"] == market and r["node"] == node:
                out[dt.datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:00Z").replace(tzinfo=UTC)] = float(r["price"])
    return out


def load_gas():
    gas = defaultdict(dict)
    with open(os.path.join(DATA, "gas.csv")) as f:
        for r in csv.DictReader(f):
            gas[(r["state"], r["sector"])][r["month"]] = float(r["usd_per_mcf"])
    return gas


def month_add(ym, n):
    y, m = map(int, ym.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def apply_model(p, model):
    """Optional tariff model, e.g. a utility rate indexed to an LMP."""
    if not model:
        return p
    p = p * model.get("scale", 1.0) + model.get("adder", 0.0)
    if "floor" in model:
        p = max(p, model["floor"])
    return p


def region_stats(name, cfg, gas):
    tz = ZoneInfo(cfg["tz"])
    series = load_series(cfg["file"], cfg["market"], cfg["node"])
    if not series:
        raise RuntimeError(f"{name}: no data for {cfg['node']}")
    # Window ends with the last local day that has (nearly) all its hours.
    per_day = defaultdict(int)
    for t in series:
        per_day[t.astimezone(tz).date()] += 1
    end = max(d for d, c in per_day.items() if c >= 23)
    start = end - dt.timedelta(days=CONFIG["window_days"] - 1)
    hours = {t: apply_model(p, cfg.get("price_model")) for t, p in series.items()
             if start <= t.astimezone(tz).date() <= end}
    expected = sum(24 for _ in range(CONFIG["window_days"]))
    if len(hours) < 0.97 * expected:
        raise RuntimeError(f"{name}: only {len(hours)} of ~{expected} hours in the window")

    # Delivered gas over the window's months.
    g = gas[(cfg["gas_state"], cfg["gas_sector"])]
    months, used, filled = sorted({t.astimezone(tz).strftime("%Y-%m") for t in hours}), [], 0
    for ym in months:
        v = g.get(ym)
        if v is None:
            v, filled = g.get(month_add(ym, -12)), filled + 1
        if v is None:
            raise RuntimeError(f"{name}: no EIA gas price for {ym} or a year earlier")
        used.append(v)
    gas_mcf = sum(used) / len(used)
    gas_mmbtu = gas_mcf / CONFIG["gas_heat_content_mmbtu_per_mcf"]
    be_eb = gas_mmbtu / CONFIG["gas_boiler_efficiency"] * MMBTU_PER_MWH * CONFIG["electric_boiler_efficiency"]
    be_hp = be_eb * CONFIG["heat_pump_cop"]

    vals = list(hours.values())
    n = len(vals)
    spring = [p for t, p in hours.items() if t.astimezone(tz).month in (4, 5, 6)]
    cheap = CONFIG["cheap_threshold_usd_mwh"]
    eb = sum(p < be_eb for p in vals)
    hp = sum(p < be_hp for p in vals)
    neg = sum(p < 0 for p in vals)
    lo = sum(p < cheap for p in vals)
    return {
        "node": cfg["node"],
        "market": cfg["market"],
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "hours": n,
        "gas_state": cfg["gas_state"],
        "gas_sector": cfg["gas_sector"],
        "gas_usd_mcf": round(gas_mcf, 3),
        "gas_usd_mmbtu": round(gas_mmbtu, 3),
        "gas_usd_therm": round(gas_mmbtu / 10, 4),
        "gas_months_estimated": filled,
        "breakeven_eb": round(be_eb, 2),
        "breakeven_hp": round(be_hp, 2),
        "eb_hours": eb,
        "eb_pct": round(100 * eb / n, 2),
        "hp_hours": hp,
        "hp_pct": round(100 * hp / n, 2),
        "neg_hours": neg,
        "neg_pct": round(100 * neg / n, 2),
        "cheap_hours": lo,
        "cheap_pct": round(100 * lo / n, 2),
        "spring_eb_pct": round(100 * sum(p < be_eb for p in spring) / len(spring), 2) if spring else None,
        "mean_price": round(sum(vals) / n, 2),
    }


def period_label(start, end):
    s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return f"{s:%b} {s.day}, {s.year} to {e:%b} {e.day}, {e.year}"


def fmt(key, v, stats):
    """Format a value for display. Percents are whole numbers, hours round to 10."""
    if key.endswith("_pct"):
        return f"{round(v):d}"
    if key.endswith("_hours"):
        return f"{int(round(v, -1)):,}"
    if key.startswith("gas_usd"):
        return f"{v:.2f}"
    if key.startswith("breakeven"):
        return f"{round(v):d}"
    return str(v)


def lookup(stats, ref):
    region, key = ref.split(".", 1)
    if region == "site":
        return key, stats["site"][key]
    return key, stats["regions"][region][key]


ATTR = re.compile(r'<(?P<tag>[a-zA-Z]+)(?P<attrs>[^>]*?\sdata-stat(?P<kind>|-width|-svgw|-svgx)="(?P<ref>[^"]+)"[^>]*)>')


def render(html, stats):
    out, pos = [], 0
    for m in ATTR.finditer(html):
        key, v = lookup(stats, m.group("ref"))
        kind = m.group("kind")
        tag_text = m.group(0)
        if kind == "-width":
            tag_text = re.sub(r'width:\s*[\d.]+%', f'width:{round(v)}%', tag_text)
        elif kind in ("-svgw", "-svgx"):
            px = round(300 * v / 100)
            attr = "width" if kind == "-svgw" else "x"
            val = px if kind == "-svgw" else 110 + px - 12
            tag_text = re.sub(rf'\s{attr}="[\d.]+"', f' {attr}="{val}"', tag_text, count=1)
        out.append(html[pos:m.start()])
        out.append(tag_text)
        pos = m.end()
        if kind == "":
            close = html.find(f"</{m.group('tag')}>", pos)
            out.append(fmt(key, v, stats) if not isinstance(v, str) else v)
            pos = close
    out.append(html[pos:])
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    gas = load_gas()
    regions = {}
    for name, cfg in CONFIG["regions"].items():
        try:
            regions[name] = region_stats(name, cfg, gas)
        except Exception as e:
            # Regions the site shows are required; others (SPP, the raw MISO
            # real-time comparison) are recorded in the JSON when available.
            if cfg.get("required", True):
                raise
            print(f"skipping {name}: {e}", file=sys.stderr)
    e = regions["ercot_west"]
    hp = sorted(round(regions[r]["hp_pct"]) for r in ("ercot_west", "ercot_hubavg"))
    stats = {
        "built": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ"),
        "assumptions": {k: CONFIG[k] for k in ("window_days", "gas_boiler_efficiency", "electric_boiler_efficiency",
                                                "heat_pump_cop", "gas_heat_content_mmbtu_per_mcf", "cheap_threshold_usd_mwh")},
        "site": {
            "ercot_period": period_label(e["window_start"], e["window_end"]),
            "wisconsin_period": period_label(regions["wisconsin"]["window_start"], regions["wisconsin"]["window_end"]),
            "ercot_hp_low": f"{hp[0]}",
            "ercot_hp_suffix": "%" if hp[0] == hp[1] else f"–{hp[1]}%",
            "ercot_hp_mid_pct": (hp[0] + hp[1]) / 2,
            "updated": dt.date.today().strftime("%b %-d, %Y"),
        },
        "regions": regions,
    }
    if a.check:
        json.dump(stats, sys.stdout, indent=1)
        return
    with open(os.path.join(ROOT, "data", "market-stats.json"), "w") as f:
        json.dump(stats, f, indent=1)
        f.write("\n")
    for page in glob.glob(os.path.join(ROOT, "**", "*.html"), recursive=True):
        html = open(page).read()
        if "data-stat" not in html:
            continue
        new = render(html, stats)
        if new != html:
            open(page, "w").write(new)
            print(f"updated {os.path.relpath(page, ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
