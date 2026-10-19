#!/usr/bin/env python3
"""
Indonesia Wildfire Dashboard builder.

Downloads live NASA FIRMS VIIRS active-fire detections (last 7 days, no API key
needed), keeps the ones inside Indonesia, groups them into fire complexes,
pulls real terrain (AWS Terrain Tiles / SRTM) for the biggest complexes, and
writes a single self-contained HTML dashboard: dashboard.html

Usage:
    pip install numpy pillow
    python build_dashboard.py            # build + open in browser
    python build_dashboard.py --top 8    # 3D terrain for top 8 complexes
"""
import argparse, base64, shutil, csv, io, json, math, os, sys, time, webbrowser
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from PIL import Image

import briefing, haze, places, share_card

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
UA = {"User-Agent": "indo-fire-dashboard/1.0"}

FIRMS = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
SOURCES = {
    "NOAA-20": FIRMS + "noaa-20-viirs-c2/csv/J1_VIIRS_C2_SouthEast_Asia_7d.csv",
    "NOAA-21": FIRMS + "noaa-21-viirs-c2/csv/J2_VIIRS_C2_SouthEast_Asia_7d.csv",
    "S-NPP":   FIRMS + "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_SouthEast_Asia_7d.csv",
}
# mirrors of the same file; raw.githubusercontent.com is blocked on some networks
COUNTRIES = ["https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@master/"
             "geojson/ne_50m_admin_0_countries.geojson",
             "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
             "master/geojson/ne_50m_admin_0_countries.geojson"]
TERRAIN = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

PIXEL_KM2 = 0.375 * 0.375          # VIIRS I-band nominal footprint
CELL_DEG = 0.05                    # ~5.5 km grid used to link detections
FOOT_DEG = 0.375 / 111.0           # ~375 m grid used for footprint area


# ---------------------------------------------------------------- download
def fetch(url, name, max_age_h=3):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600:
        with open(path, "rb") as f:
            return f.read()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with open(path, "wb") as f:
        f.write(data)
    return data


def load_fires():
    rows = []
    for sat, url in SOURCES.items():
        try:
            raw = fetch(url, os.path.basename(url)).decode("utf-8", "replace")
        except Exception as e:
            print(f"  ! {sat}: {e} (skipped)")
            continue
        n = 0
        for r in csv.DictReader(io.StringIO(raw)):
            try:
                t = datetime.strptime(r["acq_date"] + r["acq_time"].zfill(4),
                                      "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc)
                rows.append({
                    "lat": float(r["latitude"]), "lon": float(r["longitude"]),
                    "t": int(t.timestamp()), "frp": float(r.get("frp") or 0),
                    "conf": (r.get("confidence") or "n")[0].lower(),
                    "dn": r.get("daynight", "D"), "sat": sat,
                })
                n += 1
            except (ValueError, KeyError):
                continue
        print(f"  {sat}: {n:,} detections in SE Asia")
    return rows


# ------------------------------------------------------- Indonesia mask
def load_indonesia():
    for i, url in enumerate(COUNTRIES):
        try:
            raw = fetch(url, "countries50m.geojson", max_age_h=24 * 365)
            break
        except Exception as e:
            if i == len(COUNTRIES) - 1:
                raise
            print(f"  ! {url.split('/')[2]}: {e} (trying mirror)")
    gj = json.loads(raw)
    for f in gj["features"]:
        p = f["properties"]
        if p.get("ADM0_A3") == "IDN" or p.get("ISO_A3") == "IDN":
            g = f["geometry"]
            polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
            out = []
            for poly in polys:
                ring = np.array(poly[0])
                out.append((ring[:, 0].min(), ring[:, 0].max(),
                            ring[:, 1].min(), ring[:, 1].max(), ring))
            return out
    raise SystemExit("Indonesia polygon not found")


def in_ring(x, y, ring):
    xs, ys = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(xs, -1), np.roll(ys, -1)
    cond = (ys > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = (x2 - xs) * (y - ys) / (y2 - ys) + xs
    return np.count_nonzero(cond & (x < xi)) % 2 == 1


def in_indonesia(lon, lat, polys, pad=0.03):
    for x0, x1, y0, y1, ring in polys:
        if x0 - pad <= lon <= x1 + pad and y0 - pad <= lat <= y1 + pad:
            if in_ring(lon, lat, ring):
                return True
    return False


def region_of(lat, lon):
    if lon < 106.2 and lat > -6.2:            return "Sumatra"
    if lon < 106.0:                           return "Sumatra"
    if lat < -5.8 and lon < 114.7:            return "Java"
    if 108.5 <= lon <= 119.0 and lat > -4.3:  return "Kalimantan"
    if lat < -7.8 and 114.7 <= lon < 127.5:   return "Nusa Tenggara"
    if 118.7 <= lon <= 125.6 and lat > -6.5:  return "Sulawesi"
    if lon >= 134.0 or (lon >= 130.8 and lat < -0.5): return "Papua"
    if 104.0 <= lon <= 109.5 and lat > -4.3:  return "Riau Islands / Bangka"
    return "Maluku"


# ------------------------------------------------------------ clustering
def cluster(rows):
    cells = defaultdict(list)
    for i, r in enumerate(rows):
        cells[(int(math.floor(r["lat"] / CELL_DEG)), int(math.floor(r["lon"] / CELL_DEG)))].append(i)
    seen, groups = set(), []
    for c in cells:
        if c in seen:
            continue
        stack, members = [c], []
        seen.add(c)
        while stack:
            a, b = stack.pop()
            members.extend(cells[(a, b)])
            for da in (-1, 0, 1):
                for db in (-1, 0, 1):
                    n = (a + da, b + db)
                    if n in cells and n not in seen:
                        seen.add(n)
                        stack.append(n)
        groups.append(members)
    return groups


def complex_stats(rows, idx, t_end):
    pts = [rows[i] for i in idx]
    pts.sort(key=lambda r: r["t"])
    lats = np.array([p["lat"] for p in pts]); lons = np.array([p["lon"] for p in pts])
    clat, clon = float(lats.mean()), float(lons.mean())
    t0 = pts[0]["t"]
    # origin = centroid of first 6 h of detections
    first = [p for p in pts if p["t"] - t0 <= 6 * 3600]
    olat = sum(p["lat"] for p in first) / len(first)
    olon = sum(p["lon"] for p in first) / len(first)
    kx = 111.32 * math.cos(math.radians(olat))

    # daily series
    days = defaultdict(list)
    for p in pts:
        days[datetime.fromtimestamp(p["t"], timezone.utc).strftime("%Y-%m-%d")].append(p)
    foot, series, prev_front = set(), [], None
    for d in sorted(days):
        ds = days[d]
        for p in ds:
            foot.add((int(p["lat"] / FOOT_DEG), int(p["lon"] / FOOT_DEG)))
        dist = [math.hypot((p["lat"] - olat) * 110.57, (p["lon"] - olon) * kx) for p in ds]
        front = float(np.percentile(dist, 95)) if dist else 0.0
        front = max(front, prev_front or 0.0)
        ros = None if prev_front is None else round(front - prev_front, 2)
        prev_front = front
        series.append({
            "day": d, "n": len(ds), "frp": round(sum(p["frp"] for p in ds), 1),
            "maxfrp": round(max(p["frp"] for p in ds), 1),
            "area": round(len(foot) * PIXEL_KM2, 2), "front": round(front, 2), "ros": ros,
        })
    last24 = sum(1 for p in pts if t_end - p["t"] <= 86400)
    return {
        "lat": round(clat, 4), "lon": round(clon, 4),
        "olat": round(olat, 4), "olon": round(olon, 4),
        "region": region_of(clat, clon), "n": len(pts), "last24": last24,
        "frp": round(sum(p["frp"] for p in pts), 1),
        "area": round(len(foot) * PIXEL_KM2, 2),
        "first": t0, "last": pts[-1]["t"],
        "bbox": [float(lons.min()), float(lats.min()), float(lons.max()), float(lats.max())],
        "series": series, "pts": pts,
    }


# --------------------------------------------------------------- terrain
def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def terrain_grid(bbox, size=160):
    lon0, lat0, lon1, lat1 = bbox
    padx = max(0.04, (lon1 - lon0) * 0.15); pady = max(0.04, (lat1 - lat0) * 0.15)
    lon0, lon1, lat0, lat1 = lon0 - padx, lon1 + padx, lat0 - pady, lat1 + pady
    # square-ish in km
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    w, h = (lon1 - lon0) * kx, lat1 - lat0
    if w > h:
        c = (lat0 + lat1) / 2; lat0, lat1 = c - w / 2, c + w / 2
    else:
        c = (lon0 + lon1) / 2; lon0, lon1 = c - h / kx / 2, c + h / kx / 2
    z = 12
    while z > 6:
        x0, y0 = lonlat_to_tile(lon0, lat1, z); x1, y1 = lonlat_to_tile(lon1, lat0, z)
        if (int(x1) - int(x0) + 1) * (int(y1) - int(y0) + 1) <= 16:
            break
        z -= 1
    x0, y0 = lonlat_to_tile(lon0, lat1, z); x1, y1 = lonlat_to_tile(lon1, lat0, z)
    tx0, ty0, tx1, ty1 = int(x0), int(y0), int(x1), int(y1)
    mosaic = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), dtype=np.float32)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            png = fetch(TERRAIN.format(z=z, x=tx, y=ty), f"t_{z}_{tx}_{ty}.png", max_age_h=24 * 365)
            a = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.float32)
            elev = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
            mosaic[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = elev
    grid = np.zeros((size, size), dtype=np.float32)
    for j in range(size):
        lat = lat1 - (lat1 - lat0) * j / (size - 1)
        for i in range(size):
            lon = lon0 + (lon1 - lon0) * i / (size - 1)
            px, py = lonlat_to_tile(lon, lat, z)
            gx = min(max(int((px - tx0) * 256), 0), mosaic.shape[1] - 1)
            gy = min(max(int((py - ty0) * 256), 0), mosaic.shape[0] - 1)
            grid[j, i] = mosaic[gy, gx]
    grid = np.maximum(grid, -5)
    return {"bbox": [lon0, lat0, lon1, lat1], "size": size, "z": z,
            "elev": [int(round(v)) for v in grid.flatten()]}


STORY_BBOX = (95.0, -6.5, 120.5, 8.5)      # Sumatra, Strait of Malacca, Peninsular Malaysia, Borneo


def region_terrain(bbox=STORY_BBOX, nx=520, ny=306, z=6):
    """Equirectangular elevation grid (m, int16) for the 3D story scene, from zoom-6 Terrarium tiles."""
    lon0, lat0, lon1, lat1 = bbox
    x0, y0 = lonlat_to_tile(lon0, lat1, z); x1, y1 = lonlat_to_tile(lon1, lat0, z)
    tx0, ty0, tx1, ty1 = int(x0), int(y0), int(x1), int(y1)
    mosaic = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), dtype=np.float32)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            png = fetch(TERRAIN.format(z=z, x=tx, y=ty), f"t_{z}_{tx}_{ty}.png", max_age_h=24 * 365)
            a = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.float32)
            mosaic[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = \
                a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
    lons = np.linspace(lon0, lon1, nx)
    lats = np.linspace(lat1, lat0, ny)
    px = ((lons + 180) / 360 * 2 ** z - tx0) * 256
    py = ((1 - np.arcsinh(np.tan(np.radians(lats))) / np.pi) / 2 * 2 ** z - ty0) * 256
    gx = np.clip(px.astype(int), 0, mosaic.shape[1] - 1)
    gy = np.clip(py.astype(int), 0, mosaic.shape[0] - 1)
    grid = np.clip(mosaic[np.ix_(gy, gx)], -3000, 5000)
    return {"bbox": list(bbox), "nx": nx, "ny": ny, "elev": b64(np.round(grid).astype("<i2"))}


def sample_elev(ter, lat, lon):
    lon0, lat0, lon1, lat1 = ter["bbox"]; s = ter["size"]
    i = int(round((lon - lon0) / (lon1 - lon0) * (s - 1)))
    j = int(round((lat1 - lat) / (lat1 - lat0) * (s - 1)))
    i = min(max(i, 0), s - 1); j = min(max(j, 0), s - 1)
    return ter["elev"][j * s + i]


# ------------------------------------------------------------------ main
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=6, help="complexes that get 3D terrain")
    ap.add_argument("--min-points", type=int, default=15)
    ap.add_argument("--out", default=os.path.join(HERE, "dashboard.html"))
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-haze", action="store_true", help="skip the Malaysia API outlook")
    a = ap.parse_args()

    print("1/6 Downloading NASA FIRMS VIIRS detections (7 days)…")
    rows = load_fires()
    if not rows:
        sys.exit("No fire data downloaded — check your internet connection / proxy.")
    print("2/6 Keeping detections inside Indonesia…")
    polys = load_indonesia()
    bb = (94.5, -11.5, 141.5, 6.5)
    rows = [r for r in rows if bb[0] <= r["lon"] <= bb[2] and bb[1] <= r["lat"] <= bb[3]
            and r["conf"] != "l" and in_indonesia(r["lon"], r["lat"], polys)]
    print(f"  {len(rows):,} detections (nominal/high confidence) in Indonesia")
    t_end = max(r["t"] for r in rows)

    print("3/6 Grouping into fire complexes…")
    groups = [g for g in cluster(rows) if len(g) >= a.min_points]
    cx = sorted((complex_stats(rows, g, t_end) for g in groups), key=lambda c: -c["frp"])
    for k, c in enumerate(cx):
        c["id"] = k + 1
        c["name"] = f"{c['region']} #{k + 1}"
    print(f"  {len(cx)} complexes with ≥{a.min_points} detections")

    print(f"4/6 Fetching terrain for top {min(a.top, len(cx))} complexes…")
    for c in cx[:a.top]:
        try:
            c["terrain"] = terrain_grid(c["bbox"])
            for p in c["pts"]:
                p["elev"] = sample_elev(c["terrain"], p["lat"], p["lon"])
            print(f"  {c['name']}: zoom {c['terrain']['z']} ok")
        except Exception as e:
            print(f"  ! terrain {c['name']}: {e}")

    # national daily series by region (MYT days, to match the Malaysia side)
    nat = defaultdict(lambda: defaultdict(int))
    for r in rows:
        d = datetime.fromtimestamp(r["t"] + 8 * 3600, timezone.utc).strftime("%Y-%m-%d")
        nat[d][region_of(r["lat"], r["lon"])] += 1

    rows.sort(key=lambda r: r["t"])
    payload = {
        "generated": int(time.time()), "latest": t_end,
        "total": len(rows), "last24": sum(1 for r in rows if t_end - r["t"] <= 86400),
        "daily": {d: dict(v) for d, v in sorted(nat.items())},
        "fires": pack_fires(rows), "land": load_land(), "story_terrain": None,
        "complexes": [], "haze": None, "n_complexes": len(cx),
    }
    keep = set(range(1, 41))
    if not a.no_haze:
        try:
            payload["haze"] = haze.build(fetch, cx, t_end)
            h = payload["haze"]
            keep |= {s["id"] for s in h["sources"]} | {k for k, _ in h["priority"]}
            for st in h["stations"]:
                keep |= {k for k, _ in st["fsrc"] + st["bsrc"]}
            for s in h["sources"]:
                s["place"] = places.nearest_town(s["lat"], s["lon"])
        except Exception as e:
            print(f"  ! Malaysia haze outlook skipped: {e}")
    for c in cx:
        if c["id"] not in keep:
            continue
        o = {k: c[k] for k in ("id", "region", "lat", "lon", "n", "last24", "area", "frp", "first", "last", "bbox")}
        o["place"] = places.nearest_town(c["lat"], c["lon"])
        o["series"] = [{k: d[k] for k in ("day", "n", "frp", "area")} for d in c["series"]]
        if "terrain" in c:
            T = c["terrain"]
            o["terrain"] = {"bbox": T["bbox"], "size": T["size"], "elev": b64(np.array(T["elev"], dtype="<i2"))}
        payload["complexes"].append(o)

    try:
        payload["story_terrain"] = region_terrain()
    except Exception as e:
        print(f"  ! 3D story terrain skipped: {e}")
    payload["briefing"] = briefing.build(payload)
    if payload["briefing"]:
        print(f"  AI briefing written by {payload['briefing']['model']}")

    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    page = (tpl.replace("/*__LEAFLET_CSS__*/", leaflet_css())
               .replace("/*__DATA__*/null", json.dumps(payload, separators=(",", ":"))))
    # share/jerebu-watch.html: page body only (the Artifact host adds the document shell)
    os.makedirs(os.path.join(HERE, "share"), exist_ok=True)
    frag = os.path.join(HERE, "share", "jerebu-watch.html")
    with open(frag, "w", encoding="utf-8") as f:
        f.write(page)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
                "body{margin:0}[hidden]{display:none!important}</style></head><body>\n"
                + page + "\n</body></html>")
    # docs/ is what GitHub Pages serves
    docs = os.path.join(HERE, "docs"); os.makedirs(docs, exist_ok=True)
    with open(a.out, "rb") as src, open(os.path.join(docs, "index.html"), "wb") as dst:
        dst.write(src.read())
    open(os.path.join(docs, ".nojekyll"), "w").close()
    for c in share_card.build(payload, os.path.join(HERE, "share")):
        if c.endswith(".png"):
            shutil.copy(c, os.path.join(docs, os.path.basename(c)))
        print(f"  share card → {c}")
    print(f"\nDone → {a.out}  ({os.path.getsize(a.out) / 1e6:.1f} MB) · share page → {frag}")
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(a.out))


# ------------------------------------------------------------ packing
def b64(arr):
    return base64.b64encode(arr.tobytes()).decode("ascii")


def pack_fires(rows):
    """163k detections as base64 typed arrays (~7 bytes each instead of ~40 in JSON).
    lat = u16*0.0005-12, lon = u16*0.001+94, t = t0 + u16*300 s, frp = (u8/8)^2 MW."""
    t0 = rows[0]["t"]
    lat = np.array([r["lat"] for r in rows]); lon = np.array([r["lon"] for r in rows])
    t = np.array([r["t"] for r in rows]); frp = np.array([r["frp"] for r in rows])
    return {
        "t0": t0, "n": len(rows),
        "lat": b64(np.round((lat + 12) / 0.0005).astype("<u2")),
        "lon": b64(np.round((lon - 94) / 0.001).astype("<u2")),
        "dt": b64(((t - t0) // 300).astype("<u2")),
        "frp": b64(np.clip(np.round(np.sqrt(np.maximum(frp, 0)) * 8), 0, 255).astype("u1")),
    }


LAND_BBOX = (88.0, -14.0, 146.0, 14.0)


def load_land():
    """Country outlines for the region, simplified, so the maps need no tile server."""
    gj = json.loads(fetch(COUNTRIES[0], "countries50m.geojson", max_age_h=24 * 365))
    x0, y0, x1, y1 = LAND_BBOX
    out = []
    for f in gj["features"]:
        a3 = f["properties"].get("ADM0_A3")
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            ring = np.array(poly[0])
            if ring[:, 0].max() < x0 or ring[:, 0].min() > x1 or ring[:, 1].max() < y0 or ring[:, 1].min() > y1:
                continue
            pts, last = [], None
            for lon, lat in np.round(ring, 2):
                p = (round(float(lat), 2), round(float(lon), 2))
                if p != last:
                    pts.append(p); last = p
            if len(pts) >= 4:
                out.append({"k": {"MYS": "my", "IDN": "id"}.get(a3, "x"), "p": pts})
    return out


LEAFLET_CSS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"


def leaflet_css():
    # the Artifact host only allows stylesheets from Google Fonts, so inline Leaflet's
    return fetch(LEAFLET_CSS, "leaflet-1.9.4.css", max_age_h=24 * 365).decode("utf-8")

if __name__ == "__main__":
    main()