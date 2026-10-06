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
from persona import (ACTION_BOOK, ACTION_CANCEL, ACTION_CHANGE, ACTION_SLOTS,  # noqa: E402
                     build_agent_prompt, build_chat_agent, build_welcome)
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
    # make sure the voice agent has its booking actions (first run creates them in GHL)
    actions_task = asyncio.create_task(ensure_booking_actions(public_base(request)))
    # free appointment times go into the agent's instructions, so it only offers times that can really be booked
    times_task = asyncio.create_task(prompt_times(get_tz(req.tz)) if booking_enabled() else asyncio.sleep(0, []))
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
    can_book = await actions_task                 # "reply", "send" or ""
    first = await contact_task
    lead["contactId"] = (first or {}).get("id") or "" if isinstance(first, dict) else ""
    had = await current_booking(lead) if booking_enabled() and lead["contactId"] else None   # booked in an earlier visit?
    prompt = build_agent_prompt(data, visitor, {
        "ref": lead["ref"], "tz": lead["tz"], "now": datetime.now(get_tz(lead["tz"])),
        "replies": can_book == "reply", "existing": (had or {}).get("when", ""),
        "times": await times_task} if can_book else None)
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

    agent_status, contact, preview, chat_status = await asyncio.gather(
        update_agent(), save_contact(), mobile_preview(data["website"]), update_chat_bot(data, visitor))
    lead["contactId"] = lead["contactId"] or (contact or {}).get("id") or ""
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
        "chatStatus": chat_status,
        "booking": booking_status(lead["ref"]),
    }


# ================================================================ appointments + opportunities
# During the demo call the agent asks "would you like to book an appointment?". It can then book, change
# and cancel through four "custom actions" this server registers on the GHL agent:
#   /api/ghl/slots  /api/ghl/book  /api/ghl/change  /api/ghl/cancel
# Booking creates the appointment in the GHL calendar, an opportunity in the pipeline and a note on the
# contact. The same addresses power the "Book an appointment" button on the page.
# /api/booking/check?loc=<GHL location id> shows what is working and what GHL refused.
STATE_FILE = Path(__file__).with_name(".demo_state.json")     # action ids, recent leads (no email/phone), call log
STATE: dict = {"actions": {}, "leads": {}, "events": []}
try:
    STATE.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
except (OSError, ValueError):
    pass
LEADS: dict[str, dict] = STATE.setdefault("leads", {})
EVENTS: list[dict] = STATE.setdefault("events", [])           # what the agent / page asked for, and the outcome
_bg: set = set()                                              # background jobs (kept so they are not dropped)
_actions_lock = asyncio.Lock()
_book_lock = asyncio.Lock()
_bcfg: dict = {"ts": 0.0, "v": None}
REF_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"                # no 0/O/1/I - easy to copy


def save_state() -> None:
    try:
        keep = {r: {k: v for k, v in l.items() if k not in ("email", "phone")} for r, l in LEADS.items()}
        STATE_FILE.write_text(json.dumps({"actions": STATE.get("actions", {}), "chat": STATE.get("chat", {}), "leads": keep,
                                          "events": EVENTS[-60:]}), encoding="utf-8")
    except OSError as e:
        print("[booking] could not save state:", e)


def log_event(kind: str, source: str, status: str, detail: str = "") -> None:
    EVENTS.append({"ts": time.time(), "kind": kind, "source": source, "status": status, "detail": detail[:200]})
    del EVENTS[:-60]
    save_state()


def bg(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


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
    for r in [r for r, l in LEADS.items() if now - l.get("ts", 0) > (45 * 86400 if l.get("booking") else 86400)]:
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


ACTION_NAMES = {"slots": ACTION_SLOTS, "book": ACTION_BOOK, "change": ACTION_CHANGE, "cancel": ACTION_CANCEL}
ACTION_TEXT = {   # kind: (when the agent should use it, what it says meanwhile)
    "slots": ("When the caller would like to book or change an appointment and you need to know which days and times "
              "are free, or the caller asks what times are available.", "Let me check the calendar for you."),
    "book": ("When the caller has agreed to book an appointment and has chosen a specific day and time.",
             "One moment while I check that time."),
    "change": ("When the caller already has an appointment and wants to move it to a different day or time, "
               "and has told you the new day and time.", "One moment while I check the new time."),
    "cancel": ("When the caller has confirmed that they want to cancel their appointment.",
               "One moment please."),
}


def _action_params(kind: str, base: str, method: str = "GET") -> dict:
    """GET = the agent hears our answer (status + message). POST = send only, used when GHL refuses GET."""
    ref = {"name": "booking_reference", "type": "string", "example": "K7Q2M9XA",
           "description": "The booking reference given in your instructions. Always send it exactly."}
    day = {"name": "date", "type": "string", "example": "2026-10-08",
           "description": "The day as YYYY-MM-DD." + (" Leave empty to get the next free days." if kind == "slots" else "")}
    tm = {"name": "time", "type": "string", "example": "14:30", "description": "The time the caller chose, 24-hour HH:MM."}
    params = {"slots": [ref, day], "book": [ref, day, tm], "change": [ref, day, tm], "cancel": [ref]}[kind]
    api = {"url": f"{base}/api/ghl/{kind}", "method": method, "authenticationRequired": True,
           "authenticationValue": booking_secret(),
           "headers": [{"key": "X-Booking-Key", "value": booking_secret()}], "parameters": params}
    return {"triggerPrompt": ACTION_TEXT[kind][0], "triggerMessage": ACTION_TEXT[kind][1], "apiDetails": api,
            "selectedPaths": ["status", "message"] if method == "GET" else []}


async def ensure_booking_actions(base: str, force: bool = False) -> str:
    """Make sure the booking actions are on the GHL voice agent.
    Returns "reply" (agent books and hears the answer), "send" (agent can only send the request) or "" (cannot book).
    An action that is already on the agent is left alone - GHL's "update action" call fails - unless it points at
    another address, in which case it is deleted and created again."""
    if not base or not booking_enabled():
        return ""
    async with _actions_lock:
        st = STATE.setdefault("actions", {})
        now = time.time()
        ready = all(st.get(k) for k in ACTION_NAMES)
        if not force:
            if st.get("base") == base and ready and now - st.get("checked", 0) < 3600:
                return st.get("mode", "reply")
            if now - st.get("failed", 0) < 300:       # e.g. token has no permission: don't retry on every visitor
                return ""
        existing = await ghl.agent_actions()          # None = could not read the agent
        methods, errors, notes = {}, [], []
        for kind, name in ACTION_NAMES.items():
            url = f"{base}/api/ghl/{kind}"
            found = next((x for x in existing or [] if x.get("name") == name), None)
            if existing is None and st.get(kind):     # cannot read the agent: trust what was created earlier
                methods[kind] = "GET"
                continue
            if found:
                st[kind], methods[kind] = found.get("id") or st.get(kind) or "on-agent", (found.get("method") or "GET").upper()
                elsewhere = bool(found.get("url")) and found["url"] != url
                if not elsewhere:
                    continue
                if not found.get("id") or now - st.get("replaced", {}).get(kind, 0) < 6 * 3600:
                    notes.append(f"'{name}' points at {found['url']} (expected {url})")
                    continue
                st.setdefault("replaced", {})[kind] = now
                if not await ghl.delete_action(found["id"]):
                    notes.append(f"'{name}' points at {found['url']} and could not be replaced")
                    continue
            res = await ghl.save_custom_action(name, _action_params(kind, base, "GET"))
            method = "GET"
            if not res.get("id") and res.get("status") in (400, 422) and "same name" not in (res.get("error") or "").lower():
                res = await ghl.save_custom_action(name, _action_params(kind, base, "POST"))   # this account wants POST: send-only
                method = "POST"
            if res.get("id"):
                st[kind], methods[kind] = res["id"], method
                print(f"[booking] agent action '{name}' ready ({method}) -> {url}")
            elif "same name" in (res.get("error") or "").lower():       # it is there, we just could not see it
                st[kind], methods[kind] = st.get(kind) or "on-agent", "GET"
            else:
                st.pop(kind, None)
                errors.append(f"{name}: {res.get('error')}")
                print(f"[booking] could NOT add the '{name}' action to the voice agent: {res.get('error')}\n"
                      "          The token needs the scope voice-ai-agent-goals.write (Settings > Private Integrations).")
        ok = not errors
        mode = "reply" if all(m == "GET" for m in methods.values()) else "send"
        st.pop("failed", None) if ok else st.pop("checked", None)
        st.update({"base": base, "checked": now, "mode": mode, "error": "", "notes": notes} if ok
                  else {"failed": now, "error": "; ".join(errors)[:500], "notes": notes})
        if ok:
            ghl.ERRORS.pop("actions", None)
        save_state()
        return mode if ok else ""


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
        next((x for x in stages if re.search(r"appoint|book|schedul", x.get("name") or "", re.I)), None)
        or next((x for x in stages if re.search(r"meeting|call|demo", x.get("name") or "", re.I)), None)
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


def _slot_lines(by_day: dict, max_days: int = 7) -> list[str]:
    """Free times as compact lines for the agent's instructions: 'Tue 6 Oct (2026-10-06): 12:30 pm to 3 pm, 4 pm'."""
    gaps = [int((b - a).total_seconds() // 60) for ts in by_day.values() for a, b in zip(ts, ts[1:])]
    step = min([g for g in gaps if g > 0], default=30)
    lines = []
    for d, ts in list(by_day.items())[:max_days]:
        runs, start, prev = [], ts[0], ts[0]
        for t in ts[1:] + [None]:
            if t is None or (t - prev).total_seconds() // 60 != step:
                runs.append(fmt_time(start) if start == prev else f"{fmt_time(start)} to {fmt_time(prev)}")
                start = t
            prev = t
        lines.append(f"{d:%a} {d.day} {d:%b} ({d.isoformat()}): {', '.join(runs)}")
    if lines and step:
        lines.append(f"(Within a range, appointments start every {step} minutes. The last time in a range is the last start time.)")
    return lines


async def prompt_times(tz) -> list[str]:
    """The free times of the next week, read once when the visitor submits the form."""
    cfg = await booking_config()
    if not cfg["calendarId"]:
        return []
    return _slot_lines(await open_slots(cfg["calendarId"], tz, datetime.now(tz), 8) or {})


def _speak_slots(by_day: dict, max_days: int = 3, per_day: int = 3) -> str:
    return ". ".join(f"{fmt_day(d)}: {', '.join(fmt_time(t) for t in _spread(ts, per_day))}"
                     for d, ts in list(by_day.items())[:max_days])


async def _args(request: Request) -> dict:
    """What the voice agent (or the page) sent: in the address, as JSON or as a form - flat or nested.
    Names are compared without case, spaces or underscores, so bookingReference == booking_reference."""
    found: dict[str, str] = {}

    def take(obj, depth=0):
        if not isinstance(obj, dict) or depth > 4:
            return
        for k, v in obj.items():
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                found.setdefault(re.sub(r"[^a-z]", "", str(k).lower()), str(v).strip())
        for v in obj.values():
            take(v, depth + 1)

    take(dict(request.query_params))
    if request.method == "POST":
        raw = (await request.body()).decode("utf-8", "ignore")
        try:
            take(json.loads(raw))
        except ValueError:
            from urllib.parse import parse_qsl
            take(dict(parse_qsl(raw)))
    return found


def _pick(a: dict, *names: str) -> str:
    return next((a[n] for n in names if a.get(n)), "")


def _when_args(a: dict) -> tuple[str, str]:
    return (_pick(a, "date", "day", "appointmentdate", "newdate", "datetime"),
            _pick(a, "time", "appointmenttime", "newtime", "starttime"))


def _caller(request: Request, a: dict, kind: str) -> tuple[dict | None, str]:
    """Who is this for, and who is asking ("agent" or "page")? The reference in the agent's instructions names
    the lead. A caller that proves it is our GHL agent (secret key) but sent no usable reference gets the most
    recent lead - there is one shared agent, and its instructions were written for that lead."""
    secret = booking_secret()
    key_ok = a.get("key") == secret or any(secret in v for v in request.headers.values())
    source = "page" if a.get("src") == "page" else "agent"
    ref = re.sub(r"[^A-Z0-9]", "", _pick(a, "bookingreference", "ref", "reference", "bookingref").upper())
    lead = LEADS.get(ref)
    if not lead and key_ok:
        recent = [l for l in LEADS.values() if time.time() - l.get("ts", 0) < 7200]
        lead = max(recent, key=lambda l: l["ts"]) if recent else None
    if not lead and not key_ok:
        log_event(kind, source, "denied", f"no valid reference or key; sent: {', '.join(sorted(a)) or 'nothing'}")
        raise HTTPException(401, "Unknown booking reference")
    return lead, source


def _done(kind: str, source: str, lead: dict | None, res: dict, a: dict) -> dict:
    print(f"[booking] {kind} via {source} {(lead or {}).get('ref', '-')}: {res['status']} - {res['message'][:140]}")
    log_event(kind, source, res["status"], f"{res['message'][:120]} | sent: {', '.join(sorted(k for k in a if k not in ('key', 'src')))}")
    return res


NO_BOOKING = {"status": "error", "message": "I could not complete that. Apologise and say the team will contact them to arrange it."}


@app.api_route("/api/ghl/slots", methods=["GET", "POST"])
async def ghl_slots(request: Request):
    """Free appointment times. Used by the voice agent ("Check appointment times") and by the page."""
    a = await _args(request)
    lead, source = _caller(request, a, "slots")
    return _done("slots", source, lead, await slots_for(lead, _when_args(a)[0]), a)


async def slots_for(lead: dict | None, date_s: str) -> dict:
    if not booking_enabled():
        return {"status": "error", "message": "Booking is not available right now. The team will contact the caller to arrange a time."}
    tz = get_tz((lead or {}).get("tz", ""))
    now = datetime.now(tz)
    cfg = await booking_config()
    if not cfg["calendarId"]:
        return {"status": "error", "message": "The calendar is not available right now. Ask which day and time they prefer "
                                              "and say the team will confirm it."}
    day = parse_day(date_s, now.date())
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


async def _book_request(request: Request, kind: str):
    a = await _args(request)
    lead, source = _caller(request, a, kind)
    if not lead or not booking_enabled():
        return _done(kind, source, lead, dict(NO_BOOKING), a)
    date_s, time_s = _when_args(a)
    async with _book_lock:
        res = await book_for(lead, date_s, time_s, source)
    return _done(kind, source, lead, res, a)


@app.api_route("/api/ghl/book", methods=["GET", "POST"])
async def ghl_book(request: Request):
    """Book the appointment: GHL calendar event + opportunity + note. Used by the voice agent and by the page."""
    return await _book_request(request, "book")


@app.api_route("/api/ghl/change", methods=["GET", "POST"])
async def ghl_change(request: Request):
    """Move the appointment to a new day/time (books one if they had none)."""
    return await _book_request(request, "change")


@app.api_route("/api/ghl/cancel", methods=["GET", "POST"])
async def ghl_cancel(request: Request):
    """Cancel the appointment (it stays in the GHL calendar marked as cancelled) and add a note."""
    a = await _args(request)
    lead, source = _caller(request, a, "cancel")
    if not lead or not booking_enabled():
        return _done("cancel", source, lead, dict(NO_BOOKING), a)
    async with _book_lock:
        res = await cancel_for(lead)
    return _done("cancel", source, lead, res, a)


def _parse_when(value, tz) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).strip().replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)


EXACT_TIMES = True      # False when appointment times had to be read without their timezone (see ghl_appointments)


def _event_state(ev: dict) -> str:
    return (ev.get("appointmentStatus") or ev.get("appoinmentStatus") or ev.get("status") or "").lower()


async def ghl_appointments(contact_ids: set[str], tz) -> dict[str, list[tuple[datetime, dict]]] | None:
    """What GHL's calendar holds for these contacts (future, in our calendar), cancelled ones included.
    One calendar read for everybody; if the token may not read the calendar, one read per contact.
    None = GHL could not be asked."""
    global EXACT_TIMES
    cfg = await booking_config()
    if not cfg["calendarId"] or not contact_ids:
        return None
    now = datetime.now(tz)
    EXACT_TIMES = True
    events = await ghl.calendar_events(cfg["calendarId"], int((now - timedelta(hours=1)).timestamp() * 1000),
                                       int((now + timedelta(days=62)).timestamp() * 1000))
    if events is None:                                  # no calendars/events.readonly: ask per contact instead
        events, asked = [], 0
        for cid in list(contact_ids)[:8]:
            mine = await ghl.contact_appointments(cid)
            if mine is not None:
                asked += 1
                events += [{**e, "contactId": cid} for e in mine]
        if not asked:
            return None
        zone = get_tz("")                               # these times come without a timezone: assume the account's own
        EXACT_TIMES = False
    else:
        zone = tz
    out: dict[str, list] = {c: [] for c in contact_ids}
    for ev in events:
        when = _parse_when(ev.get("startTime", ""), zone)
        if (when and when > now and ev.get("id") and ev.get("contactId") in out and not ev.get("deleted")
                and ev.get("calendarId", cfg["calendarId"]) == cfg["calendarId"]):
            out[ev["contactId"]].append((when.astimezone(tz), ev))
    return out


def _booking_from(when: datetime, ev: dict) -> dict:
    return {"start": when.isoformat(), "when": f"{fmt_day(when)} at {fmt_time(when)}", "appointmentId": ev["id"],
            "calendarId": ev.get("calendarId", ""), "opportunityId": ""}


async def current_booking(lead: dict) -> dict | None:
    """The appointment this person has now: booked in this visit, in an earlier visit (same GHL contact),
    or - if this server has no record - whatever GHL shows for the contact in our calendar."""
    tz = get_tz(lead.get("tz", ""))
    now = datetime.now(tz)

    def live(b) -> bool:
        when = _parse_when((b or {}).get("start", ""), tz)
        return bool(b and b.get("appointmentId") and not b.get("cancelled") and when and when > now)

    if live(lead.get("booking")):
        return lead["booking"]
    cid = lead.get("contactId")
    if not cid:
        return None
    for other in sorted(LEADS.values(), key=lambda l: -l.get("ts", 0)):
        if other is not lead and other.get("contactId") == cid and live(other.get("booking")):
            lead["booking"] = dict(other["booking"])
            return lead["booking"]
    if (lead.get("booking") or {}).get("cancelled") or time.time() - lead.get("lookedUp", 0) < 600:
        return None                                           # we cancelled it ourselves / asked GHL a moment ago
    lead["lookedUp"] = time.time()
    found = await ghl_appointments({cid}, tz)
    upcoming = [(w, ev) for w, ev in (found or {}).get(cid, []) if _event_state(ev) not in ("cancelled", "invalid")]
    if not upcoming:
        return None
    lead["booking"] = _booking_from(*min(upcoming, key=lambda x: x[0]))
    return lead["booking"]


async def sync_bookings() -> None:
    """The chat bot books, moves and cancels straight in GHL, without telling this server. Look at the calendar
    for our recent leads: a new appointment gets its opportunity + note, and the page shows what happened."""
    if not booking_enabled():
        return
    now = time.time()
    recent = [l for l in LEADS.values() if l.get("contactId") and (now - l.get("ts", 0) < 3 * 3600 or now - l.get("seen", 0) < 180)]
    recent = sorted(recent, key=lambda l: -max(l.get("ts", 0), l.get("seen", 0)))[:15]
    if not recent:
        return
    async with _book_lock:
        newest: dict[str, dict] = {}
        for l in recent:                                   # one lead per contact: the latest visit
            newest.setdefault(l["contactId"], l)
        tz0 = get_tz("")
        found = await ghl_appointments(set(newest), tz0)
        if found is None:
            return
        cfg = await booking_config()
        for cid, lead in newest.items():
            tz = get_tz(lead.get("tz", ""))
            mine = [(w.astimezone(tz), ev) for w, ev in found.get(cid, [])]
            active = [(w, ev) for w, ev in mine if _event_state(ev) not in ("cancelled", "invalid")]
            b = lead.get("booking") or {}
            same = next(((w, ev) for w, ev in mine if ev["id"] == b.get("appointmentId")), None)
            if b.get("appointmentId") and not b.get("cancelled"):
                if same and _event_state(same[1]) in ("cancelled", "invalid"):
                    lead["booking"] = {**b, "cancelled": True}
                    log_event("cancel", "ghl", "cancelled", f"The appointment on {b.get('when')} was cancelled in GHL (chat or team)")
                    bg(_after_booking(lead, cfg, "APPOINTMENT CANCELLED (in chat or in GHL)", f"Cancelled appointment: {b.get('when', '-')}", False))
                elif same and EXACT_TIMES and _parse_when(b.get("start", ""), tz) != same[0]:
                    lead["booking"] = {**b, **_booking_from(*same), "opportunityId": b.get("opportunityId", "")}
                    log_event("change", "ghl", "rescheduled", f"Moved in GHL (chat or team) to {lead['booking']['when']}")
                    bg(_after_booking(lead, cfg, "APPOINTMENT CHANGED (in chat or in GHL)",
                                      f"New time: {lead['booking']['when']} ({tz_name(tz)})\nPrevious time: {b.get('when', '-')}"))
                if same:
                    continue
            fresh = [(w, ev) for w, ev in active if ev["id"] != b.get("appointmentId")]
            if fresh and (not b or b.get("cancelled") or not same):
                w, ev = min(fresh, key=lambda x: x[0])
                lead["booking"] = _booking_from(w, ev)
                lead.pop("tried", None)
                added = _parse_when(ev.get("dateAdded", ""), tz)
                if added and added.timestamp() < lead.get("ts", 0) - 300:      # made before this visit: nothing new to record
                    continue
                log_event("book", "ghl", "booked", f"Booked in GHL (chat bot or team): {lead['booking']['when']} ({tz_name(tz)})")
                bg(_after_booking(lead, cfg, "APPOINTMENT BOOKED (by the chat bot or directly in GHL)",
                                  f"When: {lead['booking']['when']} ({tz_name(tz)})\nCalendar: {cfg['calendarName'] or cfg['calendarId']}"))
        save_state()


async def _sync_loop() -> None:
    while True:
        await asyncio.sleep(40)
        try:
            await sync_bookings()
        except Exception as e:                              # never let the loop die
            print("[booking] sync error:", e)


@app.on_event("startup")
async def _start_sync() -> None:
    bg(_sync_loop())


# ---------------------------------------------------------------- chat bot (the widget's text chat)
# "Chat via Live Chat" and "Chat via SMS/Email" are answered by GHL's Conversation AI bot, not by the voice agent.
# We give that bot the same company knowledge on every form submission and switch on its appointment booking.
_chat_lock = asyncio.Lock()
CHAT_CHANNELS = {"live_chat": "Live_Chat", "webchat": "WebChat", "sms": "SMS", "ig": "IG", "fb": "FB", "whatsapp": "WhatsApp"}
CHAT_BOT_NAME = "AI Receptionist (Pragna AI demo)"


def chat_enabled() -> bool:
    return ghl.configured() and os.getenv("CHAT_AGENT_ENABLED", "1").lower() not in ("0", "false", "no")


async def chat_bot(force: bool = False) -> tuple[dict, str]:
    """Which Conversation AI bot answers the widget's chat: GHL_CHAT_AGENT_ID, else the account's primary bot,
    else its only bot. -> (the bot as GHL describes it, problem). ({}, "") means: no bot yet, one will be created."""
    st = STATE.setdefault("chat", {})
    want = os.getenv("GHL_CHAT_AGENT_ID", "").strip()
    if not force and st.get("id") and (not want or want == st["id"]) and time.time() - st.get("checked", 0) < 1800:
        return st.get("bot") or {"id": st["id"]}, ""
    bots = await ghl.chat_agents()
    if bots is None:
        return {}, (ghl.ERRORS.get("chat") or {}).get("message", "GHL did not answer")
    bot = (next((b for b in bots if b.get("id") == want), None) if want else
           next((b for b in bots if b.get("id") == st.get("id")), None)
           or next((b for b in bots if b.get("isPrimary")), None)
           or next((b for b in bots if b.get("name") == CHAT_BOT_NAME), None)
           or (bots[0] if len(bots) == 1 else None))
    if not bot:
        st.pop("id", None)
        st["bot"] = {}
        save_state()
        if want:
            return {}, f"GHL_CHAT_AGENT_ID={want} is not a Conversation AI bot in this account"
        return {}, ("This account has several Conversation AI bots and none is primary. Put the right bot's id in "
                    "GHL_CHAT_AGENT_ID in .env." if bots else "")
    if not st.get("backup") and bot.get("name") != CHAT_BOT_NAME:           # keep what was there before the demo used it
        full = await ghl.chat_agent(bot["id"]) or {}
        st["backup"] = {k: full.get(k) for k in ("name", "businessName", "mode", "channels", "personality", "goal", "instructions")
                        if full.get(k) is not None}
        st["backupAt"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    st.update(id=bot["id"], checked=time.time(),
              bot={k: bot.get(k) for k in ("id", "name", "mode", "channels", "isPrimary", "autoPilotMaxMessages")})
    save_state()
    return st["bot"], ""


async def update_chat_bot(data: dict, visitor: dict) -> str:
    """Write this visitor's company into the chat bot and make sure it can book. -> "updated", "off" or "error: ..."."""
    if not chat_enabled():
        return "off"
    async with _chat_lock:
        st = STATE.setdefault("chat", {})
        bot, problem = await chat_bot()
        if problem:
            st["error"] = problem
            print("[chat] chat bot not updated:", problem)
            save_state()
            return "error: " + problem
        cfg = await booking_config() if booking_enabled() else {"calendarId": ""}
        channels = {CHAT_CHANNELS.get(str(c).lower(), c) for c in (bot.get("channels") or [])} | {"Live_Chat", "WebChat"}
        res, limits = {}, [st.get("limit") or 0, 4000, 2800, 1900, 1200]     # 0 = everything; GHL may allow less
        while limits:
            limit = limits.pop(0)
            body = {**build_chat_agent(data, visitor, bool(cfg["calendarId"]), limit),
                    "businessName": data["company_name"][:80], "mode": "auto-pilot", "channels": sorted(channels),
                    "autoPilotMaxMessages": max(int(bot.get("autoPilotMaxMessages") or 0), 50)}
            if not bot:                                     # no bot in this account yet: make one for the demo
                body.update(name=CHAT_BOT_NAME, isPrimary=True, waitTime=2, waitTimeUnit="seconds", sleepEnabled=False)
            res = await ghl.save_chat_agent(bot.get("id", ""), body)
            if res.get("id"):
                st["limit"] = limit                         # remember what fitted
                break
            if res.get("status") not in (400, 413, 422):
                break
            said = [int(n) for n in re.findall(r"(\d{3,5})\s*char", res.get("error", "")) if 300 <= int(n) < (limit or 10 ** 6)]
            if said:                                        # "must be shorter than or equal to 3000 characters"
                limits = [min(said)] + [x for x in limits if x < min(said)]
        if not res.get("id"):
            st["error"] = res.get("error", "unknown error")
            print("[chat] could NOT update the chat bot:", st["error"])
            save_state()
            return "error: " + st["error"]
        if not bot:
            st.update(id=res["id"], checked=0)              # read it back next time
            print(f"[chat] created the chat bot '{CHAT_BOT_NAME}' ({res['id']})")
        st["error"], st["updated"] = "", time.time()
        # appointment booking (book + reschedule + cancel) in the chat, once
        if cfg["calendarId"] and time.time() - st.get("actionChecked", 0) > 1800:
            actions = await ghl.chat_actions(res["id"])
            if actions is not None:
                have = next((a for a in actions if a.get("type") == "appointmentBooking"), None)
                details = {"calendarId": cfg["calendarId"], "onlySendLink": False, "triggerWorkflow": False,
                           "sleepAfterBooking": False, "transferBot": False, "rescheduleEnabled": True, "cancelEnabled": True}
                d = (have or {}).get("details") or {}
                if not have:
                    made = await ghl.save_chat_action(res["id"], {"type": "appointmentBooking", "name": "Book appointment", "details": details})
                    st["action"] = made.get("id") or ""
                    st["actionError"] = made.get("error", "")
                elif d.get("onlySendLink") or not d.get("rescheduleEnabled") or not d.get("cancelEnabled"):
                    made = await ghl.save_chat_action(res["id"], {"type": "appointmentBooking", "name": have.get("name") or "Book appointment",
                                                                  "details": {**details, "calendarId": d.get("calendarId") or cfg["calendarId"]}},
                                                      have.get("id", ""))
                    st["action"], st["actionError"] = have.get("id", ""), made.get("error", "")
                else:
                    st["action"], st["actionError"] = have.get("id", ""), ""
                st["actionChecked"] = time.time()
        save_state()
        return "updated"


async def _after_booking(lead: dict, cfg: dict, headline: str, lines: str, with_opportunity: bool = True) -> None:
    """Runs after we have answered the agent (so the caller is not kept waiting): opportunity + note."""
    brand = os.getenv("BRAND_NAME", "Pragna AI")
    cid = lead.get("contactId")
    name, company = lead.get("name") or "the visitor", lead.get("company") or lead.get("website") or "their company"
    opp_line = ""
    if with_opportunity:
        opp = {}
        if cfg["pipelineId"]:
            opp = await ghl.save_opportunity(cid, f"{company} - AI voice agent demo ({name})", cfg["pipelineId"], cfg["stageId"])
        if opp.get("id"):
            if lead.get("booking"):
                lead["booking"]["opportunityId"] = opp["id"]
                save_state()
            opp_line = (f"Opportunity {'created' if opp.get('new') else 'updated'} in pipeline \"{cfg['pipelineName'] or cfg['pipelineId']}\""
                        + (f", stage \"{cfg['stageName']}\"" if cfg["stageName"] else "")) + "\n"
        else:
            opp_line = f"Opportunity NOT created ({opp.get('error') or 'no pipeline found in GHL'})\n"
            log_event("opportunity", "server", "error", opp_line.strip())
    await ghl.add_note(cid, f"{brand} voice demo - {headline}\n\n{lines}\n{opp_line}Company: {company}\nWebsite: {lead.get('website') or '-'}")


async def book_for(lead: dict, date_s: str, time_s: str, source: str = "agent") -> dict:
    """Book an appointment, or move the one they already have."""
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

    cfg = await booking_config()
    name, company = lead.get("name") or "the visitor", lead.get("company") or lead.get("website") or "their company"
    if not lead.get("contactId") and (lead.get("email") or lead.get("phone")):       # contact was not saved earlier
        lead["contactId"] = (await ghl.upsert_contact(lead, company, lead.get("website", ""))).get("id") or ""
    cid = lead.get("contactId")
    if not cid:
        return dict(NO_BOOKING)
    old = await current_booking(lead) or {}
    if old and _parse_when(old.get("start", ""), tz) == start:
        return {"status": "booked", "message": f"Already booked: {when} ({tz_name(tz)} time).", "when": when, "start": old["start"]}
    one_way = source == "agent" and STATE.get("actions", {}).get("mode") == "send"   # the agent will not hear our answer

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
            lead["tried"] = when
            if source == "agent":       # they said yes to an appointment: keep the lead visible even if no time is agreed
                bg(_after_booking(lead, cfg, "APPOINTMENT REQUESTED - NOT BOOKED (that time is not free)",
                                  f"Requested time: {when} ({tz_name(tz)})\n{alts}.\n"
                                  "If no booked appointment follows this note, please contact them to agree a time."))
            return {"status": "unavailable",
                    "message": f"NOT booked. {when} is not available. {alts}. Tell them it is not available and ask which one they would like."}
        end = (start + timedelta(minutes=cfg["slotMins"])).isoformat()
        title = f"{brand} demo call - {company} ({name})"
        desc = (f"Booked from the {brand} voice agent demo.\nName: {name}\nCompany: {company}\n"
                f"Website: {lead.get('website') or '-'}\nVisitor timezone: {tz_name(tz)}")
        if old.get("appointmentId"):                                               # they changed the time
            appt = await ghl.move_appointment(old["appointmentId"], old.get("calendarId") or cfg["calendarId"], start.isoformat(), end)
            moved = bool(appt.get("id"))
        else:
            moved = False
        if not appt.get("id"):
            appt = await ghl.create_appointment(cfg["calendarId"], cid, start.isoformat(), end, title, desc)
        if not appt.get("id"):
            if appt.get("status") in (400, 422) and re.search(r"slot|available|booked", appt.get("error", ""), re.I):
                return {"status": "unavailable", "message": f"{when} is not available. Ask for another day or time."}
            problem = f"GHL refused the appointment ({appt.get('error')})"

    if problem:                 # the opportunity is still created: they asked for an appointment, so they are a real lead
        print("[booking] appointment not created:", problem)
        bg(_after_booking(lead, cfg, "APPOINTMENT REQUESTED (not booked automatically)",
                          f"Requested time: {when} ({tz_name(tz)})\nReason: {problem}\nPlease contact them to confirm a time."))
        return {"status": "error", "message": "I could not book it in the calendar. Apologise, say their preferred time has been "
                                              f"passed to the {brand} team, who will contact them to confirm."}
    lead["booking"] = {"start": start.isoformat(), "when": when, "appointmentId": appt["id"], "calendarId": cfg["calendarId"],
                       "opportunityId": old.get("opportunityId", "")}
    lead.pop("tried", None)
    save_state()
    cal = cfg["calendarName"] or cfg["calendarId"]
    if moved:
        bg(_after_booking(lead, cfg, "APPOINTMENT CHANGED",
                          f"New time: {when} ({tz_name(tz)})\nPrevious time: {old.get('when', '-')}\nCalendar: {cal}"))
        return {"status": "rescheduled", "when": when, "start": start.isoformat(),
                "message": f"Changed. The appointment is now on {when} ({tz_name(tz)} time). Read this back to them."}
    bg(_after_booking(lead, cfg, "APPOINTMENT BOOKED", f"When: {when} ({tz_name(tz)})\nCalendar: {cal}"))
    return {"status": "booked", "when": when, "start": start.isoformat(),
            "message": f"Booked. The appointment with the {brand} team is on {when} ({tz_name(tz)} time). Read this back to them."}


async def cancel_for(lead: dict) -> dict:
    cfg = await booking_config()
    old = await current_booking(lead)
    if not old:
        return {"status": "none", "message": "There is no upcoming appointment for this caller, so there is nothing to cancel. "
                                             "Tell them, and offer to book one."}
    res = await ghl.cancel_appointment(old["appointmentId"], old.get("calendarId") or cfg["calendarId"])
    if not res.get("id"):
        bg(_after_booking(lead, cfg, "CANCELLATION REQUESTED (could not be done automatically)",
                          f"Appointment: {old.get('when', '-')}\nReason: {res.get('error')}\nPlease cancel it in the calendar.", False))
        return {"status": "error", "message": "I could not cancel it in the calendar. Apologise and say the request has been "
                                              "passed to the team, who will cancel it."}
    for l in LEADS.values():                                  # the same appointment may be remembered for an earlier visit
        if (l.get("booking") or {}).get("appointmentId") == old["appointmentId"]:
            l["booking"] = {**l["booking"], "cancelled": True}
    lead["booking"] = {**old, "cancelled": True}
    save_state()
    bg(_after_booking(lead, cfg, "APPOINTMENT CANCELLED", f"Cancelled appointment: {old.get('when', '-')}", False))
    return {"status": "cancelled", "when": old.get("when", ""),
            "message": f"Cancelled. The appointment on {old.get('when', 'that day')} has been cancelled. Tell them, and offer to book another time."}


@app.get("/api/booking")
def booking_status(ref: str = ""):
    """Lets the page show "booked / changed / cancelled" as soon as the agent has done it during the call."""
    lead = LEADS.get(re.sub(r"[^A-Z0-9]", "", ref.upper()))
    if lead:
        lead["seen"] = time.time()
    b = (lead or {}).get("booking") or {}
    return {"booked": bool(b) and not b.get("cancelled"), "cancelled": bool(b.get("cancelled")),
            "when": b.get("when", ""), "timezone": (lead or {}).get("tz", ""),
            "tried": "" if b and not b.get("cancelled") else (lead or {}).get("tried", "")}


# ---------------------------------------------------------------- booking check (what works, what GHL refused)
SCOPE_FOR = {"agent": "voice-ai-agents.readonly", "actions": "voice-ai-agent-goals.write", "calendar": "calendars.readonly",
             "appointments": "calendars/events.write", "pipelines": "opportunities.readonly",
             "opportunities": "opportunities.write", "contact-appointments": "contacts.readonly",
             "chat": "conversation-ai.readonly and conversation-ai.write", "calendar-events": "calendars/events.readonly"}
_check_cache: dict = {"ts": 0.0, "v": None}


def _ghl_problem(area: str) -> str:
    e = ghl.ERRORS.get(area)
    if not e:
        return ""
    hint = (f" -> add the scope {SCOPE_FOR[area]} to the token (GHL > Settings > Private Integrations)"
            if (e["status"] in (401, 403) or "scope" in e["message"].lower() or "authoriz" in e["message"].lower())
            and area in SCOPE_FOR else "")
    return f"GHL answered {e['message']}{hint}"


async def booking_check(base: str) -> dict:
    steps: list[dict] = []

    def step(name: str, ok, detail: str = ""):
        steps.append({"name": name, "ok": ok, "detail": detail})

    step("GHL token, location and agent are set in .env", ghl.configured(),
         "" if ghl.configured() else "Fill GHL_API_KEY, GHL_LOCATION_ID and GHL_AGENT_ID in .env and restart.")
    step("Booking is switched on", booking_enabled() or not ghl.configured(), "" if booking_enabled() else "BOOKING_ENABLED=0 in .env")
    step("Public https address GHL can call", bool(base), base or "This address is not public https. Set PUBLIC_URL in .env.")
    if not (booking_enabled() and base):
        return {"ok": False, "steps": steps, "calls": []}

    mode = await ensure_booking_actions(base, force=True)
    on_agent = await ghl.agent_actions()
    step("The server can read the voice agent", on_agent is not None,
         _ghl_problem("agent") or f"{len(on_agent or [])} action(s) on the agent")
    names = {x.get("name") for x in on_agent or []}
    missing = [n for n in ACTION_NAMES.values() if n not in names]
    step("Book / change / cancel actions are on the agent", bool(mode) and (on_agent is None or not missing),
         ((_ghl_problem("actions") or STATE.get("actions", {}).get("error")) if not mode else "")
         or ("Missing: " + ", ".join(missing) if missing else
             "; ".join(STATE.get("actions", {}).get("notes", [])) or
             "The agent hears the result of each action." if mode == "reply" else
             "GHL accepted send-only actions: the agent sends the request but does not hear the result."))

    _bcfg["ts"] = 0.0
    cfg = await booking_config()
    step("A calendar was found", bool(cfg["calendarId"]),
         cfg["calendarName"] or cfg["calendarId"] or _ghl_problem("calendar") or "No active calendar in this GHL account. Create one in Calendars.")
    if cfg["calendarId"]:
        tz = get_tz("")
        by_day = await open_slots(cfg["calendarId"], tz, datetime.now(tz), 7)
        n = sum(len(v) for v in (by_day or {}).values())
        step("The calendar has free times in the next 7 days", bool(n),
             f"{n} free times ({tz_name(tz)})" if n else _ghl_problem("calendar") or
             "No free times. Check the calendar's availability hours and that a team member is assigned.")
    step("A pipeline was found for opportunities", bool(cfg["pipelineId"]),
         (f"{cfg['pipelineName']} / stage: {cfg['stageName'] or '-'}" if cfg["pipelineId"] else
          _ghl_problem("pipelines") or "No pipeline in this GHL account. Create one in Opportunities > Pipelines."))
    for area, label in (("appointments", "Creating / changing / cancelling appointments"), ("opportunities", "Creating opportunities")):
        if ghl.ERRORS.get(area):
            step(label, False, _ghl_problem(area))

    # text chat in the widget (GHL Conversation AI bot)
    if chat_enabled():
        bot, problem = await chat_bot(force=True)
        cst = STATE.get("chat", {})
        if problem:
            step("Text chat: the chat bot can be reached", False, _ghl_problem("chat") or problem)
        elif not bot:
            step("Text chat: a chat bot exists", None, "None yet. One is created the next time the form is submitted.")
        else:
            chans = ", ".join(bot.get("channels") or []) or "-"
            step("Text chat: the chat bot is on and answers by itself",
                 str(bot.get("mode", "")).lower().replace("_", "-") == "auto-pilot" and not cst.get("error"),
                 cst.get("error") or f"{bot.get('name')} | mode: {bot.get('mode')} | channels: {chans}"
                 + (" | company text last written " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(cst["updated"])) if cst.get("updated") else
                    " | the company text is written when the form is submitted"))
            if cst.get("updated"):
                step("Text chat: the chat bot can book, change and cancel appointments", bool(cst.get("action")) and not cst.get("actionError"),
                     cst.get("actionError") or ("Appointment booking is switched on" if cst.get("action") else "Checked on the next form submission"))
        await sync_bookings()
        seen = ghl.ERRORS.get("calendar-events") or ghl.ERRORS.get("contact-appointments")
        step("Appointments made in the chat are noticed (opportunity + note)", None if seen and not EXACT_TIMES else not seen,
             (_ghl_problem("calendar-events") or _ghl_problem("contact-appointments")) if seen else
             "The calendar is read every 40 seconds for recent visitors.")

    day_ago = time.time() - 86400
    agent_calls = [e for e in EVENTS if e["source"] == "agent" and e["ts"] > day_ago]
    step("The voice agent has called this server in the last 24 hours", bool(agent_calls) or None,
         f"{len(agent_calls)} call(s); last: {agent_calls[-1]['kind']} -> {agent_calls[-1]['status']}" if agent_calls else
         "Not yet. Make a test call and ask to book an appointment. If this stays empty, the agent is not using its "
         "actions: check that the chat widget uses this agent and that the four actions are switched on in GHL.")
    calls = [{"time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(e["ts"])), "what": e["kind"], "from": e["source"],
              "result": e["status"], "detail": e["detail"]} for e in reversed(EVENTS[-25:])]
    debug = {"voiceActions": [{k: x.get(k) for k in ("name", "type", "method", "url", "keys", "paramKeys")} | {"hasId": bool(x.get("id"))}
                              for x in on_agent or []],
             "actionNotes": STATE.get("actions", {}).get("notes", []), "mode": mode,
             "chat": {k: v for k, v in STATE.get("chat", {}).items() if k in ("id", "bot", "error", "actionError", "backupAt", "updated")}}
    return {"ok": all(x["ok"] is not False for x in steps), "steps": steps, "calls": calls, "debug": debug}


@app.get("/api/booking/check")
async def booking_check_page(request: Request, loc: str = "", format: str = ""):
    """Open /api/booking/check?loc=<your GHL location id> to see why booking does or does not work."""
    from html import escape
    if not loc or loc != os.getenv("GHL_LOCATION_ID", ""):
        raise HTTPException(404, "Not found")
    if not _check_cache["v"] or time.time() - _check_cache["ts"] > 20:
        _check_cache.update(v=await booking_check(public_base(request)), ts=time.time())
    r = _check_cache["v"]
    if format == "json":
        return r
    mark = {True: ("&#10003;", "#16A34A"), False: ("&#10007;", "#DC2626"), None: ("?", "#D97706")}
    rows = "".join(f'<li><b style="color:{mark[x["ok"]][1]}">{mark[x["ok"]][0]}</b> <span>{escape(x["name"])}'
                   f'<small>{escape(x["detail"])}</small></span></li>' for x in r["steps"])
    calls = "".join(f'<tr><td>{escape(c["time"])}</td><td>{escape(c["from"])}</td><td>{escape(c["what"])}</td>'
                    f'<td>{escape(c["result"])}</td><td>{escape(c["detail"])}</td></tr>' for c in r["calls"])
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Booking check</title><style>body{{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#0F172A}}
ul{{list-style:none;padding:0}}li{{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid #E2E8F0}}li b{{font-size:18px;width:20px}}
small{{display:block;color:#64748B}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #E2E8F0;vertical-align:top}}
h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px}}</style>
<h1>Appointment booking check: {'everything needed is in place' if r['ok'] else 'something needs fixing'}</h1>
<ul>{rows}</ul><h2>Recent booking requests (newest first)</h2>
<table><tr><th>Time</th><th>From</th><th>What</th><th>Result</th><th>Detail</th></tr>{calls or '<tr><td colspan=5>None yet</td></tr>'}</table>""",
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
