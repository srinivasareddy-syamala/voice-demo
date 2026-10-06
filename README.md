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

## Appointments: book, change, cancel (calendar + opportunity)
During the demo call the agent asks **"Would you like to book an appointment with the Pragna AI team?"** as soon
as the visitor sounds interested. It can then **book**, **change the time** and **cancel**. The same is available
from the **Book an appointment / Change time** button next to the phone, and the page follows what the agent does.

What happens in GHL:
- **Book**: an appointment in the calendar (linked to the contact from the form), an opportunity in the pipeline
  (`<Company> - AI voice agent demo (<Name>)`, status open) and a note on the contact.
- **Change**: the same appointment is moved to the new time; note "APPOINTMENT CHANGED".
- **Cancel**: the appointment is marked cancelled (it stays visible in the calendar); note "APPOINTMENT CANCELLED".
  The opportunity is left open so the team can follow up.
- A returning visitor (same email/phone) can change or cancel the appointment made in an earlier visit.

How it works: the server adds four *custom actions* to the Voice AI agent (AI Agents → Voice AI → agent → Actions):
`Check appointment times`, `Book appointment`, `Change appointment time`, `Cancel appointment`. They call
`/api/ghl/slots`, `/api/ghl/book`, `/api/ghl/change`, `/api/ghl/cancel` on this server, so it must be on a public
**https** address (`PUBLIC_URL` in `.env`, or the address the page is opened on). On `localhost` only the button works.

**Check page** — open `https://<your address>/api/booking/check?loc=<GHL_LOCATION_ID>`. It shows a tick or cross
for every requirement (token permissions, actions on the agent, calendar, free times, pipeline) with GHL's own
error text, and a list of the latest booking requests the agent or the page made and their results.

Setup:
1. Add these scopes to the Private Integration token: `voice-ai-agent-goals.write`, `calendars.readonly`,
   `calendars/events.write`, `opportunities.readonly`, `opportunities.write`, `contacts.readonly`.
2. Have at least one active **calendar** (with availability and a team member) and one **pipeline**.
   The app picks the first active calendar / first pipeline, preferring names with "demo" and a stage with
   "appointment" or "booked". To choose exactly, set `GHL_CALENDAR_ID`, `GHL_PIPELINE_ID`, `GHL_PIPELINE_STAGE_ID`.
3. Restart, submit the form once, then open the check page.

## Several visitors at once: lines and the queue
A **line** is one GHL voice agent with its own chat widget. Each visitor is given a free line, their company is
written into that line's agent, and their phone preview loads that line's widget, so two companies can be shown
at the same time without being mixed up. Two lines are built in (`DEFAULT_LINES` in `main.py`); list more in
`GHL_LINES=agentId:widgetId,…` after duplicating the agent and the widget in GHL (the new widget must use the new agent).

When every line is in use the visitor sees **"Please wait, the line is busy"** with their place in the queue and is
connected automatically, first come first served. A line becomes free when its visitor closes the page, when the
page has been silent for 90 seconds, or - only if somebody is waiting - after `LINE_HOLD_SECONDS` (7 minutes).
The visitor who lost the line sees "Your demo session has ended" with a Start again button.
The lead is saved in GHL as soon as the form is sent, even if the visitor gives up waiting.

The text chat bot is shared by all lines (GHL has one primary chat bot), so it knows the most recent visitor's company.

## Text chat in the widget
"Chat via Live Chat" and "Chat via SMS/Email" are answered by GHL's **Conversation AI bot**, a different bot from the
Voice AI agent. On every form submission the app also writes the visitor's company into that bot, sets it to
**auto-pilot** on Live Chat + Web Chat and switches on its **appointment booking** action (book, reschedule, cancel)
on the same calendar. It uses `GHL_CHAT_AGENT_ID`, else the account's primary bot, else it creates one.
The bot's earlier settings are saved once in `.demo_state.json` (`chat.backup`). Turn this off with `CHAT_AGENT_ENABLED=0`.

Note: this bot answers **every** web chat in the sub-account, as the latest visitor's company.

The chat bot books straight into GHL, so the server reads the calendar every 40 seconds for recent visitors: a new
appointment gets its opportunity and note, and the result page shows it. This needs the visitor to use the same
email or phone in the chat as in the form.

Extra token scopes: `conversation-ai.readonly`, `conversation-ai.write`, `calendars/events.readonly`.

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
