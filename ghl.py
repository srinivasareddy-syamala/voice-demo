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


async def upsert_contact(visitor: dict, company_name: str = "", website: str = "",
                         tags: list[str] | None = None) -> dict:
    """POST /contacts/upsert (scope: contacts.write). Saves the form data as a GHL contact.
    Returns {"id": ..., "new": bool} or {"error": ...}. Never raises."""
    if not (visitor.get("email") or visitor.get("phone")):
        return {"error": "no email or phone"}
    first, _, last = (visitor.get("name") or "").strip().partition(" ")
    body = {
        "locationId": os.environ["GHL_LOCATION_ID"],
        "firstName": first, "lastName": last,
        "name": (visitor.get("name") or "").strip(),
        "email": (visitor.get("email") or "").strip() or None,
        "phone": (visitor.get("phone") or "").strip() or None,
        "companyName": company_name or visitor.get("company") or None,
        "website": website or visitor.get("website") or None,
        "source": "Pragna AI Voice Demo",
        "tags": tags or ["voice-ai-demo"],
    }
    body = {k: v for k, v in body.items() if v}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{BASE}/contacts/upsert", headers=_headers("2021-07-28"), json=body)
            if r.status_code >= 400 and "phone" in body and "phone" in r.text.lower():
                # GHL rejects badly formatted phone numbers -> save without it (phone goes in the note)
                body.pop("phone")
                r = await c.post(f"{BASE}/contacts/upsert", headers=_headers("2021-07-28"), json=body)
            if r.status_code >= 400:
                print("[ghl] contact save failed", r.status_code, r.text[:300])
                return {"error": f"{r.status_code}: {r.text[:300]}"}
            j = r.json()
            cid = (j.get("contact") or {}).get("id")
            print("[ghl] contact saved", cid, "(new)" if j.get("new") else "(updated)")
            return {"id": cid, "new": j.get("new", False)}
    except httpx.HTTPError as e:
        print("[ghl] contact save error", e)
        return {"error": str(e)}


async def add_note(contact_id: str, text: str) -> bool:
    """POST /contacts/{id}/notes (scope: contacts.write). Best effort."""
    if not contact_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{BASE}/contacts/{contact_id}/notes", headers=_headers("2021-07-28"),
                             json={"body": text[:4900]})
            if r.status_code >= 400:
                print("[ghl] note failed", r.status_code, r.text[:200])
            return r.status_code < 400
    except httpx.HTTPError as e:
        print("[ghl] note error", e)
        return False
