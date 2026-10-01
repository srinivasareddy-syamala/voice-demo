"""Fill the eApps Global prompt template + welcome message with real values.

Why: GHL website voice-widget calls are anonymous (no contact attached), so contact
merge fields like {{contact.name}} render empty ("Hi , I'm 's AI Agent").
We therefore write the finished text straight into the agent before each demo call.
"""
import os
import re
from pathlib import Path

from scraper import profile_text

TEMPLATE = Path(__file__).with_name("prompt_template.txt").read_text(encoding="utf-8")
WELCOME = "Hi {first_name}, I'm {company_name}'s AI Agent from {brand_name} — thanks for trying the free preview. Got a minute?"


def _safe(text: str) -> str:
    """Scraped text must never close/open our <company_brief> tag or inject braces."""
    text = re.sub(r"</?\s*company_brief\s*>", "", text or "", flags=re.I)
    return text.replace("<", "(").replace(">", ")").replace("{", "(").replace("}", ")")


def build_agent_prompt(p: dict, visitor: dict) -> str:
    brief = profile_text(p).split("\n", 2)[-1].strip()  # drop the Company/Website header lines
    description = p.get("description") or (p["about"][0] if p.get("about") else "") or "(not found on site)"
    return TEMPLATE.format(
        company_name=_safe(p["company_name"]),
        company_website=p["website"],
        company_brief=_safe(brief) or "(the website scrape returned almost no usable text)",
        company_description=_safe(description),
        contact_name=_safe(visitor.get("name") or "the visitor"),
        brand_name=os.getenv("BRAND_NAME", "eApps Global"),
    )


def build_welcome(p: dict, visitor: dict) -> str:
    first = _safe((visitor.get("name") or "there").split(" ")[0])
    company = _safe(p["company_name"])
    brand = os.getenv("BRAND_NAME", "eApps Global")
    msg = WELCOME.format(first_name=first, company_name=company, brand_name=brand)
    if len(msg) > 190:  # GHL limit
        msg = WELCOME.format(first_name=first, company_name=company[:190 - len(msg) + len(company) - 1].rstrip() + "…",
                             brand_name=brand)
    return msg[:190]
