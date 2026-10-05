"""GoHighLevel (LeadConnector) API v2 client: Voice AI agent + its actions, contacts, calendar, opportunities."""
import os
import time

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


# ---------------------------------------------------------------- appointments + opportunities
CAL_VERSION = "2021-04-15"
# Last failure per kind of request (cleared when that kind works again). Shown on the booking check page.
ERRORS: dict[str, dict] = {}
AREAS = (("/voice-ai/actions", "actions"), ("/voice-ai/agents", "agent"), ("/calendars/events", "appointments"),
         ("/calendars", "calendar"), ("/opportunities/pipelines", "pipelines"), ("/opportunities", "opportunities"),
         ("/contacts", "contacts"))


def _area(path: str) -> str:
    if path.startswith("/contacts") and path.endswith("/appointments"):
        return "contact-appointments"
    return next((name for prefix, name in AREAS if path.startswith(prefix)), "other")


async def _call(method: str, path: str, version: str = "2021-07-28", **kw) -> tuple[int, dict]:
    """One GHL request. Never raises: returns (status, json) - status 0 means the request itself failed."""
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.request(method, f"{BASE}{path}", headers=_headers(version), **kw)
        try:
            j = r.json()
        except ValueError:
            j = {"message": r.text[:300]}
        j = j if isinstance(j, dict) else {"data": j}
        if r.status_code >= 400:
            print(f"[ghl] {method} {path} -> {r.status_code}: {str(j)[:300]}")
            ERRORS[_area(path)] = {"status": r.status_code, "message": _err(r.status_code, j), "ts": time.time()}
        else:
            ERRORS.pop(_area(path), None)
        return r.status_code, j
    except httpx.HTTPError as e:
        print(f"[ghl] {method} {path} error: {e}")
        ERRORS[_area(path)] = {"status": 0, "message": f"could not reach GHL: {e}", "ts": time.time()}
        return 0, {"message": str(e)}


def _err(status: int, j: dict) -> str:
    m = j.get("message") or j.get("error") or j
    return f"{status}: {m if isinstance(m, str) else '; '.join(map(str, m)) if isinstance(m, list) else m}"[:300]


async def list_calendars() -> list[dict]:
    """GET /calendars/ (scope: calendars.readonly)"""
    s, j = await _call("GET", "/calendars/", CAL_VERSION, params={"locationId": os.environ["GHL_LOCATION_ID"]})
    return j.get("calendars") or [] if s == 200 else []


async def free_slots(calendar_id: str, start_ms: int, end_ms: int, tz: str) -> dict | None:
    """GET /calendars/{id}/free-slots (scope: calendars.readonly) -> {"2026-10-07": ["2026-10-07T10:00:00+01:00", ...]}.
    None when GHL could not be asked (missing scope, network)."""
    s, j = await _call("GET", f"/calendars/{calendar_id}/free-slots", CAL_VERSION,
                       params={"startDate": start_ms, "endDate": end_ms, "timezone": tz})
    if s != 200:
        return None
    return {d: v.get("slots") or [] for d, v in j.items() if isinstance(v, dict) and "slots" in v}


async def create_appointment(calendar_id: str, contact_id: str, start_iso: str, end_iso: str | None,
                             title: str, description: str = "") -> dict:
    """POST /calendars/events/appointments (scope: calendars/events.write) -> {"id"} or {"error"}."""
    body = {"calendarId": calendar_id, "locationId": os.environ["GHL_LOCATION_ID"], "contactId": contact_id,
            "startTime": start_iso, "title": title[:200], "appointmentStatus": "confirmed",
            "description": description[:1500], "toNotify": True}
    if end_iso:
        body["endTime"] = end_iso
    s, j = await _call("POST", "/calendars/events/appointments", CAL_VERSION, json=body)
    if s in (200, 201):
        return {"id": j.get("id") or (j.get("appointment") or {}).get("id") or ""}
    return {"error": _err(s, j), "status": s}


async def move_appointment(event_id: str, calendar_id: str, start_iso: str, end_iso: str | None) -> dict:
    """PUT /calendars/events/appointments/{id} - the visitor changed their mind about the time."""
    body = {"calendarId": calendar_id, "startTime": start_iso, "appointmentStatus": "confirmed", "toNotify": True}
    if end_iso:
        body["endTime"] = end_iso
    s, j = await _call("PUT", f"/calendars/events/appointments/{event_id}", CAL_VERSION, json=body)
    return {"id": event_id} if s == 200 else {"error": _err(s, j), "status": s}


async def cancel_appointment(event_id: str, calendar_id: str) -> dict:
    """PUT /calendars/events/appointments/{id} with status cancelled - it stays visible in GHL as cancelled."""
    s, j = await _call("PUT", f"/calendars/events/appointments/{event_id}", CAL_VERSION,
                       json={"calendarId": calendar_id, "appointmentStatus": "cancelled", "toNotify": True})
    return {"id": event_id} if s == 200 else {"error": _err(s, j), "status": s}


async def contact_appointments(contact_id: str) -> list[dict]:
    """GET /contacts/{id}/appointments (scope: contacts.readonly) - finds an appointment booked in an earlier visit."""
    s, j = await _call("GET", f"/contacts/{contact_id}/appointments")
    return j.get("events") or [] if s == 200 else []


async def list_pipelines() -> list[dict]:
    """GET /opportunities/pipelines (scope: opportunities.readonly)"""
    s, j = await _call("GET", "/opportunities/pipelines", params={"locationId": os.environ["GHL_LOCATION_ID"]})
    return j.get("pipelines") or [] if s == 200 else []


async def save_opportunity(contact_id: str, name: str, pipeline_id: str, stage_id: str = "") -> dict:
    """POST /opportunities/ (scope: opportunities.write). If this contact already has one in the pipeline
    (GHL can forbid duplicates) the existing one is moved to the stage instead. -> {"id","new"} or {"error"}."""
    loc = os.environ["GHL_LOCATION_ID"]
    body = {"pipelineId": pipeline_id, "locationId": loc, "name": name[:200], "status": "open", "contactId": contact_id}
    if stage_id:
        body["pipelineStageId"] = stage_id
    s, j = await _call("POST", "/opportunities/", json=body)
    if s in (200, 201):
        return {"id": (j.get("opportunity") or {}).get("id") or j.get("id") or "", "new": True}
    first_error = _err(s, j)
    s2, found = await _call("GET", "/opportunities/search",
                            params={"location_id": loc, "contact_id": contact_id, "pipeline_id": pipeline_id, "limit": 1})
    opp = (found.get("opportunities") or [None])[0] if s2 == 200 else None
    if not opp:
        return {"error": first_error}
    upd = {"name": name[:200], "status": "open", "pipelineId": pipeline_id}
    if stage_id:
        upd["pipelineStageId"] = stage_id
    s3, j3 = await _call("PUT", f"/opportunities/{opp['id']}", json=upd)
    return {"id": opp["id"], "new": False} if s3 == 200 else {"error": _err(s3, j3)}


# ---------------------------------------------------------------- voice agent actions (tools the agent can use in a call)
def _voice_version() -> str:
    return os.getenv("GHL_VOICE_API_VERSION", "v3")


async def agent_actions() -> list[dict] | None:
    """Actions already on the agent (GET /voice-ai/agents/{id}, scope voice-ai-agents.readonly). None = could not read."""
    s, j = await _call("GET", f"/voice-ai/agents/{os.environ['GHL_AGENT_ID']}", _voice_version(),
                       params={"locationId": os.environ["GHL_LOCATION_ID"]})
    if s != 200:
        return None
    return [{"id": a.get("id") or a.get("_id"), "name": a.get("name"), "type": a.get("actionType"),
             "url": ((a.get("actionParameters") or {}).get("apiDetails") or {}).get("url"),
             "method": ((a.get("actionParameters") or {}).get("apiDetails") or {}).get("method"),
             "say": (a.get("actionParameters") or {}).get("triggerMessage")}
            for a in (j.get("actions") or []) if isinstance(a, dict)]


async def save_custom_action(name: str, params: dict, action_id: str = "") -> dict:
    """POST /voice-ai/actions or PUT /voice-ai/actions/{id} (scope: voice-ai-agent-goals.write).
    A custom action lets the voice agent call our server during a call. -> {"id"} or {"error"}."""
    body = {"agentId": os.environ["GHL_AGENT_ID"], "locationId": os.environ["GHL_LOCATION_ID"],
            "actionType": "CUSTOM_ACTION", "name": name, "actionParameters": params}
    method, path = ("PUT", f"/voice-ai/actions/{action_id}") if action_id else ("POST", "/voice-ai/actions")
    ver = _voice_version()
    s, j = await _call(method, path, ver, json=body)
    if s in (400, 422) and "version" in str(j).lower() and ver != CAL_VERSION:
        ver = CAL_VERSION
        s, j = await _call(method, path, ver, json=body)
    if s == 404 and action_id:              # it was deleted in GHL -> make it again
        method, path = "POST", "/voice-ai/actions"
        s, j = await _call(method, path, ver, json=body)
    if s == 400 and action_id and "same name" in str(j).lower():      # GHL refuses to update in place: replace it
        d, _ = await _call("DELETE", f"/voice-ai/actions/{action_id}", ver,
                           params={"locationId": os.environ["GHL_LOCATION_ID"], "agentId": os.environ["GHL_AGENT_ID"]})
        if d in (200, 204):
            method, path = "POST", "/voice-ai/actions"
            s, j = await _call(method, path, ver, json=body)
    if s >= 400 and "selectedPaths" in str(j):   # older field name used in GHL's own examples
        p2 = dict(params)
        p2["responsePathsToExtract"] = p2.pop("selectedPaths", [])
        s, j = await _call(method, path, ver, json={**body, "actionParameters": p2})
    if s in (200, 201):
        return {"id": j.get("id") or j.get("_id") or (j.get("action") or {}).get("id") or action_id}
    return {"error": _err(s, j), "status": s}
