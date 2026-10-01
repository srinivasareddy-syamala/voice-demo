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
from scraper import mobile_screenshot, scrape_company  # noqa: E402

app = FastAPI(title="Voice AI Agent Demo")
print("=" * 60)
print("GHL agent updates:", "ON" if ghl.configured() else
      "OFF (DEMO MODE) - fill GHL_API_KEY, GHL_LOCATION_ID, GHL_AGENT_ID in .env")
print("Widget ID:", os.getenv("GHL_WIDGET_ID", "6abdf6d0b9739b959264b321"))
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


@app.get("/api/config")
def config():
    """Public config the frontend needs to open the GHL voice widget / call the agent."""
    return {
        "widgetId": os.getenv("GHL_WIDGET_ID", "6abdf6d0b9739b959264b321"),
        "agentPhone": os.getenv("GHL_AGENT_PHONE", ""),
        "ghlConfigured": ghl.configured(),
    }


@app.post("/api/train")
async def train(req: TrainRequest):
    t0 = time.time()
    try:
        data = await scrape_company(req.website, req.company)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not (data["about"] or data["services"] or data["highlights"] or data["description"]):
        raise HTTPException(422, "Website loaded but no readable text was found. "
                                 "If it is a JavaScript site, install Playwright on the server.")

    visitor = req.model_dump()
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
        return await ghl.upsert_contact(visitor, data) if ghl.configured() else None

    agent_status, contact, shot = await asyncio.gather(
        update_agent(), save_contact(), mobile_screenshot(data["website"]))

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
        "screenshot": shot,
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
