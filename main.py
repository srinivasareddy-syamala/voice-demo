"""Voice AI demo: form -> scrape company site -> train GHL Voice AI agent -> 'Try your voice agent'."""
import asyncio
import os
import time

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(".env")           # if you create one, it wins
load_dotenv(".env.example")   # otherwise use the values in .env.example

import ghl  # noqa: E402
from persona import build_agent_prompt, build_welcome  # noqa: E402
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
    }


@app.post("/api/train")
async def train(req: TrainRequest):
    t0 = time.time()
    if not req.consent:
        raise HTTPException(400, "Please tick the consent box to continue.")
    visitor = req.model_dump()
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

    prompt = build_agent_prompt(data, visitor)
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
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
