"""Portrait share cards (1080x1350) for Threads/Instagram, one per language:
share/card-en.html, share/card-ms.html, and PNGs when Edge or Chrome is installed."""
import math, os, shutil, subprocess, time
from datetime import datetime, timedelta, timezone

MYT = timezone(timedelta(hours=8))
CAT_EN = ["Good", "Moderate", "Unhealthy", "Very unhealthy", "Hazardous"]
CAT_MS = ["Baik", "Sederhana", "Tidak sihat", "Sangat tidak sihat", "Berbahaya"]
CAT_COL = ["#4f97e3", "#4bae59", "#efbd2b", "#ef812d", "#dc4943"]
MONTHS_MS = ["Jan", "Feb", "Mac", "Apr", "Mei", "Jun", "Jul", "Ogos", "Sep", "Okt", "Nov", "Dis"]
DAYS_MS = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
BOX = (99.3, 0.6, 119.5, 7.6)          # lon0, lat0, lon1, lat1 shown on the card map


def cat(a):
    return next(i for i, c in enumerate([50, 100, 200, 300, 10 ** 9]) if a <= c)


def model(payload):
    h = payload["haze"]
    t0 = h["t_now"]
    d0 = datetime.fromtimestamp(t0, MYT).replace(hour=0, minute=0, second=0)
    day0 = int(d0.timestamp())
    st = []
    for s in h["stations"]:
        obs = dict((t, a) for t, a in s["obs"])
        fc = {t: c for t, r, c in (s["fc"]["series"] if s["fc"] else []) if t > t0}
        now = obs.get(t0, s["api"])
        days = []
        for k in range(3):
            a, b = day0 + k * 86400, day0 + (k + 1) * 86400
            vals = [v for t, v in obs.items() if a <= t < b and t <= t0] + [v for t, v in fc.items() if a <= t < b]
            days.append(max(vals) if vals else None)
        st.append({"area": s["area"], "state": s["state"], "lat": s["lat"], "lon": s["lon"], "now": now, "days": days})
    cx = {c["id"]: c for c in payload["complexes"]}
    prov = {}
    for cid, f in h["priority"]:
        if cid in cx:
            pl = cx[cid]["place"]
            prov.setdefault(pl[1], [pl, 0.0])[1] += f
    provs = sorted(prov.values(), key=lambda x: -x[1])[:3]
    return st, provs, t0


def map_svg(land, st, w, h):
    lon0, lat0, lon1, lat1 = BOX
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    sx = w / ((lon1 - lon0) * kx)
    sy = sx
    oy = (h - (lat1 - lat0) * sy) / 2

    def P(lat, lon):
        return (lon - lon0) * kx * sx, oy + (lat1 - lat) * sy

    paths = []
    fills = {"my": "#ffffff", "id": "#e6e1d9", "x": "#eef1f3"}
    for poly in land:
        pts = poly["p"]
        if not any(lon0 - 2 < lo < lon1 + 2 and lat0 - 2 < la < lat1 + 2 for la, lo in pts[::5] or pts):
            continue
        d = "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in (P(la, lo) for la, lo in pts)) + "Z"
        paths.append(f'<path d="{d}" fill="{fills[poly["k"]]}" stroke="#b6c1ca" stroke-width="1"/>')
    dots = []
    for s in sorted(st, key=lambda s: s["now"] or 0):
        x, y = P(s["lat"], s["lon"])
        if s["now"] is None:
            continue
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="{CAT_COL[cat(s["now"])]}" stroke="#fff" stroke-width="2.5"/>')
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{w}" height="{h}" fill="#dce4ea"/>{"".join(paths)}{"".join(dots)}</svg>')


def card_html(payload, lang):
    st, provs, t0 = model(payload)
    ms = lang == "ms"
    CAT = CAT_MS if ms else CAT_EN
    when = datetime.fromtimestamp(t0, MYT)
    if ms:
        stamp = f"{DAYS_MS[when.weekday()]}, {when.day} {MONTHS_MS[when.month - 1]} {when.year} · {when.strftime('%I:%M %p').lstrip('0')}"
    else:
        stamp = f"{when.strftime('%A')}, {when.day} {when.strftime('%b %Y')} · {when.strftime('%I:%M %p').lstrip('0')}"
    n = sum(1 for s in st if (s["now"] or 0) > 100)
    worst = sorted(st, key=lambda s: -(s["now"] or 0))[:5]
    dcount = [sum(1 for s in st if (s["days"][k] or 0) > 100) for k in range(3)]
    dnames = ["Hari ini", "Esok"] if ms else ["Today", "Tomorrow"]
    d2 = when + timedelta(days=2)
    dnames.append(DAYS_MS[d2.weekday()] if ms else d2.strftime("%A"))
    pnames = [p[0][2] if ms else p[0][1] for p in provs]
    join = (" dan " if ms else " and ")
    src = (", ".join(pnames[:-1]) + join + pnames[-1]) if len(pnames) > 1 else "".join(pnames)
    rows = "".join(
        f'<div class="w"><span class="n">{s["area"]}<small>{s["state"].replace("WP ", "W.P. ")}</small></span>'
        f'<b style="background:{CAT_COL[cat(s["now"])]}">{s["now"]}</b></div>' for s in worst)
    days = "".join(
        f'<div class="d"><span>{dnames[k]}</span><b>{dcount[k]}</b><small>{"kawasan tidak sihat" if ms else "areas unhealthy"}</small></div>'
        for k in range(3))
    T = {
        "of": "daripada {} kawasan" if ms else "of {} areas",
        "head": "di Malaysia mencatat udara tidak sihat" if ms else "in Malaysia have unhealthy air right now",
        "worst": "Paling teruk sekarang" if ms else "Worst right now",
        "next": "3 hari akan datang" if ms else "Next 3 days",
        "src": (f"Asap kebanyakannya dari kebakaran di <b>{src}</b>, Indonesia." if ms
                else f"Smoke mainly from fires in <b>{src}</b>, Indonesia."),
        "tip": ("Pakai pelitup muka di luar · Kurangkan aktiviti luar · Minum banyak air" if ms
                else "Wear a mask outdoors · Cut down outdoor activity · Drink plenty of water"),
        "foot": ("Ramalan tidak rasmi. Bacaan rasmi: APIMS JAS. Data: NASA FIRMS, JAS, Copernicus CAMS." if ms
                 else "Unofficial forecast. Official readings: DOE APIMS. Data: NASA FIRMS, DOE, Copernicus CAMS."),
        "api": "IPU" if ms else "API",
    }
    scale = "".join(f'<span><i style="background:{c}"></i>{CAT[i]}</span>' for i, c in enumerate(CAT_COL))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=IBM+Plex+Mono:wght@500&display=swap">
<style>
*{{box-sizing:border-box}}body{{margin:0;width:1080px;height:1350px;background:#e8ecef;color:#17202a;font:28px/1.35 "Atkinson Hyperlegible","Segoe UI",sans-serif}}
.c{{position:absolute;inset:0;padding:52px 68px 46px;display:flex;flex-direction:column;gap:22px}}
.top{{display:flex;justify-content:space-between;align-items:center;font:500 22px "IBM Plex Mono",monospace;letter-spacing:.06em;text-transform:uppercase;color:#596572}}
.brand{{display:flex;align-items:center;gap:14px;font:800 32px "Bricolage Grotesque",sans-serif;letter-spacing:-.02em;text-transform:none;color:#17202a}}
.hero{{display:flex;align-items:flex-end;gap:28px}}
.big{{font:800 200px/.8 "Bricolage Grotesque",sans-serif;letter-spacing:-.05em;color:#ef812d}}
.hero h1{{margin:0 0 14px;font:800 54px/1.05 "Bricolage Grotesque",sans-serif;letter-spacing:-.02em}}
.hero h1 small{{display:block;font:700 30px "Atkinson Hyperlegible",sans-serif;color:#596572;margin-bottom:8px}}
.mid{{display:grid;grid-template-columns:520px 1fr;gap:30px}}
.map{{border-radius:22px;overflow:hidden;box-shadow:0 2px 0 #0000000d}}
.list h2,.days h2{{margin:0 0 12px;font:700 26px "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.06em;color:#596572}}
.w{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:2px solid #d2d9df}}
.w .n{{font-weight:700;font-size:31px;line-height:1.1}}.w small{{display:block;font-weight:400;font-size:21px;color:#596572}}
.w b{{font:800 32px "Bricolage Grotesque",sans-serif;border-radius:12px;padding:4px 14px;min-width:92px;text-align:center}}
.dd{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}
.d{{background:#fff;border-radius:18px;padding:14px 20px}}.d span{{font-weight:700;font-size:26px}}.d b{{display:block;font:800 62px/1 "Bricolage Grotesque",sans-serif;color:#ef812d;margin-top:4px}}.d small{{font-size:21px;color:#596572}}
.src{{font-size:30px}}.tip{{background:#efbd2b;border-radius:18px;padding:18px 24px;font-weight:700;font-size:26px}}
.scale{{display:flex;gap:18px;font-size:20px;color:#596572;flex-wrap:wrap}}.scale i{{display:inline-block;width:18px;height:18px;border-radius:50%;margin-right:7px;vertical-align:-2px}}
.foot{{margin-top:auto;font-size:21px;color:#17202a;font-weight:700}}
</style></head><body><div class="c">
<div class="top"><div class="brand"><svg width="44" height="44" viewBox="0 0 30 30"><circle cx="15" cy="13" r="8" fill="#efbd2b"/><rect x="3" y="15" width="24" height="3.2" rx="1.6" fill="#596572" opacity=".75"/><rect x="6" y="20.5" width="19" height="3.2" rx="1.6" fill="#596572" opacity=".55"/><rect x="9" y="26" width="12" height="3" rx="1.5" fill="#596572" opacity=".35"/></svg>Jerebu Watch</div><div>{stamp}</div></div>
<div class="hero"><div class="big">{n}</div><h1><small>{T['of'].format(len(st))}</small>{T['head']}</h1></div>
<div class="mid"><div class="map">{map_svg(payload['land'], st, 520, 400)}</div>
<div class="list"><h2>{T['worst']}</h2>{rows}</div></div>
<div class="days"><h2>{T['next']}</h2><div class="dd">{days}</div></div>
<div class="src">{T['src']}</div>
<div class="tip">{T['tip']}</div>
<div class="scale">{scale}</div>
<div class="foot">{T['foot']}</div>
</div></body></html>"""


def browser():
    for p in [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
              os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
              os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
              shutil.which("chromium") or "", shutil.which("google-chrome") or ""]:
        if p and os.path.exists(p):
            return p
    return None


def build(payload, outdir):
    if not payload.get("haze"):
        return []
    outdir = os.path.abspath(outdir)
    made = []
    exe = browser()
    for lang in ("en", "ms"):
        html_path = os.path.abspath(os.path.join(outdir, f"card-{lang}.html"))
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(card_html(payload, lang))
        made.append(html_path)
        if exe:
            png = os.path.join(outdir, f"card-{lang}.png")
            prof = os.path.join(outdir, ".browser-profile")
            t_start = time.time() - 1
            try:
                subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={prof}",
                                "--window-size=1080,1350", "--virtual-time-budget=8000", f"--screenshot={png}",
                                "file:///" + html_path.replace("\\", "/")], timeout=90, capture_output=True)
                for _ in range(60):          # Edge can hand off to a running instance and return early
                    if os.path.exists(png) and os.path.getmtime(png) >= t_start:
                        made.append(png)
                        break
                    time.sleep(0.5)
            except Exception as e:
                print(f"  ! card screenshot: {e}")
    return made
