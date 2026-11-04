#!/usr/bin/env python3
"""Fetch hourly wholesale prices and delivered gas prices into data/prices/.

Sources (all public, no keys):
  ERCOT  real-time settlement point prices at hubs
         - NP6-905-CD (report 12301), 15-minute, last ~8 days retained
         - NP6-785-ER (report 13061), yearly workbook, used to backfill gaps
  MISO   real-time final (and day-ahead ex-post) LMPs, daily CSVs on docs.misoenergy.org
  SPP    RTBM 5-minute LMP by settlement location, daily CSVs on portal.spp.org
  EIA    state delivered natural gas prices (industrial and commercial), monthly

Each market is cached as data/prices/<market>.csv with columns
  ts_utc,market,node,price
where ts_utc is the UTC start of the hour and price is $/MWh, averaged over the
hour. Runs are incremental: only days missing from the cache are fetched.

Usage: fetch.py [gas] [ercot] [miso] [spp] [--days N] [--workers N]
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data", "prices")
CONFIG = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))
UTC = dt.timezone.utc
CENTRAL = ZoneInfo("America/Chicago")
EST = dt.timezone(dt.timedelta(hours=-5))
UA = "CurrentTech-price-updater (+https://currenttech.energy)"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, tries=4, timeout=180):
    """GET with retries. Returns bytes, or None on 404."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:  # network hiccup
            err = e
        time.sleep(2 ** (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}: {err}")


def safe(fn):
    """Wrap a per-file fetch so one bad file is logged and skipped, not fatal."""
    def run(*a):
        try:
            return fn(*a)
        except Exception as e:
            log(f"skip {fn.__name__}{a}: {e}")
            return []
    return run


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:00Z")


# ---------------------------------------------------------------- cache

def load(market):
    """Cached hourly rows for nodes still listed in config.json."""
    path = os.path.join(DATA, f"{market}.csv")
    nodes = set(CONFIG[market]["nodes"])
    rows = {}
    if os.path.exists(path):
        with open(path) as f:
            for r in csv.DictReader(f):
                if r["node"] in nodes:
                    rows[(r["ts_utc"], r["market"], r["node"])] = float(r["price"])
    return rows


def save(market, rows):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, f"{market}.csv")
    keep_from = iso(dt.datetime.now(UTC) - dt.timedelta(days=CONFIG["keep_days"]))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_utc", "market", "node", "price"])
        for k in sorted(rows):
            if k[0] >= keep_from:
                w.writerow([*k, f"{rows[k]:.2f}"])
    os.replace(tmp, path)


def hourly(points):
    """points: iterable of (hour_start_utc, market, node, price) -> averaged dict."""
    acc = defaultdict(lambda: [0.0, 0])
    for t, m, n, p in points:
        a = acc[(iso(t), m, n)]
        a[0] += p
        a[1] += 1
    return {k: s / c for k, (s, c) in acc.items()}


def days_present(rows, market, node, tz, min_hours=23):
    """Local dates (in tz) that already have a full day for this node."""
    count = defaultdict(int)
    for (ts, m, n) in rows:
        if m == market and n == node:
            t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:00Z").replace(tzinfo=UTC)
            count[t.astimezone(tz).date()] += 1
    return {d for d, c in count.items() if c >= min_hours}


def wanted_days(days, lag=1):
    today = dt.date.today()
    return [today - dt.timedelta(days=i) for i in range(lag, days + 1)]


# ---------------------------------------------------------------- ERCOT

def ercot_local_hour(date_str, he, repeated):
    """ERCOT hour-ending (1-24, Central prevailing) -> UTC hour start."""
    d = dt.datetime.strptime(date_str, "%m/%d/%Y")
    local = d + dt.timedelta(hours=int(he) - 1)
    local = local.replace(tzinfo=CENTRAL, fold=1 if repeated in ("Y", True) else 0)
    return local.astimezone(UTC)


def ercot_doclist(report_type):
    raw = get(f"https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={report_type}")
    docs = json.loads(raw)["ListDocsByRptTypeRes"]["DocumentList"]
    return [d["Document"] for d in docs]


def ercot_download(doc_id):
    raw = get(f"https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={doc_id}")
    if not raw or raw[:2] != b"PK":
        raise RuntimeError(f"ERCOT doc {doc_id} did not return a zip (blocked or missing)")
    return zipfile.ZipFile(io.BytesIO(raw))


def ercot_recent(nodes, workers, need):
    """Report 12301: one small CSV per 15-minute interval, ~8 days retained.

    Only fetches intervals on days we still need, plus the last three days so
    partial days fill in.
    """
    today = dt.date.today()
    want = {d.strftime("%Y%m%d") for d in need} | {
        (today - dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(4)}
    docs = [d for d in ercot_doclist(12301)
            if d["FriendlyName"].endswith("_csv") and d["FriendlyName"].split("_")[1] in want]
    log(f"ercot: {len(docs)} interval files listed")

    def one(doc):
        out = []
        z = ercot_download(doc["DocID"])
        for name in z.namelist():
            for r in csv.DictReader(io.TextIOWrapper(z.open(name))):
                if r["SettlementPointName"] in nodes:
                    t = ercot_local_hour(r["DeliveryDate"], r["DeliveryHour"], r["DSTFlag"])
                    out.append((t, "rt", r["SettlementPointName"], float(r["SettlementPointPrice"])))
        return out

    pts = []
    with ThreadPoolExecutor(workers) as ex:
        for res in ex.map(safe(one), docs):
            pts.extend(res)
    return pts


def ercot_year(year, nodes):
    """Report 13061: yearly workbook of 15-minute hub and load zone prices."""
    import openpyxl
    docs = [d for d in ercot_doclist(13061) if d["FriendlyName"].endswith(str(year))]
    if not docs:
        return []
    z = ercot_download(docs[0]["DocID"])
    name = [n for n in z.namelist() if n.endswith(".xlsx")][0]
    wb = openpyxl.load_workbook(io.BytesIO(z.read(name)), read_only=True)
    pts = []
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        hdr = next(it)
        ix = {h: i for i, h in enumerate(hdr)}
        for r in it:
            n = r[ix["Settlement Point Name"]]
            if n in nodes:
                t = ercot_local_hour(r[ix["Delivery Date"]], r[ix["Delivery Hour"]], r[ix["Repeated Hour Flag"]])
                pts.append((t, "rt", n, float(r[ix["Settlement Point Price"]])))
    log(f"ercot: {year} workbook, {len(pts)} intervals")
    return pts


def fetch_ercot(days, workers):
    nodes = set(CONFIG["ercot"]["nodes"])
    rows = load("ercot")
    have = days_present(rows, "rt", CONFIG["ercot"]["nodes"][0], CENTRAL)
    need = [d for d in wanted_days(days) if d not in have]
    pts = ercot_recent(nodes, workers, need)
    recent = {t.astimezone(CENTRAL).date() for t, *_ in pts}
    gaps = sorted(d for d in need if d not in recent)
    for year in sorted({d.year for d in gaps}):
        try:
            pts.extend(ercot_year(year, nodes))
        except Exception as e:  # the yearly file is sometimes refused; retry next run
            log(f"ercot: could not fill {year} gaps from the yearly file: {e}")
    rows.update(hourly(pts))
    save("ercot", rows)
    log(f"ercot: {len(rows)} hourly rows cached")


# ---------------------------------------------------------------- MISO

def miso_day(d, kind):
    """kind 'rt' (real-time final, prelim fallback) or 'da' (day-ahead ex-post)."""
    stem = d.strftime("%Y%m%d")
    names = ["rt_lmp_final", "rt_lmp_prelim"] if kind == "rt" else ["da_expost_lmp"]
    for nm in names:
        raw = get(f"https://docs.misoenergy.org/marketreports/{stem}_{nm}.csv")
        if raw:
            break
    else:
        return []
    keep = set(CONFIG["miso"]["nodes"])
    base = dt.datetime(d.year, d.month, d.day, tzinfo=EST)  # MISO hours are EST, hour ending
    out = []
    for r in csv.reader(io.StringIO(raw.decode("utf-8", "replace"))):
        if len(r) >= 27 and r[2] == "LMP" and r[0] in keep:
            for h in range(24):
                if r[3 + h] != "":
                    out.append((base + dt.timedelta(hours=h), kind, r[0], float(r[3 + h])))
    return out


def fetch_miso(days, workers):
    rows = load("miso")
    probe = CONFIG["miso"]["probe_node"]
    jobs = []
    for kind in CONFIG["miso"]["markets"]:
        have = days_present(rows, kind, probe, EST, 24)
        # Re-fetch the last few days so preliminary real-time prices get replaced by final ones.
        recent = set(wanted_days(CONFIG["refresh_days"]))
        jobs += [(d, kind) for d in wanted_days(days) if d not in have or d in recent]
    log(f"miso: fetching {len(jobs)} day files")
    pts = []
    with ThreadPoolExecutor(workers) as ex:
        for res in ex.map(lambda j: safe(miso_day)(*j), jobs):
            pts.extend(res)
    rows.update(hourly(pts))
    save("miso", rows)
    log(f"miso: {len(rows)} hourly rows cached")


# ---------------------------------------------------------------- SPP

def spp_day(d):
    path = f"/{d:%Y}/{d:%m}/By_Day/RTBM-LMP-DAILY-SL-{d:%Y%m%d}.csv"
    raw = get("https://portal.spp.org/file-browser-api/download/rtbm-lmp-by-location?path=" + path, timeout=600)
    if not raw:
        return []
    keep = set(CONFIG["spp"]["nodes"])
    out = []
    rd = csv.reader(io.StringIO(raw.decode("utf-8", "replace")))
    hdr = [h.strip() for h in next(rd)]
    gi, ni, pi = hdr.index("GMT Interval"), hdr.index("Settlement Location Name"), hdr.index("LMP")
    for r in rd:
        if len(r) > pi and r[ni] in keep:
            ts = r[gi].strip()
            end = dt.datetime.strptime(ts, "%m/%d/%Y %H:%M:%S" if ts.count(":") == 2 else "%m/%d/%Y %H:%M").replace(tzinfo=UTC)
            start = (end - dt.timedelta(minutes=5)).replace(minute=0, second=0)
            out.append((start, "rt", r[ni], float(r[pi])))
    log(f"spp: {d} ok")
    return out


def fetch_spp(days, workers):
    rows = load("spp")
    have = days_present(rows, "rt", CONFIG["spp"]["nodes"][0], CENTRAL)
    need = [d for d in wanted_days(days) if d not in have]
    log(f"spp: fetching {len(need)} day files")
    pts = []
    with ThreadPoolExecutor(workers) as ex:
        for res in ex.map(safe(spp_day), need):
            pts.extend(res)
    rows.update(hourly(pts))
    save("spp", rows)
    log(f"spp: {len(rows)} hourly rows cached")


# ---------------------------------------------------------------- EIA gas

def fetch_gas():
    """EIA monthly delivered gas price by state and sector, $/Mcf.

    Uses the public dnav history workbooks (no API key). Series ids:
    N3035xx3 = industrial, N3020xx3 = commercial, where xx is the state code.
    """
    import xlrd
    sectors = {"industrial": "N3035", "commercial": "N3020"}
    out = []
    for state in sorted({m["gas_state"] for m in CONFIG["regions"].values()}):
        for sector, prefix in sectors.items():
            sid = f"{prefix}{state}3"
            raw = get(f"https://www.eia.gov/dnav/ng/hist_xls/{sid}m.xls")
            book = xlrd.open_workbook(file_contents=raw)
            sh = book.sheet_by_name("Data 1")
            for i in range(3, sh.nrows):
                v = sh.cell_value(i, 1)
                if v == "":
                    continue
                ym = xlrd.xldate_as_datetime(sh.cell_value(i, 0), book.datemode).strftime("%Y-%m")
                out.append((ym, state, sector, float(v)))
    path = os.path.join(DATA, "gas.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "state", "sector", "usd_per_mcf"])
        for r in sorted(out):
            if r[0] >= "2015-01":
                w.writerow(r)
    log(f"gas: {len(out)} monthly values")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", nargs="*", default=["gas", "ercot", "miso", "spp"])
    ap.add_argument("--days", type=int, default=CONFIG["fetch_days"])
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    failed = []
    for w in a.what:
        try:
            if w == "gas":
                fetch_gas()
            else:
                globals()[f"fetch_{w}"](a.days, a.workers)
        except Exception as e:
            log(f"{w}: FAILED: {e}")
            failed.append(w)
    if failed:
        log("failed: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
