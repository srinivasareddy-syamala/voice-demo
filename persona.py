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

# The agent can really book: our server registered two actions on it (see main.py).
BOOKING_ON = """Booking an appointment — important:
- As soon as {contact_name} sounds interested (asks about price, setup, how to get this on their own site, next steps, or says they like it), and in any case before the call ends, ask: "Would you like to book an appointment with the {brand_name} team to get this set up for {company_name}?"
- If they say yes, ask which day and time suits them. To suggest free times, use the "{action_slots}" action, then offer two or three of the times it returns.
- When they choose a day and time, use the "{action_book}" action with booking_reference "{booking_ref}", date as YYYY-MM-DD and time as 24-hour HH:MM. All times are in {contact_name}'s timezone, {timezone}.
- Say the appointment is booked ONLY after the action replies with status "booked", and read the day and time back from its reply. If it replies that the time is not available, offer the alternative times it gives and book the one they pick. If it replies with an error, apologise and say the {brand_name} team will contact them to arrange a time.
- Never ask for their name, email or phone number — they already gave these in the form.
- If they say no, accept it politely and carry on. Ask once, do not push.
- Today is {today}. Dates for the coming days: {dates}."""

# Booking is not connected (GHL not configured, or this server has no public address): take a preference only.
BOOKING_OFF = """Booking an appointment:
- When {contact_name} sounds interested, and in any case before the call ends, ask: "Would you like to book an appointment with the {brand_name} team to get this set up for {company_name}?"
- If yes, ask which day and time suits them and say the {brand_name} team will confirm it with them by email or phone. You cannot book it yourself, so never say that it is booked or confirmed.
- Ask once, do not push."""


def _booking_section(p: dict, visitor: dict, booking: dict | None) -> str:
    common = dict(contact_name=_safe(visitor.get("name") or "the visitor"), company_name=_safe(p["company_name"]),
                  brand_name=os.getenv("BRAND_NAME", "Pragna AI"))
    if not booking:
        return BOOKING_OFF.format(**common)
    now: datetime = booking["now"]                       # already in the visitor's timezone
    days = [now + timedelta(days=i) for i in range(1, 11)]
    return BOOKING_ON.format(
        **common, action_slots=ACTION_SLOTS, action_book=ACTION_BOOK,
        booking_ref=booking["ref"], timezone=_safe(booking["tz"]),
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
