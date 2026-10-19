# Jerebu Watch — Indonesia fires → Malaysia haze

Public site, built in one command. Each build writes:
- `dashboard.html` — the full site, one self-contained file (open it, or upload it to any static host
  such as GitHub Pages or Netlify Drop as `index.html`)
- `share/jerebu-watch.html` — the same page without the document shell, for publishing as a Claude Artifact
- `share/card-en.png`, `share/card-ms.png` — 1080×1350 share images for Threads / Instagram

Tabs: **Today** (national picture) · **Your area** (one station, 3-day outlook, advice, smoke sources) ·
**Smoke** (animated plume forecast) · **Fires** (hotspot time-lapse, 3D terrain) · **About**. English / Bahasa Melayu.
Maps draw from embedded coastlines, so they work without a tile server.
The UI follows the Elevate design system (`ai-elevate/frontend/DESIGN.md`): one map, a frosted-glass
side panel, a command deck on the map, Inter + Newsreader, one lavender accent.

**Optional AI briefing (DeepSeek).** Set the key before building and DeepSeek writes the Today briefing
in English and Bahasa Melayu from the page's own numbers (`briefing.py`). The key never leaves the
build machine; replies that mention any number not in the data are rejected. Without a key the page
uses its rule-based summary.

```powershell
$env:DEEPSEEK_API_KEY = "..."      # optional: $env:DEEPSEEK_MODEL = "deepseek-v4-flash"
python build_dashboard.py
```

---

# Technical notes

Builds a single `dashboard.html` from **live NASA FIRMS satellite fire data** (last 7 days):
national map with time-lapse, ranked fire complexes, 3D terrain spread replay,
burn footprint, rate of spread, fire power and elevation charts — plus a
**Malaysia haze outlook** that links those fires to Malaysia's Air Pollutant Index (API / IPU).

## Run (VS Code terminal)
```bash
pip install numpy pillow
python build_dashboard.py
```
The dashboard opens in your browser. Re-run any time to refresh (fire data cached 3 h, APIMS 1 h, forecasts 3 h, in `cache/`).

Options: `--top 10` (3D terrain for more complexes), `--min-points 30`, `--no-open`, `--no-haze` (skip the Malaysia section).

## Malaysia haze outlook
- **Observed** — DOE APIMS hourly API, all 68 stations, last 7 days.
- **Forecast (0–72 h+)** — CAMS global PM2.5 (Copernicus, via Open-Meteo) converted to API with the
  DOE PM2.5 sub-index formula, scaled by each station's observed/model ratio over the last 72 h, and
  blended with the latest reading (persistence weight e^(−lead/48 h)). Each station shows its own
  backtest: a forecast issued 72 h ago, scored against what was then observed. Across all stations on
  the first run: ~11 API points mean error vs 16.5 for persistence and 28 for raw CAMS.
- **Which fires** — 925/850 hPa wind trajectories (Open-Meteo) run forward 72 h from the 40 most
  active fire complexes, and back 48 h from every station. *Priority fires* = share of forecast plume
  reaching DOE stations — the fires whose suppression most protects Malaysia.
- **What to do** — DOE "Indeks Pencemar Udara" activity advice for the worst of now / 72 h forecast,
  and DOE "Nasihat Semasa Jerebu" when API > 100.

Limits: trajectories are single-level kinematic paths (no dispersion, rain-out or plume height), and the
fire complexes assume today's burning continues for 48 h. Treat source shares as relative, not absolute.

## Data sources (all free, no API key)
- NASA FIRMS VIIRS NOAA-20 / NOAA-21 / S-NPP — firms.modaps.eosdis.nasa.gov
- DOE Malaysia APIMS — eqms.doe.gov.my (rate-limited; the build fetches one request at a time)
- CAMS PM2.5 forecast — air-quality-api.open-meteo.com
- Wind forecast — api.open-meteo.com
- Natural Earth country boundaries — cdn.jsdelivr.net (raw.githubusercontent.com as fallback)
- AWS Terrain Tiles (SRTM) — s3.amazonaws.com/elevation-tiles-prod
- Basemaps — server.arcgisonline.com (Esri)

If you're on the office network and downloads fail, those hosts must be reachable.

## Refresh (optional)
Windows Task Scheduler or cron, e.g. hourly: `python build_dashboard.py --no-open`
