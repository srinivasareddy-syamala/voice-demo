"""Voice AI demo: form -> scrape company site -> train GHL Voice AI agent -> 'Try your voice agent'."""
import asyncio
import os
import time

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(".env")           # if you create one, it wins
load_dotenv(".env.example")   # otherwise use the values in .env.example

import ghl  # noqa: E402
from persona import build_agent_prompt, build_welcome  # noqa: E402
from scraper import mobile_preview, scrape_company  # noqa: E402

DEFAULT_WIDGET_ID = "6abf5919cb9ce9d3ea4df163"
app = FastAPI(title="Voice AI Agent Demo")
print("=" * 60)
print("GHL agent updates:", "ON" if ghl.configured() else
      "OFF (DEMO MODE) - fill GHL_API_KEY, GHL_LOCATION_ID, GHL_AGENT_ID in .env")
print("Widget ID:", os.getenv("GHL_WIDGET_ID", DEFAULT_WIDGET_ID))
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


@app.get("/api/config")
def config():
    """Public config the frontend needs to open the GHL voice widget / call the agent."""
    return {
        "widgetId": os.getenv("GHL_WIDGET_ID", DEFAULT_WIDGET_ID),
        "whatsapp": os.getenv("WHATSAPP_NUMBER", "+44 7446 952720"),
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
