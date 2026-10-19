"""Optional AI briefing, written by DeepSeek at build time.

Runs only when DEEPSEEK_API_KEY is set. The key stays on the machine that builds the page;
only the finished text is embedded. The model sees the page's own numbers and nothing else,
and any reply that contains a number not in those facts is rejected (the page then falls
back to its rule-based summary).

    set DEEPSEEK_API_KEY=...            (PowerShell: $env:DEEPSEEK_API_KEY="...")
    set DEEPSEEK_MODEL=deepseek-v4-flash   (optional)
"""
import json, os, re, urllib.error, urllib.request
from datetime import datetime

from share_card import MYT, model

API = "https://api.deepseek.com/chat/completions"
SYSTEM = (
    "You write the daily haze briefing for a public website in Malaysia. Use only the facts in the "
    "user's JSON. Do not add numbers, places, causes, health claims or advice that are not in the "
    "facts. Three short sentences at most, plain words, no exclamation marks, no emoji, no hashtags. "
    "Say what the air is like now, what the next three days look like, and where the smoke comes "
    "from. Write one version in English and one in Bahasa Melayu (Malaysian, not Indonesian). "
    'Reply with JSON only: {"en": "...", "ms": "..."}'
)


def facts(payload):
    st, provs, t0 = model(payload)
    worst = sorted(st, key=lambda s: -(s["now"] or 0))[:5]
    days = [sum(1 for s in st if (s["days"][k] or 0) > 100) for k in range(3)]
    states = {}
    for s in st:
        if (s["now"] or 0) > 100:
            states[s["state"]] = states.get(s["state"], 0) + 1
    return {
        "reading_time_MYT": datetime.fromtimestamp(t0, MYT).strftime("%A %d %B %Y, %I:%M %p"),
        "stations_total": len(st),
        "stations_unhealthy_now (API above 100)": sum(1 for s in st if (s["now"] or 0) > 100),
        "worst_now": [{"area": s["area"], "state": s["state"], "api": s["now"]} for s in worst],
        "states_with_unhealthy_stations": states,
        "stations_forecast_unhealthy": {"today": days[0], "tomorrow": days[1], "day_after": days[2]},
        "smoke_sources_share_percent": [{"province_en": p[0][1], "province_ms": p[0][2], "share": round(p[1] * 100)}
                                        for p in provs],
        "fire_hotspots_last_24h_indonesia": payload["last24"],
        "api_categories": "0-50 good, 51-100 moderate, 101-200 unhealthy, 201-300 very unhealthy, above 300 hazardous",
        "official_advice_when_unhealthy": "cut down long outdoor activities; people with asthma, heart or "
                                          "breathing problems should stay indoors; wear a mask outside",
    }


def numbers_ok(text, f):
    allowed = set(re.findall(r"\d+", json.dumps(f)))
    allowed |= {"1", "2", "3", "24", "48", "72"}
    return all(n in allowed for n in re.findall(r"\d+", text.replace(",", "")))


def _post(key, body):
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def build(payload):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key or not payload.get("haze"):
        return None
    f = facts(payload)
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(f, ensure_ascii=False)}]
    names = [m for m in (os.environ.get("DEEPSEEK_MODEL"), "deepseek-v4-flash", "deepseek-chat") if m]
    for name in dict.fromkeys(names):
        for extra in ({"thinking": {"type": "disabled"}}, {}):
            body = {"model": name, "messages": msgs, "temperature": 0.3, "max_tokens": 700,
                    "response_format": {"type": "json_object"}, **extra}
            try:
                res = _post(key, body)
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 422):
                    continue                     # unknown model or parameter: try the next combination
                print(f"  ! DeepSeek briefing: HTTP {e.code}")
                return None
            except Exception as e:
                print(f"  ! DeepSeek briefing: {e}")
                return None
            try:
                out = json.loads(res["choices"][0]["message"]["content"])
                en, ms = out["en"].strip(), out["ms"].strip()
            except Exception:
                print("  ! DeepSeek briefing: reply was not the expected JSON")
                return None
            if not (numbers_ok(en, f) and numbers_ok(ms, f)):
                print("  ! DeepSeek briefing rejected: it used numbers that are not in the data")
                return None
            return {"en": en, "ms": ms, "model": res.get("model", name)}
    print("  ! DeepSeek briefing: no model accepted the request")
    return None
