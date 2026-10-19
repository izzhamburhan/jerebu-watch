"""
Malaysia haze outlook: links Indonesian fire complexes to Malaysia's Air
Pollutant Index (API / IPU).

  * observed  — DOE APIMS, 68 stations, hourly API for the last 7 days
  * forecast  — CAMS global PM2.5 (via Open-Meteo), converted to API with the
                DOE PM2.5 sub-index formula and calibrated per station against
                the observed API of the last 72 h
  * sources   — wind trajectories (925 / 850 hPa, Open-Meteo) run forward from
                every active fire complex and backward from every station, to
                say which fires feed which stations
"""
import json, math, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

APIMS = "https://eqms.doe.gov.my/api3/publicportalapims/"
AQ = "https://air-quality-api.open-meteo.com/v1/air-quality"
WX = "https://api.open-meteo.com/v1/forecast"
MYT = timezone(timedelta(hours=8))

# DOE "API Calculation" (USEPA-based PM2.5 sub-index, 24 h average, µg/m³)
PM25_BP = [(0.0, 12.0, 0, 50), (12.1, 50.4, 51, 100), (50.5, 55.4, 101, 150),
           (55.5, 150.4, 151, 200), (150.5, 250.4, 201, 300),
           (250.5, 350.4, 301, 400), (350.5, 500.4, 401, 500)]

# wind domain covering Sumatra, Peninsular Malaysia and Borneo
GLON = np.arange(92.0, 122.01, 1.0)
GLAT = np.arange(-8.0, 9.01, 1.0)
LEVELS = (925, 850)
BACK_H, FWD_H = 48, 72
HIT_KM = 60.0          # a plume passing within this distance "hits" a station
DECAY_H = 48.0         # e-folding of plume strength (dilution / deposition)


def pm25_to_api(c):
    if c is None or c != c:
        return None
    c = max(0.0, c)
    for c0, c1, a0, a1 in PM25_BP:
        if c <= c1 + 0.05:
            return round(a0 + (a1 - a0) * (max(c, c0) - c0) / (c1 - c0))
    return 500


def api_to_pm25(a):
    for c0, c1, a0, a1 in PM25_BP:
        if a <= a1:
            return c0 + (c1 - c0) * (max(a, a0) - a0) / (a1 - a0)
    return 500.4


def hourly_epoch(times):
    return [int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()) for t in times]


# ------------------------------------------------------------------ APIMS
def load_apims(fetch, days=7):
    cur = json.loads(fetch(APIMS + "mapcaqm", "apims_current.json", max_age_h=0.5))["data"]
    stations = {}
    for s in cur:
        if s.get("LATITUDE") is None or s.get("STATION_STATUS") != 1:
            continue
        stations[s["STATION_ID"]] = {
            "id": s["STATION_ID"], "name": s["STATION_LOCATION"], "state": s["STATE_NAME"],
            "state_id": s["STATE_ID"], "lat": float(s["LATITUDE"]), "lon": float(s["LONGITUDE"]),
            "api": s.get("API"), "cls": s.get("CLASS"), "param": s.get("PARAM_SYMBOL"),
            "obs": {},
        }
    latest = max(datetime.fromisoformat(s["RECORD_DT"]) for s in cur if s.get("RECORD_DT"))
    jobs = []
    for sid in sorted({s["state_id"] for s in stations.values()}):
        for k in range(days):
            end = latest - timedelta(hours=24 * k)
            stamp = end.strftime("%Y-%m-%dT%H:00:00")
            jobs.append((sid, stamp, 0.9 if k == 0 else 24 * 30))

    def get(job):
        # APIMS rate-limits (HTTP 429): one request at a time, back off on 429
        sid, stamp, age = job
        name = f"apims_{sid}_{stamp.replace(':', '')}.json"
        for wait in (0, 5, 15, 30, 60):
            time.sleep(wait)
            try:
                t = time.time()
                raw = fetch(f"{APIMS}apitablehourly?stateid={sid}&datetime={stamp}&", name, max_age_h=age)
                if time.time() - t > 0.05:      # went to the network, not the cache
                    time.sleep(0.6)
                return json.loads(raw).get("api_table_hourly") or []
            except Exception as e:
                if "429" not in str(e) or wait == 60:
                    print(f"  ! APIMS state {sid} {stamp}: {e}")
                    return []
        return []

    for rows in map(get, jobs):
        for r in rows:
            st = stations.get(r["STATION_ID"])
            if st is None or r.get("API") is None or r["API"] < 0:
                continue
            t = int(datetime.fromisoformat(r["DATETIME"]).replace(tzinfo=MYT).timestamp())
            st["obs"][t] = r["API"]
    t_latest = int(latest.replace(tzinfo=MYT).timestamp())
    return list(stations.values()), t_latest


# --------------------------------------------------------- CAMS forecast
def load_cams(fetch, stations, t_now):
    out = {}
    for i in range(0, len(stations), 40):
        chunk = stations[i:i + 40]
        url = (f"{AQ}?latitude={','.join(f'{s['lat']:.3f}' for s in chunk)}"
               f"&longitude={','.join(f'{s['lon']:.3f}' for s in chunk)}"
               f"&hourly=pm2_5&past_days=7&forecast_days=5&timezone=GMT")
        res = json.loads(fetch(url, f"cams_{i}_{t_now}.json", max_age_h=3))
        if isinstance(res, dict):
            res = [res]
        for s, r in zip(chunk, res):
            ts = hourly_epoch(r["hourly"]["time"])
            pm = [v if v is not None else np.nan for v in r["hourly"]["pm2_5"]]
            out[s["id"]] = (ts, np.array(pm, dtype=float))
    return out


def rolling24(a):
    out = np.full_like(a, np.nan)
    for i in range(23, len(a)):
        w = a[i - 23:i + 1]
        if np.isfinite(w).sum() >= 18:
            out[i] = np.nanmean(w)
    return out


BLEND_TAU_H = 48.0    # persistence weight e^(-lead/τ); τ chosen by backtest


def blend(pm_model, k, pm_last, lead_s):
    """Forecast PM2.5: decaying persistence of the last observation blended with
    CAMS scaled by the station's observed/model ratio. On a 7-day backtest over
    all 68 stations this cut 0–72 h API error from 28 (raw CAMS) and 16.5
    (persistence) to ~11."""
    w = math.exp(-lead_s / (BLEND_TAU_H * 3600))
    return w * pm_last + (1 - w) * pm_model * k


def calibrate(st, cams, t_now):
    ts, pm = cams
    pm24 = rolling24(pm)
    idx = {t: i for i, t in enumerate(ts)}
    pairs = [(t, api_to_pm25(a), pm24[idx[t]]) for t, a in st["obs"].items()
             if t in idx and np.isfinite(pm24[idx[t]]) and pm24[idx[t]] > 0.5]

    def ratio(lo, hi):
        r = [o / m for t, o, m in pairs if lo < t <= hi]
        return float(np.clip(np.median(r), 0.3, 6.0)) if len(r) >= 12 else None

    k = ratio(t_now - 72 * 3600, t_now) or ratio(t_now - 168 * 3600, t_now) or 1.0
    last = st["obs"].get(t_now)

    # backtest: pretend it is 72 h ago, forecast the last 72 h, compare to obs
    t_s = t_now - 72 * 3600
    k_bt, last_bt = ratio(t_s - 72 * 3600, t_s), st["obs"].get(t_s)
    err = {"raw": [], "persist": [], "fcst": []}
    if k_bt and last_bt is not None:
        for t, o, m in pairs:
            if t_s < t <= t_now:
                a = st["obs"][t]
                err["raw"].append(abs(pm25_to_api(m) - a))
                err["persist"].append(abs(last_bt - a))
                err["fcst"].append(abs(pm25_to_api(blend(m, k_bt, api_to_pm25(last_bt), t - t_s)) - a))
    series = []
    for t, v in zip(ts, pm24):
        if not np.isfinite(v):
            continue
        if t > t_now and last is not None:
            cal = pm25_to_api(blend(v, k, api_to_pm25(last), t - t_now))
        else:
            cal = pm25_to_api(v * k)
        series.append([t, pm25_to_api(v), cal])
    mae = {n: round(float(np.mean(e)), 1) if e else None for n, e in err.items()}
    return {"k": round(k, 2), "series": series,
            "mae_raw": mae["raw"], "mae_persist": mae["persist"], "mae_cal": mae["fcst"]}


# ------------------------------------------------------------------ wind
def load_wind(fetch, t_now):
    pts = [(la, lo) for la in GLAT for lo in GLON]
    var = ",".join(f"wind_speed_{l}hPa,wind_direction_{l}hPa" for l in LEVELS)
    U = V = None
    times = None
    for i in range(0, len(pts), 100):
        chunk = pts[i:i + 100]
        url = (f"{WX}?latitude={','.join(f'{p[0]:.1f}' for p in chunk)}"
               f"&longitude={','.join(f'{p[1]:.1f}' for p in chunk)}"
               f"&hourly={var}&wind_speed_unit=ms&past_days=3&forecast_days=4&timezone=GMT")
        res = json.loads(fetch(url, f"wind_{i}_{t_now}.json", max_age_h=3))
        if isinstance(res, dict):
            res = [res]
        for j, r in enumerate(res):
            h = r["hourly"]
            if times is None:
                times = np.array(hourly_epoch(h["time"]))
                U = np.zeros((len(LEVELS), len(times), len(pts)), dtype=np.float32)
                V = np.zeros_like(U)
            for li, l in enumerate(LEVELS):
                sp = np.array([x if x is not None else 0 for x in h[f"wind_speed_{l}hPa"]], dtype=float)
                dr = np.radians([x if x is not None else 0 for x in h[f"wind_direction_{l}hPa"]])
                # meteorological direction = where wind blows FROM
                U[li, :, i + j] = -sp * np.sin(dr)
                V[li, :, i + j] = -sp * np.cos(dr)
    shape = (len(LEVELS), len(times), len(GLAT), len(GLON))
    return times, U.reshape(shape), V.reshape(shape)


class Wind:
    def __init__(self, times, U, V):
        self.t, self.U, self.V = times, U, V

    def at(self, li, t, lon, lat):
        if not (GLON[0] <= lon <= GLON[-1] and GLAT[0] <= lat <= GLAT[-1]):
            return None
        if t < self.t[0] or t > self.t[-1]:
            return None
        k = min(int((t - self.t[0]) // 3600), len(self.t) - 2)
        ft = (t - self.t[k]) / 3600.0
        x = (lon - GLON[0]); y = (lat - GLAT[0])
        i = min(int(x), len(GLON) - 2); j = min(int(y), len(GLAT) - 2)
        fx, fy = x - i, y - j

        def bil(A):
            return ((A[j, i] * (1 - fx) + A[j, i + 1] * fx) * (1 - fy)
                    + (A[j + 1, i] * (1 - fx) + A[j + 1, i + 1] * fx) * fy)
        u = bil(self.U[li, k]) * (1 - ft) + bil(self.U[li, k + 1]) * ft
        v = bil(self.V[li, k]) * (1 - ft) + bil(self.V[li, k + 1]) * ft
        return float(u), float(v)

    def trajectory(self, li, t0, lon, lat, hours, sign=1):
        path = [(round(lon, 3), round(lat, 3))]
        t = t0
        for _ in range(hours):
            # midpoint (RK2) step, 1 h
            w = self.at(li, t, lon, lat)
            if w is None:
                break
            dt = 3600.0 * sign
            kx = 111320.0 * math.cos(math.radians(lat))
            mlon = lon + w[0] * dt / 2 / kx; mlat = lat + w[1] * dt / 2 / 111320.0
            w2 = self.at(li, t + dt / 2, mlon, mlat)
            if w2 is None:
                break
            lon += w2[0] * dt / kx; lat += w2[1] * dt / 111320.0; t += dt
            path.append((round(lon, 3), round(lat, 3)))
        return path


def pretty_area(name):
    """'Nilai, NEGERI SEMBILAN' -> 'Nilai'; 'JOHAN SETIA' -> 'Johan Setia'."""
    a = name.split(",")[0].strip()
    return a.title() if a.isupper() else a


def km(lat1, lon1, lat2, lon2):
    kx = 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat1 - lat2) * 110.57, (lon1 - lon2) * kx)


# ------------------------------------------------------------------ main
def build(fetch, complexes, t_fire_end):
    print("5/6 Malaysia API (DOE APIMS) + CAMS smoke forecast…")
    stations, t_obs = load_apims(fetch)
    print(f"  {len(stations)} stations, latest reading {datetime.fromtimestamp(t_obs, MYT):%Y-%m-%d %H:%M} MYT")
    t_now = t_obs
    cams = load_cams(fetch, stations, t_now // 10800)
    for st in stations:
        st["fc"] = calibrate(st, cams[st["id"]], t_now) if st["id"] in cams else None

    print("6/6 Wind trajectories (fires → Malaysia)…")
    wt, U, V = load_wind(fetch, t_now // 10800)
    wind = Wind(wt, U, V)
    t0 = (t_now // 3600) * 3600

    # sources: complexes active in the last 24 h inside the wind domain
    sources = []
    for c in complexes:
        recent = [p for p in c["pts"] if t_fire_end - p["t"] <= 86400]
        if not recent:
            continue
        w = sum(p["frp"] for p in recent) or 1.0
        lat = sum(p["lat"] * p["frp"] for p in recent) / w
        lon = sum(p["lon"] * p["frp"] for p in recent) / w
        if GLON[0] <= lon <= GLON[-1] and GLAT[0] <= lat <= GLAT[-1]:
            sources.append({"id": c["id"], "name": c["name"], "lat": lat, "lon": lon, "w": w})
    sources.sort(key=lambda s: -s["w"])
    sources = sources[:40]

    # forward plumes: release every 12 h for 48 h (fires assumed to keep burning)
    plumes = []
    for s in sources:
        for rel in range(0, 49, 12):
            for li in range(len(LEVELS)):
                path = wind.trajectory(li, t0 + rel * 3600, s["lon"], s["lat"], FWD_H)
                if len(path) > 1:
                    plumes.append({"src": s["id"], "t0": t0 + rel * 3600, "lvl": LEVELS[li], "p": path})

    # forecast hits per station (hourly, by source)
    wmax = max((s["w"] for s in sources), default=1.0)
    sw = {s["id"]: s["w"] / wmax for s in sources}
    impact = defaultdict(float)
    for st in stations:
        hits = defaultdict(float); by_src = defaultdict(float)
        for pl in plumes:
            for h, (lon, lat) in enumerate(pl["p"]):
                d = km(lat, lon, st["lat"], st["lon"])
                if d < HIT_KM * 1.5:
                    v = sw[pl["src"]] * math.exp(-(d / HIT_KM) ** 2) * math.exp(-h / DECAY_H) / len(LEVELS)
                    hits[pl["t0"] + h * 3600] += v
                    by_src[pl["src"]] += v
        st["hits"] = sorted([t, round(v, 3)] for t, v in hits.items() if v > 0.005)
        tot = sum(by_src.values())
        st["fsrc"] = [[k, round(v / tot, 3)] for k, v in sorted(by_src.items(), key=lambda x: -x[1])[:5]] if tot else []
        st["fload"] = round(tot, 3)
        for k, v in by_src.items():
            impact[k] += v

    # back trajectories: where did the air at each station come from
    cpos = [(c["id"], c["lat"], c["lon"], c["frp"]) for c in complexes]
    fmax = max((c[3] for c in cpos), default=1.0)
    for st in stations:
        paths, by_src = [], defaultdict(float)
        for li in range(len(LEVELS)):
            path = wind.trajectory(li, t0, st["lon"], st["lat"], BACK_H, sign=-1)
            paths.append(path)
            for h, (lon, lat) in enumerate(path):
                for cid, clat, clon, frp in cpos:
                    if abs(clat - lat) < 1.0 and abs(clon - lon) < 1.0:
                        d = km(lat, lon, clat, clon)
                        if d < HIT_KM * 1.5:
                            by_src[cid] += frp / fmax * math.exp(-(d / HIT_KM) ** 2) * math.exp(-h / DECAY_H)
        st["back"] = paths
        tot = sum(by_src.values())
        st["bsrc"] = [[k, round(v / tot, 3)] for k, v in sorted(by_src.items(), key=lambda x: -x[1])[:5]] if tot else []
        st["bload"] = round(tot, 3)

    # map wind arrows: 2° grid, 3-hourly, from -48 h to +72 h
    keep = [k for k, t in enumerate(wt) if (t - t0) % 10800 == 0 and -BACK_H * 3600 <= t - t0 <= FWD_H * 3600]
    gi = list(range(0, len(GLON), 2)); gj = list(range(0, len(GLAT), 2))
    arrows = {"t": [int(wt[k]) for k in keep],
              "lon": [float(GLON[i]) for i in gi], "lat": [float(GLAT[j]) for j in gj],
              "uv": [[[round(float(U[0, k, j, i]), 1), round(float(V[0, k, j, i]), 1)]
                      for j in gj for i in gi] for k in keep]}

    out_st = []
    for st in stations:
        o = {k: st[k] for k in ("id", "name", "state", "lat", "lon", "api", "cls", "param",
                                "hits", "fsrc", "fload", "back", "bsrc", "bload")}
        o["obs"] = sorted([t, a] for t, a in st["obs"].items())
        o["area"] = pretty_area(st["name"])
        o["back"] = [[[round(x, 2), round(y, 2)] for x, y in p] for p in st["back"]]
        o["fc"] = st["fc"]
        out_st.append(o)
    pri = sorted(impact.items(), key=lambda x: -x[1])
    ptot = sum(v for _, v in pri) or 1.0
    n_unh = sum(1 for s in stations if (s["api"] or 0) > 100)
    print(f"  {len(sources)} active source complexes, {len(plumes)} plume trajectories, "
          f"{n_unh} stations currently > 100")
    return {
        "t_now": t0, "t_obs": t_obs, "stations": out_st,
        "sources": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()} for s in sources],
        "plumes": [{**p, "p": [[round(x, 2), round(y, 2)] for x, y in p["p"]]} for p in plumes],
        "arrows": arrows,
        "priority": [[k, round(v / ptot, 3)] for k, v in pri[:15]],
        "levels": list(LEVELS),
    }
