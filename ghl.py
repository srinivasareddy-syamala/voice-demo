"""GoHighLevel (LeadConnector) API v2 client: Voice AI agent patch + contact upsert."""
import os

import httpx

BASE = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com")


def _headers(version: str) -> dict:
    return {
        "Authorization": f"Bearer {os.environ['GHL_API_KEY']}",  # Private Integration token
        "Version": version,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def configured() -> bool:
    return all(os.getenv(k) for k in ("GHL_API_KEY", "GHL_LOCATION_ID", "GHL_AGENT_ID"))


async def update_voice_agent(prompt: str, welcome: str) -> dict:
    """PATCH /voice-ai/agents/{agentId}  (scope: voice-ai-agents.write).
    Only prompt + first message change; name, voice, widget etc. stay as set in GHL."""
    url = f"{BASE}/voice-ai/agents/{os.environ['GHL_AGENT_ID']}"
    body = {"agentPrompt": prompt, "welcomeMessage": welcome}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(url, params={"locationId": os.environ["GHL_LOCATION_ID"]},
                          headers=_headers(os.getenv("GHL_VOICE_API_VERSION", "v3")), json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"GHL agent update failed [{r.status_code}]: {r.text[:400]}")
        return r.json()


async def upsert_contact(visitor: dict, company: dict) -> dict | None:
    """POST /contacts/upsert (scope: contacts.write). Best effort - never blocks the demo."""
    if not (visitor.get("email") or visitor.get("phone")):
        return None
    first, _, last = (visitor.get("name") or "").partition(" ")
    body = {
        "locationId": os.environ["GHL_LOCATION_ID"],
        "firstName": first, "lastName": last,
        "email": visitor.get("email") or None,
        "phone": visitor.get("phone") or None,
        "companyName": company["company_name"],
        "website": company["website"],
        "source": "Voice AI Demo Page",
        "tags": ["voice-ai-demo"],
    }
    body = {k: v for k, v in body.items() if v}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{BASE}/contacts/upsert", headers=_headers("2021-07-28"), json=body)
            return r.json() if r.status_code < 400 else {"error": r.text[:300]}
    except httpx.HTTPError as e:
        return {"error": str(e)}
