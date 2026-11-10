#!/usr/bin/env python3
"""Recompute "hours cheaper than gas" stats and write them into the site.

Reads data/prices/*.csv (from fetch.py), writes data/market-stats.json, and
updates every element in the HTML pages that carries a data-stat attribute:

  data-stat="region.key"        text content is replaced with the formatted value
  data-stat-width="region.key"  style width is set to the value as a percent
  data-stat-svgw="region.key"   SVG width is set to the value as a share of 300
  data-stat-svgx="region.key"   SVG x is set to 110 + share of 300 (bar labels)
  data-geom="dc.key"            attributes of the price duration chart (path d,
                                line and label positions) are redrawn, and the
                                chart's hover data (var d={...}) is replaced

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
PRICES = {}  # region -> hourly prices in its window, kept for the chart


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
    PRICES[name] = vals
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


# Price duration curve on the home page (svg#dc). Geometry must match the
# SVG's viewBox, axes and the hover script in index.html.
DC = {"region": "ercot_west", "W": 720, "H": 360, "L": 64, "R": 16, "T": 24, "B": 48,
      "ymin": -40, "ymax": 100, "clamp": 400, "hover_step": 24}


def duration_curve(prices, be):
    """Geometry for the sorted-price chart: {element key: {attribute: value}}, plus hover data."""
    c = DC
    p = sorted(prices, reverse=True)
    n = len(p)
    pw, ph = c["W"] - c["L"] - c["R"], c["H"] - c["T"] - c["B"]
    X = lambda i: c["L"] + pw * i / n
    Y = lambda v: c["T"] + ph * (c["ymax"] - max(-c["clamp"], min(c["clamp"], v))) / (c["ymax"] - c["ymin"])
    # Dense sampling where the curve is steep (the first ~400 hours), sparse after.
    idx = sorted(set(list(range(0, 400, 2)) + list(range(408, n, 12)) + [n - 1]))
    pts = lambda ii: " L".join(f"{X(i):.1f},{Y(p[i]):.1f}" for i in ii)
    gy = min(max(Y(be), c["T"] + 30), c["H"] - c["B"] - 4)
    k = sum(v >= be for v in p)  # hours at or above break-even; the curve crosses at rank k
    cx = X(k)
    # Keep the curve's label just above the curve near x=160.
    i160 = round((160 - c["L"]) / pw * n)
    label_y = max(c["T"] + 16, min(Y(p[i160]) - 19, gy - 40))
    geom = {
        "price": {"d": "M" + pts(idx)},
        "area": {"d": f"M{cx:.1f},{gy:.1f} L" + pts([i for i in idx if i > k]) + f" L{X(n):.1f},{gy:.1f} Z"},
        "gas": {"y1": f"{gy:.1f}", "y2": f"{gy:.1f}"},
        "cross": {"x1": f"{cx:.1f}", "x2": f"{cx:.1f}", "y1": f"{gy:.1f}"},
        "gas_label": {"y": f"{gy - 8:.1f}"},
        "eb_label": {"y": f"{gy - 28:.1f}"},
        "eb_hours": {"y": f"{gy - 8:.1f}"},
        "price_label": {"y": f"{label_y:.1f}"},
    }
    hover = {"n": n, "step": c["hover_step"], **{k2: c[k2] for k2 in ("L", "R", "T", "B", "W", "H", "ymin", "ymax")},
             "be": round(be, 2), "p": [round(v, 1) + 0.0 for v in p[::c["hover_step"]]]}
    return geom, hover


GEOM = re.compile(r'<[a-zA-Z]+[^>]*?\sdata-geom="(?P<ref>[^"]+)"[^>]*>')
HOVER = re.compile(r'var d=\{"n":[^;]*?\]\}')


def render_chart(html, geom, hover):
    def tag(m):
        t = m.group(0)
        for attr, val in geom[m.group("ref").split(".", 1)[1]].items():
            t = re.sub(rf'(\s{attr}=")[^"]*"', lambda mm: f'{mm.group(1)}{val}"', t, count=1)
        return t
    html = GEOM.sub(tag, html)
    return HOVER.sub(lambda m: "var d=" + json.dumps(hover, separators=(",", ":")), html)


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
            "miso_period": period_label(regions["wisconsin_rt_lmp"]["window_start"], regions["wisconsin_rt_lmp"]["window_end"]),
            "oge_period": period_label(regions["oge"]["window_start"], regions["oge"]["window_end"]),
            "spp_period": period_label(regions["spp_south"]["window_start"], regions["spp_south"]["window_end"]),
            "ercot_hp_low": f"{hp[0]}",
            "ercot_hp_suffix": "%" if hp[0] == hp[1] else f"–{hp[1]}%",
            "ercot_hp_mid_pct": (hp[0] + hp[1]) / 2,
            "updated": dt.date.today().strftime("%b %-d, %Y"),
            "dc_desc": (f"The sorted price curve falls below the gas boiler break-even line for roughly the cheapest "
                        f"{round(e['eb_pct'])} percent of hours, and below zero for about {round(e['neg_pct'])} percent."),
        },
        "regions": regions,
    }
    if a.check:
        json.dump(stats, sys.stdout, indent=1)
        return
    with open(os.path.join(ROOT, "data", "market-stats.json"), "w") as f:
        json.dump(stats, f, indent=1)
        f.write("\n")
    geom, hover = duration_curve(PRICES[DC["region"]], regions[DC["region"]]["breakeven_eb"])
    for page in glob.glob(os.path.join(ROOT, "**", "*.html"), recursive=True):
        html = open(page).read()
        if "data-stat" not in html:
            continue
        new = render(html, stats)
        if "data-geom" in new:
            new = render_chart(new, geom, hover)
        if new != html:
            open(page, "w").write(new)
            print(f"updated {os.path.relpath(page, ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
