"""Fill the prompt template + welcome message with real values.

Why: GHL website voice-widget calls are anonymous (no contact attached), so contact
merge fields like {{contact.name}} render empty ("Hi , I'm 's AI Agent").
We therefore write the finished text straight into the agent before each demo call.
"""
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from scraper import profile_text

TEMPLATE = Path(__file__).with_name("prompt_template.txt").read_text(encoding="utf-8")
WELCOME = "Hi {first_name}, I'm {company_name}'s AI Agent from {brand_name} — thanks for trying the free preview. Got a minute?"

# Action names as they appear on the agent in GHL (AI Agents > Voice AI > agent > Actions).
ACTION_SLOTS = "Check appointment times"
ACTION_BOOK = "Book appointment"
ACTION_CHANGE = "Change appointment time"
ACTION_CANCEL = "Cancel appointment"

_ASK = ('- As soon as {contact_name} sounds interested (asks about price, setup, how to get this on their own site, next steps, '
        'or says they like it), and in any case before the call ends, ask: "Would you like to book an appointment with the '
        '{brand_name} team to get this set up for {company_name}?"')
_RULES = """- {existing}
- All times are in {contact_name}'s timezone, {timezone}. Always send the date as YYYY-MM-DD and the time as 24-hour HH:MM.
- Never ask for their name, email or phone number — they already gave these in the form.
- If they say no, accept it politely and carry on. Ask once, do not push.
- Today is {today}. Dates for the coming days: {dates}."""

# The agent can really book, change and cancel: our server registered four actions on it (see main.py)
# and the agent hears the server's answer.
BOOKING_ON = """Appointments — important. You can book, change and cancel appointments yourself with your actions:
""" + _ASK + """
- If they say yes, ask which day and time suits them. To suggest free times, use the "{action_slots}" action, then offer two or three of the times it returns.
- To BOOK: when they choose a day and time, use the "{action_book}" action with booking_reference "{booking_ref}", the date and the time.
- To CHANGE an appointment they already have: ask for the new day and time, then use the "{action_change}" action with booking_reference "{booking_ref}", the new date and the new time.
- To CANCEL: first check ("Just to confirm, you'd like to cancel your appointment?"). When they confirm, use the "{action_cancel}" action with booking_reference "{booking_ref}", then offer to book another time.
- Say an appointment is booked, changed or cancelled ONLY after the action replies with status "booked", "rescheduled" or "cancelled", and read the day and time back from its reply. If it replies that a time is not available, offer the alternative times it gives and use the action again with the one they pick. If it replies with an error, apologise and say the {brand_name} team will contact them.
""" + _RULES

# Same actions, but this GHL account only lets them SEND data (the agent does not hear an answer).
BOOKING_SEND = """Appointments — important. You can send booking requests with your actions:
""" + _ASK + """
- If they say yes, ask which weekday and time (office hours) suits them.
- To BOOK: use the "{action_book}" action with booking_reference "{booking_ref}", the date and the time.
- To CHANGE an appointment: ask for the new day and time, then use the "{action_change}" action with booking_reference "{booking_ref}", the new date and the new time.
- To CANCEL: first check that they really want to cancel, then use the "{action_cancel}" action with booking_reference "{booking_ref}".
- The actions do not answer you. After using one, say: "I've sent that through to the {brand_name} calendar — the team will confirm it with you." Never say it is confirmed.
""" + _RULES

# Booking is not connected (GHL not configured, or this server has no public address): take a preference only.
BOOKING_OFF = """Booking an appointment:
""" + _ASK + """
- If yes, ask which day and time suits them and say the {brand_name} team will confirm it with them by email or phone. You cannot book it yourself, so never say that it is booked or confirmed.
- Ask once, do not push."""


def _booking_section(p: dict, visitor: dict, booking: dict | None) -> str:
    common = dict(contact_name=_safe(visitor.get("name") or "the visitor"), company_name=_safe(p["company_name"]),
                  brand_name=os.getenv("BRAND_NAME", "Pragna AI"))
    if not booking:
        return BOOKING_OFF.format(**common)
    now: datetime = booking["now"]                       # already in the visitor's timezone
    days = [now + timedelta(days=i) for i in range(1, 11)]
    existing = (f"{common['contact_name']} already has an appointment on {_safe(booking['existing'])}."
                if booking.get("existing") else f"{common['contact_name']} has no appointment booked yet.")
    return (BOOKING_ON if booking.get("replies", True) else BOOKING_SEND).format(
        **common, action_slots=ACTION_SLOTS, action_book=ACTION_BOOK, action_change=ACTION_CHANGE,
        action_cancel=ACTION_CANCEL, booking_ref=booking["ref"], timezone=_safe(booking["tz"]), existing=existing,
        today=f"{now:%A} {now.day} {now:%B %Y} ({now:%Y-%m-%d})",
        dates=", ".join(f"{d:%a} {d.day} {d:%b} = {d:%Y-%m-%d}" for d in days))


def _safe(text: str) -> str:
    """Scraped text must never close/open our <company_brief> tag or inject braces."""
    text = re.sub(r"</?\s*company_brief\s*>", "", text or "", flags=re.I)
    return text.replace("<", "(").replace(">", ")").replace("{", "(").replace("}", ")")


def build_agent_prompt(p: dict, visitor: dict, booking: dict | None = None) -> str:
    brief = profile_text(p).split("\n", 2)[-1].strip()  # drop the Company/Website header lines
    description = p.get("description") or (p["about"][0] if p.get("about") else "") or "(not found on site)"
    return TEMPLATE.format(
        company_name=_safe(p["company_name"]),
        company_website=p["website"],
        company_brief=_safe(brief) or "(the website scrape returned almost no usable text)",
        company_description=_safe(description),
        contact_name=_safe(visitor.get("name") or "the visitor"),
        brand_name=os.getenv("BRAND_NAME", "Pragna AI"),
        booking_section=_booking_section(p, visitor, booking),
    )


def build_welcome(p: dict, visitor: dict) -> str:
    first = _safe((visitor.get("name") or "there").split(" ")[0])
    company = _safe(p["company_name"])
    brand = os.getenv("BRAND_NAME", "Pragna AI")
    msg = WELCOME.format(first_name=first, company_name=company, brand_name=brand)
    if len(msg) > 190:  # GHL limit
        msg = WELCOME.format(first_name=first, company_name=company[:190 - len(msg) + len(company) - 1].rstrip() + "…",
                             brand_name=brand)
    return msg[:190]
