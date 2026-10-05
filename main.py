"""Voice AI demo: form -> scrape company site -> train GHL Voice AI agent -> 'Try your voice agent'."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(".env")           # if you create one, it wins
load_dotenv(".env.example")   # otherwise use the values in .env.example

import ghl  # noqa: E402
from persona import ACTION_BOOK, ACTION_SLOTS, build_agent_prompt, build_welcome  # noqa: E402
from scraper import mobile_preview, scrape_company  # noqa: E402

# The chat + call widget shown inside the phone preview. Fixed in code on purpose: an old
# GHL_WIDGET_ID environment variable (e.g. on Render) must not bring back the previous widget.
WIDGET_ID = "6abf5919cb9ce9d3ea4df163"
WIDGET_LOCATION_ID = "s9jsy9dp0zOh0nDsRvcD"
DEFAULT_WIDGET_ID = WIDGET_ID
app = FastAPI(title="Voice AI Agent Demo")
print("=" * 60)
print("GHL agent updates:", "ON" if ghl.configured() else
      "OFF (DEMO MODE) - fill GHL_API_KEY, GHL_LOCATION_ID, GHL_AGENT_ID in .env")
print("Widget ID:", WIDGET_ID)
print("=" * 60)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
_agent_lock = asyncio.Lock()  # one shared demo agent -> serialize updates


class TrainRequest(BaseModel):
    name: str
    email: str = ""
    phone: str = ""
    company: str = ""
    website: str
    consent: bool = False
    tz: str = ""          # visitor's timezone from the browser, e.g. Europe/London (used for appointments)


try:                                   # can this server take its own screenshots (headless Chrome)?
    import playwright  # noqa: F401
    OWN_SCREENSHOTS = True
except ImportError:
    OWN_SCREENSHOTS = False


def thum_prefix() -> str:
    """thum.io screenshot URL prefix; the site URL is appended to it.
    Free tier = desktop layout. With a paid key (THUM_IO_AUTH=<id>-<secret>) we ask for a phone-width render."""
    auth = os.getenv("THUM_IO_AUTH", "").strip()
    if auth:
        return f"https://image.thum.io/get/auth/{auth}/width/600/crop/1300/viewportWidth/420/noanimate/"
    return "https://image.thum.io/get/width/600/crop/1500/noanimate/"


# ---------------------------------------------------------------- live phone preview
# Pages scraped by /api/train, kept briefly so the phone can show the LIVE site (not only a picture).
# /proxy only ever serves these cached pages - it never fetches arbitrary addresses on request.
PAGE_CACHE: dict[str, dict] = {}
PAGE_TTL = 2 * 3600
PAGE_MAX = 80


def _page_key(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url.strip() if "://" in url else "https://" + url.strip())
    host = p.netloc.lower()
    return (host[4:] if host.startswith("www.") else host) + p.path.rstrip("/")


def cache_page(urls: list[str], html: str, base_url: str, rendered: bool) -> None:
    now = time.time()
    for k in [k for k, v in PAGE_CACHE.items() if now - v["ts"] > PAGE_TTL]:
        PAGE_CACHE.pop(k, None)
    while len(PAGE_CACHE) >= PAGE_MAX:
        PAGE_CACHE.pop(min(PAGE_CACHE, key=lambda k: PAGE_CACHE[k]["ts"]), None)
    entry = {"html": html[:3_000_000], "base": base_url, "rendered": rendered, "ts": now}
    for u in urls:
        if u:
            PAGE_CACHE[_page_key(u)] = entry


# Runs inside the re-served page (which is sandboxed, so it has no storage/cookies of its own):
# gives scripts a harmless in-memory storage, stops links leaving the preview, and tells the
# phone whether a real, phone-width page actually rendered.
LIVE_INJECT_JS = r"""
(function () {
  function mem() { var d = {}; return { getItem: function (k) { return k in d ? d[k] : null; },
    setItem: function (k, v) { d[k] = String(v); }, removeItem: function (k) { delete d[k]; },
    clear: function () { d = {}; }, key: function (i) { return Object.keys(d)[i] || null; },
    get length() { return Object.keys(d).length; } }; }
  ["localStorage", "sessionStorage"].forEach(function (n) {
    try { window[n].getItem("x"); } catch (e) {
      try { Object.defineProperty(window, n, { configurable: true, value: mem() }); } catch (e2) {} }
  });
  try { document.cookie; } catch (e) {
    try { Object.defineProperty(document, "cookie", { configurable: true, get: function () { return ""; }, set: function () {} }); } catch (e2) {} }
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest && e.target.closest("a[href]");
    if (a && a.getAttribute("href").charAt(0) !== "#") e.preventDefault();
  }, true);
  document.addEventListener("submit", function (e) { e.preventDefault(); }, true);
  function report() {
    try {
      var t = document.body ? document.body.innerText : "";
      parent.postMessage({ type: "pv-ready", n: t.length, w: document.documentElement.scrollWidth, iw: window.innerWidth,
        c: /checking your browser|just a moment|verif(y|ying) you are human|access denied|attention required/i
             .test((document.title || "") + " " + t.slice(0, 600)) }, "*");
    } catch (e) {}
  }
  if (document.readyState === "complete") setTimeout(report, 1200);
  else window.addEventListener("load", function () { setTimeout(report, 1200); });
  setTimeout(report, 6000);
})();
"""


def proxied_html(html: str, base_url: str, rendered: bool) -> str:
    """Prepare a scraped page for showing inside the phone: resolve its links against the real site,
    drop its framing restrictions, and add our small helper script."""
    import re as _re
    from html import escape
    h = _re.sub(r"<meta[^>]+http-equiv\s*=\s*[\"']?(content-security-policy|x-frame-options|refresh)[\"']?[^>]*>",
                "", html, flags=_re.I)
    h = _re.sub(r"<base\b[^>]*>", "", h, flags=_re.I)
    if rendered:   # a snapshot of an already-rendered app: running its scripts again would redraw or break it
        h = _re.sub(r"<script\b[^>]*>.*?</script\s*>", "", h, flags=_re.I | _re.S)
    inject = (f'<base href="{escape(base_url, quote=True)}" target="_self">'
              '<meta name="referrer" content="no-referrer">'
              '<meta name="viewport" content="width=device-width, initial-scale=1">'
              '<style>html{scrollbar-width:none}::-webkit-scrollbar{display:none}</style>'
              f'<script>{LIVE_INJECT_JS}</script>')
    mt = _re.search(r"<head\b[^>]*>", h, flags=_re.I)
    return h[:mt.end()] + inject + h[mt.end():] if mt else inject + h


@app.get("/proxy", response_class=HTMLResponse)
def proxy(url: str = ""):
    """The visitor's own website, re-served from our address so it can be shown inside the phone
    (most sites forbid being embedded directly). Only pages we scraped in the last 2 hours."""
    entry = PAGE_CACHE.get(_page_key(url)) if url else None
    if not entry or time.time() - entry["ts"] > PAGE_TTL:
        raise HTTPException(404, "No live preview stored for this site")
    return HTMLResponse(proxied_html(entry["html"], entry["base"], entry["rendered"]), headers={
        "Content-Security-Policy": "sandbox allow-scripts",     # no access to our own site's data
        "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"})


FRAME_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Preview</title>
<style>
  html,body{margin:0;height:100%;overflow:hidden;background:#fff;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  #view{position:fixed;inset:0;overflow:hidden;background:#fff}
  #live{position:absolute;inset:0;width:100%;height:100%;border:0;background:#fff;opacity:0;pointer-events:none}
  #live.on{opacity:1;pointer-events:auto}
  #shotwrap{position:absolute;inset:0;overflow-y:auto;display:none;scrollbar-width:none}
  #shotwrap::-webkit-scrollbar{display:none}
  #shot{width:100%;display:block}
  #skeleton{position:absolute;inset:0;background:#fff;display:flex;flex-direction:column;z-index:2}
  #skeleton .bar{height:44px;background:#F8FAFC;display:flex;align-items:center;padding:0 16px}
  #skeleton .body{flex:1;padding:16px;display:grid;gap:12px;align-content:start}
  .sk{background:#EEF2F7;border-radius:12px;animation:skp 1.2s ease-in-out infinite}
  @keyframes skp{50%{opacity:.45}}
  #skeleton .foot{display:flex;flex-direction:column;align-items:center;gap:8px;padding-bottom:110px;color:#94A3B8;font-size:12px}
  .spin{width:26px;height:26px;border:3px solid #E2E8F0;border-top-color:#2563EB;border-radius:50%;animation:sp 1s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
  #fallback{position:absolute;inset:0;display:none;background:linear-gradient(135deg,#2563EB,#45C9FD);color:#fff;
            flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:24px;text-align:center}
  #fallback h1{margin:0;font-size:22px} #fallback p{margin:0;opacity:.9;font-size:13px}
  #fallback img{width:76px;height:76px;border-radius:18px;background:#fff;padding:8px;display:block;margin-bottom:6px}
</style></head>
<body>
<div id="view">
  <!-- 1st choice: the live website, re-served by /proxy, sandboxed -->
  <iframe id="live" title="Live website" sandbox="allow-scripts"></iframe>
  <!-- 2nd choice: a picture of it -->
  <div id="shotwrap"><img id="shot" alt=""></div>
  <!-- while loading -->
  <div id="skeleton">
    <div class="bar"><div class="sk" style="width:70px;height:10px"></div></div>
    <div class="body"><div class="sk" style="height:110px"></div><div class="sk" style="height:12px;width:75%"></div>
      <div class="sk" style="height:12px;width:50%"></div><div class="sk" style="height:150px"></div></div>
    <div class="foot"><div class="spin"></div>Fetching a live view of your site…</div>
  </div>
  <!-- last resort -->
  <div id="fallback"><img src="https://www.google.com/s2/favicons?sz=128&domain=__HOST__" alt="" onerror="this.style.display='none'"><h1>__NAME__</h1><p>__HOST__</p></div>
</div>
<script>
  // What the phone shows, in order:
  //   1. the live site (via /proxy)            - real mobile layout, scrollable
  //   2. our own phone-layout screenshot       - taken by the server's headless Chrome
  //   3. Microlink, 4. WordPress mShots, 5. thum.io   - outside screenshot services
  //   6. a branded card
  (function () {
    var live = document.getElementById("live"), wrap = document.getElementById("shotwrap"),
        img = document.getElementById("shot"), sk = document.getElementById("skeleton"),
        fb = document.getElementById("fallback");
    var own = ""; try { own = sessionStorage.getItem("pv_shot") || ""; } catch (e) {}
    var shots = [own, "__MICROLINK__", "__MSHOTS__", "__THUM__"].filter(function (u) { return !!u; });
    var done = false, liveTimer = null;
    function show(which) {
      done = true; sk.style.display = "none";
      live.className = which === "live" ? "on" : "";
      wrap.style.display = which === "shot" ? "block" : "none";
      fb.style.display = which === "card" ? "flex" : "none";
      try { parent.postMessage({type: "preview-view", view: which}, "*"); } catch (e) {}
    }
    function nextShot() { if (done) return; if (!shots.length) return show("card"); img.src = shots.shift(); }
    img.onload = function () {
      if (done) return;
      if (img.naturalWidth < 200 || img.naturalHeight < 300) return nextShot();   // "still generating" placeholder
      show("shot");
    };
    img.onerror = nextShot;
    function startShots() { if (done) return; clearTimeout(liveTimer); live.src = "about:blank"; nextShot(); }
    window.addEventListener("message", function (e) {
      if (done || e.source !== live.contentWindow || !e.data || e.data.type !== "pv-ready") return;
      var d = e.data;                       // real text, no "checking your browser", and it fits a phone width
      if (d.n >= 80 && !d.c && d.w <= d.iw * 1.25) { clearTimeout(liveTimer); show("live"); }
      else startShots();
    });
    if (__HAS_LIVE__) { live.src = "__PROXY__"; liveTimer = setTimeout(startShots, 9000); }
    else startShots();
    setTimeout(function () { if (!done) show("card"); }, 30000);    // never spin forever
  })();
  // tell the page outside the phone what the voice call is doing
  (function () {
    var orig = window.fetch;
    window.fetch = function () {
      var args = arguments, url = String((args[0] && args[0].url) || args[0] || "");
      return orig.apply(this, args).then(function (res) {
        if (url.indexOf("start-voice-ai-call") !== -1) {
          var noAgent = /\/undefined$/.test(url);
          parent.postMessage({type: "voice", ok: res.status < 400, status: res.status, noAgent: noAgent}, "*");
        }
        return res;
      });
    };
  })();
  // open the chat widget automatically so the options are visible straight away
  window.addEventListener("LC_chatWidgetLoaded", function () {
    parent.postMessage({type: "widget-loaded"}, "*");
    if (__AUTO_OPEN__) setTimeout(function () {
      try { window.leadConnector.chatWidget.openWidget(); } catch (e) {}
    }, 1500);
  });
</script>
<!-- GHL chat + call widget: exact embed code from GHL (Sites > Chat Widget > Get Code) -->
<div data-chat-widget data-widget-id="__WIDGET__" data-location-id="__LOCATION__"></div><script src="https://widgets.leadconnectorhq.com/loader.js" data-resources-url="https://widgets.leadconnectorhq.com/chat-widget/loader.js" data-widget-id="__WIDGET__"></script>
</body></html>"""


@app.get("/preview-frame", response_class=HTMLResponse)
def preview_frame(site: str = "", name: str = "", mode: str = ""):
    """The page shown INSIDE the phone: the visitor's website (live, or a picture) with the GHL widget on top."""
    from html import escape
    from urllib.parse import quote, urlparse
    site = site.strip()
    p = urlparse(site)
    if p.scheme not in ("http", "https") or not p.netloc or any(ch in site for ch in '"<> \\\n\r\'`'):
        raise HTTPException(400, "Invalid website address")
    q = quote(site, safe="")
    entry = PAGE_CACHE.get(_page_key(site))
    has_live = bool(entry) and time.time() - entry["ts"] <= PAGE_TTL
    html = (FRAME_HTML
            .replace("__PROXY__", "/proxy?url=" + q)
            .replace("__HAS_LIVE__", "true" if has_live else "false")
            .replace("__MICROLINK__", "https://api.microlink.io/?url=" + q + "&screenshot=true&meta=false&embed=screenshot.url"
                     "&viewport.width=390&viewport.height=844&viewport.isMobile=true&viewport.hasTouch=true&viewport.deviceScaleFactor=2")
            .replace("__MSHOTS__", "https://s.wordpress.com/mshots/v1/" + q + "?w=390&h=844")
            .replace("__THUM__", thum_prefix() + site)
            .replace("__NAME__", escape(name[:80] or p.netloc))
            .replace("__HOST__", escape(p.netloc))
            .replace("__WIDGET__", WIDGET_ID)
            .replace("__LOCATION__", WIDGET_LOCATION_ID)
            .replace("__AUTO_OPEN__", "false" if os.getenv("WIDGET_AUTO_OPEN", "1") in ("0", "false", "no") else "true"))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def config():
    """Public config the frontend needs to open the GHL voice widget / call the agent."""
    return {
        "widgetId": WIDGET_ID,
        "whatsapp": os.getenv("WHATSAPP_NUMBER", "+44 7446 952720"),
        "thumPrefix": thum_prefix(),
        "ownScreenshots": OWN_SCREENSHOTS,
        "agentPhone": os.getenv("GHL_AGENT_PHONE", ""),
        "ghlConfigured": ghl.configured(),
        "booking": booking_enabled(),
    }


@app.post("/api/train")
async def train(req: TrainRequest, request: Request):
    t0 = time.time()
    if not req.consent:
        raise HTTPException(400, "Please tick the consent box to continue.")
    visitor = req.model_dump()
    # make sure the voice agent has its two booking actions (first run creates them in GHL)
    actions_task = asyncio.create_task(ensure_booking_actions(public_base(request)))
    consent_line = "Consent to use public website content for the demo: YES (" + \
                   time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()) + ")"

    # 1) save the form to GHL straight away - the lead is kept even if the website can't be read
    contact_task = asyncio.create_task(
        ghl.upsert_contact(visitor, req.company, req.website) if ghl.configured() else asyncio.sleep(0))

    async def note(text: str):
        contact = await contact_task
        if isinstance(contact, dict) and contact.get("id"):
            await ghl.add_note(contact["id"], text)
        return contact

    form_lines = (f"Name: {req.name}\nEmail: {req.email or '-'}\nPhone: {req.phone or '-'}\n"
                  f"Company: {req.company or '-'}\nWebsite: {req.website}\n{consent_line}")

    # 2) read the company website
    try:
        data = await scrape_company(req.website, req.company)
        if not (data["about"] or data["services"] or data["highlights"] or data["description"]):
            raise ValueError("Website loaded but no readable text was found.")
    except ValueError as e:
        await note(f"Pragna AI voice demo request - website could NOT be read\n\n{form_lines}\n\nReason: {e}")
        raise HTTPException(400, str(e))
    # keep the homepage we fetched so the phone can show the live site
    home_html, home_url, rendered = data.pop("_home_html", None), data.pop("_home_url", None), data.pop("_rendered", False)
    if home_html:
        site_typed = req.website.strip() if "://" in req.website else "https://" + req.website.strip()
        cache_page([site_typed, home_url, data["website"]], home_html, home_url or data["website"], rendered)

    # remember this lead so the agent (or the button on the page) can book an appointment for them
    lead = new_lead(visitor, data["company_name"], data["website"])
    can_book = await actions_task
    prompt = build_agent_prompt(data, visitor, {"ref": lead["ref"], "tz": lead["tz"],
                                                "now": datetime.now(get_tz(lead["tz"]))} if can_book else None)
    welcome = build_welcome(data, visitor)

    # run in parallel: train GHL agent, save contact, take mobile screenshot of the site
    async def update_agent():
        if not ghl.configured():
            return "demo-mode (GHL not configured)"
        async with _agent_lock:
            try:
                await ghl.update_voice_agent(prompt, welcome)
                return "updated"
            except RuntimeError as e:
                print("[ghl]", e)
                return f"error: {e}"

    async def save_contact():
        if not ghl.configured():
            return None
        services = ", ".join(data["services"][:10]) or "-"
        return await note(
            f"Pragna AI voice demo request\n\n{form_lines}\n\n"
            f"Scraped company: {data['company_name']} ({data['website']})\n"
            f"Description: {data.get('description') or '-'}\n"
            f"Services: {services}\n"
            f"Phones: {', '.join(data['phones']) or '-'} | Emails: {', '.join(data['emails']) or '-'}\n"
            f"Pages read: {len(data['pages_scraped'])}")

    agent_status, contact, preview = await asyncio.gather(
        update_agent(), save_contact(), mobile_preview(data["website"]))
    lead["contactId"] = (contact or {}).get("id") or ""
    save_state()
    # brand colour: what the page really renders, else what its stylesheets say
    theme = preview.get("theme") if (preview.get("theme") or {}).get("primary") else data.get("theme")

    return {
        "status": "ok",
        "agentStatus": agent_status,
        "seconds": round(time.time() - t0, 1),
        "company": {k: data[k] for k in ("company_name", "website", "description", "services",
                                         "hours", "address", "emails", "phones", "socials", "pages_scraped")},
        "faqCount": len(data["faqs"]),
        "welcomeMessage": welcome,
        "promptPreview": prompt[:1500],
        "promptChars": len(prompt),
        "contact": contact,
        "screenshot": preview.get("screenshot"),
        "previewBlocked": bool(preview.get("blocked")),
        "theme": theme,
        "bookingRef": lead["ref"] if booking_enabled() else "",
        "agentCanBook": bool(can_book),
    }


# ================================================================ appointments + opportunities
# During the demo call the agent asks "would you like to book an appointment?". If yes it calls
# /api/ghl/slots and /api/ghl/book (two "custom actions" this server registers on the GHL agent).
# Booking creates the appointment in the GHL calendar, an opportunity in the pipeline and a note
# on the contact. The same two addresses power the "Book an appointment" button on the page.
STATE_FILE = Path(__file__).with_name(".demo_state.json")     # action ids + recent leads (no email/phone)
STATE: dict = {"actions": {}, "leads": {}}
try:
    STATE.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
except (OSError, ValueError):
    pass
LEADS: dict[str, dict] = STATE.setdefault("leads", {})
_actions_lock = asyncio.Lock()
_book_lock = asyncio.Lock()
_bcfg: dict = {"ts": 0.0, "v": None}
REF_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"                # no 0/O/1/I - easy to copy


def save_state() -> None:
    try:
        keep = {r: {k: v for k, v in l.items() if k not in ("email", "phone")} for r, l in LEADS.items()}
        STATE_FILE.write_text(json.dumps({"actions": STATE.get("actions", {}), "leads": keep}), encoding="utf-8")
    except OSError as e:
        print("[booking] could not save state:", e)


def booking_enabled() -> bool:
    return ghl.configured() and os.getenv("BOOKING_ENABLED", "1").lower() not in ("0", "false", "no")


def booking_secret() -> str:
    return os.getenv("BOOKING_SECRET") or hashlib.sha256(
        (os.getenv("GHL_API_KEY", "") + "|pragna-booking").encode()).hexdigest()[:32]


def get_tz(name: str):
    from zoneinfo import ZoneInfo
    for n in (name, os.getenv("DEFAULT_TIMEZONE", "Europe/London")):
        if n and re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+\-]+){0,2}", n):
            try:
                return ZoneInfo(n)
            except Exception:       # unknown name, or no timezone data on this machine (pip install tzdata)
                pass
    return timezone.utc


def tz_name(tz) -> str:
    return getattr(tz, "key", "UTC")


def new_lead(visitor: dict, company: str, website: str) -> dict:
    now = time.time()
    for r in [r for r, l in LEADS.items() if now - l.get("ts", 0) > 86400]:
        LEADS.pop(r, None)
    while len(LEADS) >= 200:
        LEADS.pop(min(LEADS, key=lambda r: LEADS[r].get("ts", 0)), None)
    ref = "".join(secrets.choice(REF_CHARS) for _ in range(8))
    lead = {"ref": ref, "ts": now, "name": (visitor.get("name") or "").strip(), "company": company, "website": website,
            "email": visitor.get("email") or "", "phone": visitor.get("phone") or "",
            "tz": tz_name(get_tz(visitor.get("tz") or "")), "contactId": "", "booking": None}
    LEADS[ref] = lead
    return lead


def public_base(request: Request) -> str:
    """Address GHL can reach this server on. PUBLIC_URL in .env wins; otherwise the address in the browser."""
    env = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    h = host.split(":")[0].lower()
    if proto != "https" or not h or "." not in h or h.endswith(".local") or re.match(r"^[\d.]+$", h):
        return ""                       # localhost / LAN address: GHL cannot call it
    return f"https://{host}"


def _action_params(kind: str, base: str) -> dict:
    ref = {"name": "booking_reference", "type": "string", "example": "K7Q2M9XA",
           "description": "The booking reference given in your instructions. Always send it exactly."}
    day = {"name": "date", "type": "string", "example": "2026-10-08",
           "description": "The day as YYYY-MM-DD." + (" Leave empty to get the next free days." if kind == "slots" else "")}
    tm = {"name": "time", "type": "string", "example": "14:30", "description": "The time the caller chose, 24-hour HH:MM."}
    api = {"url": f"{base}/api/ghl/{kind}", "method": "GET", "authenticationRequired": True,
           "authenticationValue": booking_secret(),
           "headers": [{"key": "X-Booking-Key", "value": booking_secret()}],
           "parameters": [ref, day] + ([tm] if kind == "book" else [])}
    if kind == "slots":
        return {"triggerPrompt": "When the caller would like to book an appointment and you need to know which days and "
                                 "times are free, or the caller asks what times are available.",
                "triggerMessage": "Let me check the calendar for you.", "apiDetails": api,
                "selectedPaths": ["status", "message"]}
    return {"triggerPrompt": "When the caller has agreed to book an appointment and has chosen a specific day and time.",
            "triggerMessage": "One moment, I'm booking that for you.", "apiDetails": api,
            "selectedPaths": ["status", "message"]}


async def ensure_booking_actions(base: str) -> bool:
    """Create (once) or repair the two booking actions on the GHL voice agent. True = the agent can book."""
    if not base or not booking_enabled():
        return False
    async with _actions_lock:
        st = STATE.setdefault("actions", {})
        now = time.time()
        if st.get("base") == base and st.get("slots") and st.get("book") and now - st.get("checked", 0) < 3600:
            return True
        if now - st.get("failed", 0) < 300:           # e.g. token has no permission: don't retry on every visitor
            return False
        existing = await ghl.agent_actions()          # None = could not read the agent
        ok = True
        for kind, name in (("slots", ACTION_SLOTS), ("book", ACTION_BOOK)):
            url = f"{base}/api/ghl/{kind}"
            found = next((a for a in existing or [] if a.get("name") == name), None)
            aid = (found or {}).get("id") or ("" if existing is not None else st.get(kind, ""))
            if found and found.get("url") == url:
                st[kind] = aid
                continue
            res = await ghl.save_custom_action(name, _action_params(kind, base), aid)
            if res.get("id"):
                st[kind] = res["id"]
                print(f"[booking] agent action '{name}' ready -> {url}")
            else:
                ok = False
                st.pop(kind, None)
                print(f"[booking] could NOT add the '{name}' action to the voice agent: {res.get('error')}\n"
                      "          The token needs the scope voice-ai-agent-goals.write (Settings > Private Integrations).")
        st.update({"base": base, "checked": now} if ok else {"failed": now})
        save_state()
        return ok


async def booking_config() -> dict:
    """Which calendar and pipeline to use: from .env, otherwise picked automatically from the GHL account."""
    if _bcfg["v"] and time.time() - _bcfg["ts"] < 600:
        return _bcfg["v"]
    cals, pipes = await asyncio.gather(ghl.list_calendars(), ghl.list_pipelines())
    want = os.getenv("GHL_CALENDAR_ID", "").strip()
    active = [c for c in cals if c.get("isActive", True)]
    cal = next((c for c in cals if c.get("id") == want), None) if want else (
        next((c for c in active if re.search(r"demo|pragna|voice|discovery|consult", c.get("name") or "", re.I)), None)
        or (active[0] if active else None))
    mins = 30
    if cal and cal.get("slotDuration"):
        mins = int(cal["slotDuration"] * (60 if cal.get("slotDurationUnit") == "hours" else 1))
    want_p, want_s = os.getenv("GHL_PIPELINE_ID", "").strip(), os.getenv("GHL_PIPELINE_STAGE_ID", "").strip()
    pipe = next((x for x in pipes if x.get("id") == want_p), None) if want_p else (
        next((x for x in pipes if re.search(r"voice|demo|pragna|sales|lead", x.get("name") or "", re.I)), None)
        or (pipes[0] if pipes else None))
    stages = (pipe or {}).get("stages") or []
    stage = next((x for x in stages if x.get("id") == want_s), None) if want_s else (
        next((x for x in stages if re.search(r"appoint|book|demo|meeting|call", x.get("name") or "", re.I)), None)
        or (stages[0] if stages else None))
    v = {"calendarId": (cal or {}).get("id") or want, "calendarName": (cal or {}).get("name") or "", "slotMins": mins,
         "pipelineId": (pipe or {}).get("id") or want_p, "pipelineName": (pipe or {}).get("name") or "",
         "stageId": (stage or {}).get("id") or want_s, "stageName": (stage or {}).get("name") or ""}
    print(f"[booking] calendar: {v['calendarName'] or v['calendarId'] or 'NONE FOUND'} | "
          f"pipeline: {v['pipelineName'] or v['pipelineId'] or 'NONE FOUND'} / stage: {v['stageName'] or v['stageId'] or '-'}")
    if v["calendarId"] and v["pipelineId"]:
        _bcfg.update(ts=time.time(), v=v)
    return v


# ---- understanding "next Tuesday at 2:30 pm"
MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
               "october", "november", "december"]
WEEKDAYS = ["mon(day)?", "tue(s|sday)?", "wed(s|nesday)?", "thu(r|rs|rsday)?", "fri(day)?", "sat(urday)?", "sun(day)?"]


def _mk_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_day(text: str, today: date) -> date | None:
    s = (text or "").strip().lower()
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _mk_date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b", s)          # British: day/month(/year)
    if m:
        y = int(m[3]) if m[3] else today.year
        d = _mk_date(y + 2000 if y < 100 else y, int(m[2]), int(m[1]))
        if d and not m[3] and d < today:
            d = _mk_date(d.year + 1, d.month, d.day)
        return d
    for m in list(re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})\b", s)) + \
            list(re.finditer(r"\b([a-z]{3,9})\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b", s)):
        a, b = m[1], m[2]
        word, num = (b, a) if a.isdigit() else (a, b)
        mon = next((i for i, n in enumerate(MONTH_NAMES, 1) if n.startswith(word)), 0)
        if mon:
            ym = re.search(r"\b(20\d{2})\b", s)
            d = _mk_date(int(ym[1]) if ym else today.year, mon, int(num))
            if d and not ym and d < today:
                d = _mk_date(d.year + 1, d.month, d.day)
            return d
    if "day after tomorrow" in s:
        return today + timedelta(days=2)
    if "tomorrow" in s:
        return today + timedelta(days=1)
    if "today" in s or "tonight" in s:
        return today
    for i, w in enumerate(WEEKDAYS):
        if re.search(rf"\b{w}\b", s):
            return today + timedelta(days=((i - today.weekday()) % 7) or 7)
    return None


def parse_time(text: str) -> tuple[int, int] | None:
    s = re.sub(r"\d{4}-\d{1,2}-\d{1,2}t?", " ", (text or "").strip().lower())
    if not s.strip():
        return None
    if "noon" in s or "midday" in s:
        return 12, 0
    m = re.search(r"\b(\d{1,2})(?:[:.h](\d{2}))?\s*(a\.?m|p\.?m)\b", s)
    if m:
        h, mi = int(m[1]), int(m[2] or 0)
        if not (1 <= h <= 12 and mi < 60):
            return None
        return (h % 12) + (12 if m[3].startswith("p") else 0), mi
    m = re.search(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)\b", s)
    if m:
        return int(m[1]), int(m[2])
    m = re.fullmatch(r"\s*([01]\d|2[0-3])([0-5]\d)\s*", s)                        # "1430"
    if m:
        return int(m[1]), int(m[2])
    m = re.fullmatch(r"\s*(?:at\s+)?(\d{1,2})(?:\s*o'?clock)?\s*", s) or re.search(r"\b(\d{1,2})\s*o'?clock\b", s)
    if m and 0 < int(m[1]) < 24:
        h = int(m[1])
        return (h + 12 if h < 8 else h), 0                                         # "at 3" means 3 pm
    return None


def fmt_day(d) -> str:
    return f"{d:%A} {d.day} {d:%B}"


def fmt_time(dt) -> str:
    h = dt.hour % 12 or 12
    return f"{h}{'' if dt.minute == 0 else f':{dt.minute:02d}'} {'am' if dt.hour < 12 else 'pm'}"


def _spread(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


async def open_slots(calendar_id: str, tz, start: datetime, days: int) -> dict[date, list[datetime]] | None:
    """Free appointment times from GHL, grouped by day, in the visitor's timezone. None = could not ask GHL."""
    begin = max(start, datetime.now(tz) + timedelta(minutes=30))
    end = datetime.combine(start.date() + timedelta(days=days), datetime.min.time(), tzinfo=tz)
    raw = await ghl.free_slots(calendar_id, int(begin.timestamp() * 1000), int(end.timestamp() * 1000), tz_name(tz))
    if raw is None:
        return None
    out: dict[date, list[datetime]] = {}
    for times in raw.values():
        for t in times:
            try:
                dt = datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(tz)
            except ValueError:
                continue
            if begin <= dt < end:
                out.setdefault(dt.date(), []).append(dt)
    return {d: sorted(set(v)) for d, v in sorted(out.items())}


def _speak_slots(by_day: dict, max_days: int = 3, per_day: int = 3) -> str:
    return ". ".join(f"{fmt_day(d)}: {', '.join(fmt_time(t) for t in _spread(ts, per_day))}"
                     for d, ts in list(by_day.items())[:max_days])


async def _args(request: Request) -> dict:
    """Values sent by the voice agent: in the address (GET) or in the body (POST)."""
    a = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                a.update({k: v for k, v in body.items() if isinstance(v, (str, int, float))})
        except ValueError:
            pass
    return {k: str(v).strip() for k, v in a.items()}


def _find_lead(request: Request, a: dict) -> dict | None:
    """Who is this booking for? The reference in the agent's instructions says so. A caller that proves it is
    our GHL agent (secret key) but sent no usable reference gets the most recent lead - the agent is shared."""
    sent = (request.headers.get("x-booking-key") or a.get("key") or
            re.sub(r"^bearer\s+", "", request.headers.get("authorization") or "", flags=re.I))
    key_ok = bool(sent) and hmac.compare_digest(sent.strip(), booking_secret())
    ref = re.sub(r"[^A-Z0-9]", "", (a.get("booking_reference") or a.get("ref") or "").upper())
    if ref in LEADS:
        return LEADS[ref]
    recent = [l for l in LEADS.values() if time.time() - l.get("ts", 0) < 7200]
    if key_ok and recent:
        return max(recent, key=lambda l: l["ts"])
    if not key_ok:
        raise HTTPException(401, "Unknown booking reference")
    return None


@app.api_route("/api/ghl/slots", methods=["GET", "POST"])
async def ghl_slots(request: Request):
    """Free appointment times. Used by the voice agent ("Check appointment times") and by the page."""
    a = await _args(request)
    lead = _find_lead(request, a)
    if not booking_enabled():
        return {"status": "error", "message": "Booking is not available right now. The team will contact the caller to arrange a time."}
    tz = get_tz((lead or {}).get("tz", ""))
    now = datetime.now(tz)
    cfg = await booking_config()
    if not cfg["calendarId"]:
        return {"status": "error", "message": "The calendar is not available right now. Ask which day and time they prefer "
                                              "and say the team will confirm it."}
    day = parse_day(a.get("date", ""), now.date())
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz) if day and day >= now.date() else now
    by_day = await open_slots(cfg["calendarId"], tz, start, 1 if day else 7)
    if day and not by_day:                                   # nothing that day -> look at the following week
        by_day = await open_slots(cfg["calendarId"], tz, start, 8)
        prefix = f"There are no free times on {fmt_day(day)}. "
    else:
        prefix = ""
    if by_day is None:
        return {"status": "error", "message": "I could not read the calendar. Ask which day and time they prefer and try to book it."}
    if not by_day:
        return {"status": "none", "timezone": tz_name(tz), "days": [],
                "message": prefix + "There are no free appointment times in the next few days. Say the team will contact them to arrange a time."}
    days = [{"date": d.isoformat(), "label": fmt_day(d),
             "times": [{"time": f"{t:%H:%M}", "label": fmt_time(t)} for t in ts[:24]]} for d, ts in list(by_day.items())[:6]]
    return {"status": "ok", "timezone": tz_name(tz), "days": days,
            "message": f"{prefix}Free times ({tz_name(tz)} time): {_speak_slots(by_day)}. Offer two or three of these."}


@app.api_route("/api/ghl/book", methods=["GET", "POST"])
async def ghl_book(request: Request):
    """Book the appointment: GHL calendar event + opportunity + note. Used by the voice agent and by the page."""
    a = await _args(request)
    lead = _find_lead(request, a)
    if not lead or not booking_enabled():
        return {"status": "error", "message": "I could not complete the booking. Apologise and say the team will contact them to arrange a time."}
    async with _book_lock:
        res = await book_for(lead, a.get("date", ""), a.get("time", ""))
    print(f"[booking] {lead['ref']} {lead.get('company')}: {res['status']} - {res['message'][:140]}")
    return res


async def book_for(lead: dict, date_s: str, time_s: str) -> dict:
    brand = os.getenv("BRAND_NAME", "Pragna AI")
    tz = get_tz(lead.get("tz", ""))
    now = datetime.now(tz)
    day = parse_day(date_s, now.date())
    hm = parse_time(time_s) or parse_time(date_s)
    if not day:
        return {"status": "need_time", "message": "I need the day. Ask which day they would like, then try again with the date as YYYY-MM-DD."}
    if not hm:
        return {"status": "need_time", "message": f"I need the time. Ask what time on {fmt_day(day)} suits them, then try again with the time as HH:MM."}
    start = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=tz)
    when = f"{fmt_day(start)} at {fmt_time(start)}"
    if start < now + timedelta(minutes=15):
        return {"status": "unavailable", "message": f"{when} has already passed or is too soon. Ask for a later day or time."}
    if start > now + timedelta(days=60):
        return {"status": "unavailable", "message": "That is too far ahead. Ask for a day within the next few weeks."}
    old = lead.get("booking") or {}
    if old.get("appointmentId") and old.get("start") == start.isoformat():
        return {"status": "booked", "message": f"Already booked: {when} ({tz_name(tz)} time).", "when": when, "start": old["start"]}

    cfg = await booking_config()
    name, company = lead.get("name") or "the visitor", lead.get("company") or lead.get("website") or "their company"
    if not lead.get("contactId") and (lead.get("email") or lead.get("phone")):       # contact was not saved earlier
        lead["contactId"] = (await ghl.upsert_contact(lead, company, lead.get("website", ""))).get("id") or ""
    cid = lead.get("contactId")
    if not cid:
        return {"status": "error", "message": "I could not complete the booking. Apologise and say the team will contact them to arrange a time."}

    appt, problem = {}, ""
    if not cfg["calendarId"]:
        problem = "no calendar was found in GHL (create one in Calendars, or set GHL_CALENDAR_ID in .env)"
    else:
        by_day = await open_slots(cfg["calendarId"], tz, datetime.combine(day, datetime.min.time(), tzinfo=tz), 1)
        if by_day is not None and start not in by_day.get(day, []):
            same_day = sorted(by_day.get(day, []), key=lambda t: abs((t - start).total_seconds()))[:3]
            if same_day:
                alts = f"The closest free times on {fmt_day(day)} are {', '.join(fmt_time(t) for t in sorted(same_day))}"
            else:
                later = await open_slots(cfg["calendarId"], tz, start, 8) or {}
                alts = f"Other free times: {_speak_slots(later)}" if later else "There are no other free times this week"
            return {"status": "unavailable", "message": f"{when} is not available. {alts}. Ask which one they would like."}
        end = (start + timedelta(minutes=cfg["slotMins"])).isoformat()
        title = f"{brand} demo call - {company} ({name})"
        desc = (f"Booked from the {brand} voice agent demo.\nName: {name}\nCompany: {company}\n"
                f"Website: {lead.get('website') or '-'}\nVisitor timezone: {tz_name(tz)}")
        if old.get("appointmentId"):                                               # they changed the time
            appt = await ghl.move_appointment(old["appointmentId"], cfg["calendarId"], start.isoformat(), end)
        if not appt.get("id"):
            appt = await ghl.create_appointment(cfg["calendarId"], cid, start.isoformat(), end, title, desc)
        if not appt.get("id"):
            if appt.get("status") in (400, 422) and re.search(r"slot|available|booked", appt.get("error", ""), re.I):
                return {"status": "unavailable", "message": f"{when} is not available. Ask for another day or time."}
            problem = f"GHL refused the appointment ({appt.get('error')})"

    # the opportunity is created either way: they asked for an appointment, so they are a real lead
    opp = {}
    if cfg["pipelineId"]:
        opp = await ghl.save_opportunity(cid, f"{company} - AI voice agent demo ({name})", cfg["pipelineId"], cfg["stageId"])
    opp_line = (f"Opportunity {'created' if opp.get('new') else 'updated'} in pipeline \"{cfg['pipelineName'] or cfg['pipelineId']}\""
                + (f", stage \"{cfg['stageName']}\"" if cfg["stageName"] else "")) if opp.get("id") else \
               f"Opportunity NOT created ({opp.get('error') or 'no pipeline found in GHL'})"
    if problem:
        print("[booking] appointment not created:", problem)
        await ghl.add_note(cid, f"{brand} voice demo - APPOINTMENT REQUESTED (not booked automatically)\n\n"
                                f"Requested time: {when} ({tz_name(tz)})\nReason: {problem}\n{opp_line}\n\nPlease contact them to confirm a time.")
        return {"status": "error", "message": "I could not book it in the calendar. Apologise, say their preferred time has been "
                                              f"passed to the {brand} team, who will contact them to confirm."}
    lead["booking"] = {"start": start.isoformat(), "when": when, "appointmentId": appt["id"], "opportunityId": opp.get("id", "")}
    save_state()
    await ghl.add_note(cid, f"{brand} voice demo - APPOINTMENT BOOKED\n\nWhen: {when} ({tz_name(tz)})\n"
                            f"Calendar: {cfg['calendarName'] or cfg['calendarId']}\n{opp_line}\nCompany: {company}\nWebsite: {lead.get('website') or '-'}")
    return {"status": "booked", "when": when, "start": start.isoformat(),
            "message": f"Booked. The appointment with the {brand} team is on {when} ({tz_name(tz)} time). Read this back to them."}


@app.get("/api/booking")
def booking_status(ref: str = ""):
    """Lets the page show "appointment booked" as soon as the agent has booked it during the call."""
    lead = LEADS.get(re.sub(r"[^A-Z0-9]", "", ref.upper()))
    b = (lead or {}).get("booking")
    return {"booked": bool(b), "when": (b or {}).get("when", ""), "timezone": (lead or {}).get("tz", "")}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
