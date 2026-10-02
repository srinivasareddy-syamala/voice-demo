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


def thum_prefix() -> str:
    """thum.io screenshot URL prefix; the site URL is appended to it.
    Free tier = desktop layout. With a paid key (THUM_IO_AUTH=<id>-<secret>) we ask for a phone-width render."""
    auth = os.getenv("THUM_IO_AUTH", "").strip()
    if auth:
        return f"https://image.thum.io/get/auth/{auth}/width/600/crop/1300/viewportWidth/420/noanimate/"
    return "https://image.thum.io/get/width/600/crop/1500/noanimate/"


FRAME_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Preview</title>
<style>
  html,body{margin:0;background:#fff;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  html{scrollbar-width:none} ::-webkit-scrollbar{display:none}
  #shot{width:100%;display:block}
  #wait{padding:40px 16px;text-align:center;color:#64748B;font-size:13px}
  #fallback{display:none;min-height:100vh;background:linear-gradient(135deg,#2563EB,#45C9FD);color:#fff;
            flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:24px;text-align:center}
  #fallback h1{margin:0;font-size:22px} #fallback p{margin:0;opacity:.9;font-size:13px}
</style></head>
<body>
<div id="wait">Loading your website…</div>
<img id="shot" alt="" style="display:none">
<div id="fallback"><h1>__NAME__</h1><p>__HOST__</p></div>
<script>
  // 1) their website: thum.io screenshot -> our own screenshot -> simple branded card
  (function () {
    var img = document.getElementById("shot"), wait = document.getElementById("wait");
    var own = ""; try { own = sessionStorage.getItem("pv_shot") || ""; } catch (e) {}
    var tried = false;
    function show() { wait.style.display = "none"; img.style.display = "block"; }
    function fail() {
      if (!tried && own) { tried = true; img.src = own; return; }
      wait.style.display = "none"; img.style.display = "none";
      document.getElementById("fallback").style.display = "flex";
    }
    img.onload = function () { if (img.naturalWidth < 50) return fail(); show(); };
    img.onerror = fail;
    img.src = "__THUM__";
    setTimeout(function () { if (img.style.display === "none" && !tried) fail(); }, 20000);
  })();
  // 2) tell the page outside the phone what the voice call is doing
  (function () {
    var orig = window.fetch;
    window.fetch = function () {
      var args = arguments, url = String((args[0] && args[0].url) || args[0] || "");
      return orig.apply(this, args).then(function (res) {
        if (url.indexOf("start-voice-ai-call") !== -1) {
          var noAgent = /\\/undefined$/.test(url);
          parent.postMessage({type: "voice", ok: res.status < 400, status: res.status, noAgent: noAgent}, "*");
        }
        return res;
      });
    };
  })();
  // 3) open the chat widget automatically so the options are visible straight away
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
def preview_frame(site: str = "", name: str = ""):
    """The page shown INSIDE the phone: a screenshot of the visitor's website with the GHL chat widget on top."""
    from html import escape
    from urllib.parse import urlparse
    p = urlparse(site.strip())
    if p.scheme not in ("http", "https") or not p.netloc or any(ch in site for ch in '"<> \\\n\r'):
        raise HTTPException(400, "Invalid website address")
    html = (FRAME_HTML
            .replace("__THUM__", escape(thum_prefix() + site.strip(), quote=True))
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
        "theme": theme,
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
