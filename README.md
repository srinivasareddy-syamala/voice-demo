# Pragna AI — Free AI Voice Agent Preview (GoHighLevel)

Landing → **Try it free** → form → scrape company website → fill the GHL Voice AI agent's prompt
and first message → mobile company view → **Talk with Agent** → microphone on → GHL voice widget
call starts → agent talks as that company's AI agent.

## Why the greeting showed "Hi , I'm 's AI Agent"
Calls through the GHL **website voice widget are anonymous**: no contact is attached, so contact
merge fields (Contact Full Name, Company Name, Company Website…) come out empty.
This app fixes that: right before the visitor talks, the backend writes the **finished** prompt and
greeting (real names filled in) into the agent via the API. Nothing needs merge fields any more.

## Files
| File | What it does |
|---|---|
| `main.py` | FastAPI: `POST /api/train`, `GET /api/config`, serves the page |
| `scraper.py` | httpx + BeautifulSoup + trafilatura (+ optional Playwright). No paid APIs |
| `prompt_template.txt` | **Your eApps Global prompt**, with `{company_name}`, `{contact_name}`, `{company_brief}`… slots. Edit wording here |
| `persona.py` | Fills the template + welcome message (`Hi Srinivas, I'm Acme's AI Agent from eApps Global — thanks for trying the free preview. Got a minute?`), and cleans scraped text so it can't break out of `<company_brief>` |
| `ghl.py` | `PATCH /voice-ai/agents/{id}` (only `agentPrompt` + `welcomeMessage`), `POST /contacts/upsert` |
| `static/index.html` | Mobile-first page: landing, bottom-sheet form, progress, company card, sticky **Talk with Agent** |

## GHL setup
1. Your Voice AI widget is already wired in: `data-widget-id="6abf5919cb9ce9d3ea4df163"`
   (default in code; override with `GHL_WIDGET_ID`). Don't paste the `<script>` on the page yourself:
   the page loads it **after** the agent has been trained, so the first call already has the new prompt.
2. Make sure that widget is linked to the agent whose ID you put in `GHL_AGENT_ID`
   (AI Agents → Voice AI → open agent → ID in the URL).
3. Settings → Private Integrations → create token (free) with `voice-ai-agents.write`, `voice-ai-agents.readonly`, `contacts.write`
   → `GHL_API_KEY`. Location ID → `GHL_LOCATION_ID`.
4. Optional: the agent's phone number → `GHL_AGENT_PHONE` (tel: fallback if the widget can't load).

## Run on Windows with a public HTTPS link (testing)
Double-click **`start_public.bat`**. It creates a virtualenv, installs packages, opens `.env`
for your GHL values (first run), starts the server, and opens a free **Cloudflare Quick Tunnel**
(no account). The public `https://xxxx.trycloudflare.com` link is printed, copied to the clipboard
and opened in the browser. Open it on your phone to test **Talk with Agent** (mic needs HTTPS, the
tunnel provides it). The link changes on every run; close the SERVER and TUNNEL windows to stop.

## Run manually
```bash
pip install -r requirements.txt
playwright install chromium   # optional: JavaScript-heavy sites
cp .env.example .env          # put GHL_API_KEY, GHL_LOCATION_ID, GHL_AGENT_ID in .env (never commit .env)
uvicorn main:app --host 0.0.0.0 --port 8000
```
Deploy behind **HTTPS** (Render / Railway / VPS + Nginx). Phones only allow the microphone on HTTPS.
Without GHL keys it runs in demo mode: scraping and the company view work, but the agent isn't updated.

## Appointment booking (calendar + opportunity)
During the demo call the agent asks **"Would you like to book an appointment with the Pragna AI team?"** as soon
as the visitor sounds interested. If they say yes it offers free times and books one. The same thing is available
as a **Book an appointment** button next to the phone.

What gets created in GHL:
- an **appointment** in the calendar (linked to the contact saved from the form),
- an **opportunity** in the pipeline (`<Company> - AI voice agent demo (<Name>)`, status open),
- a **note** on the contact with the time, calendar and pipeline stage.

How it works: on the first form submission the server adds two *custom actions* to the Voice AI agent
(`Check appointment times`, `Book appointment`; see AI Agents → Voice AI → agent → Actions). They call
`/api/ghl/slots` and `/api/ghl/book` on this server, so the server must be on a public **https** address
(`PUBLIC_URL` in `.env`, or simply the address the page is opened on). On `localhost` only the button works.

Setup:
1. Add these scopes to the Private Integration token: `voice-ai-agent-goals.write`, `calendars.readonly`,
   `calendars/events.write`, `opportunities.readonly`, `opportunities.write`.
2. Have at least one active **calendar** (with availability) and one **pipeline** in the sub-account.
   The app picks the first active calendar / first pipeline, preferring names with "demo" and a stage with
   "appointment" or "booked". To choose exactly, set `GHL_CALENDAR_ID`, `GHL_PIPELINE_ID`, `GHL_PIPELINE_STAGE_ID`.
3. Restart. The log prints `[booking] calendar: … | pipeline: … / stage: …` and any missing permission.

## Talk button behaviour
1. Asks for microphone permission inside the tap (needed on iPhone Safari / Chrome mobile).
2. Opens the GHL widget (`window.leadConnector.chatWidget.openWidget()`).
3. Looks for the widget's start-call / mic button and presses it, so the call starts by itself.
   If GHL changes its widget layout, the visitor sees "Tap the microphone button in the agent window".
GHL widget limits: iOS 16+, Chrome/Edge on mobile; max 20 calls at the same time; calls are not recorded.

## Limits
- **One shared agent**: each new form submission replaces the prompt. Two visitors submitting within
  the same minute: the second overwrites the first. Fine for a pilot. For volume, use a pool of
  agents + widgets and hand each visitor a free one.
- Sites behind Cloudflare bot protection may refuse to load; the form shows an error.
