"""Company website scraper - pure Python, no API keys.

Packages: httpx (fetch), BeautifulSoup + lxml (parse), trafilatura (main-content
extraction), optional Playwright (renders JavaScript-only sites).

Output: a structured company profile:
  name, description, about, services, pricing, faqs, hours, address,
  phones, emails, socials, pages_scraped
"""
import asyncio
import json
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:  # optional
    trafilatura = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_PAGES = 8
KEY_PAGES = re.compile(r"about|company|who-we-are|our-story|service|solution|product|offer|"
                       r"pricing|price|plan|package|contact|team|faq|industr|feature|why|treatment|menu", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?)\d{3,5}[\s.-]?\d{3,5}")
PRICE_RE = re.compile(r"(₹|rs\.?|inr|\$|usd|€|£|aed)\s?\d|\d\s?(₹|rs|inr|usd|/month|/mo|per month)", re.I)
HOURS_RE = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b.{0,40}\d{1,2}(:\d{2})?\s?(am|pm)|"
                      r"\b24\s?/\s?7\b|open (daily|every day)", re.I)
ADDRESS_RE = re.compile(r"\b(road|rd\.?|street|st\.?|avenue|ave|floor|suite|nagar|colony|hills|"
                        r"sector|plot|building|lane|blvd|pin|zip)\b.*\d|\d.*\b(road|street|avenue|floor|suite)\b", re.I)
ABOUT_RE = re.compile(r"\b(we are|we're|founded|since \d{4}|established|our mission|our story|"
                      r"years of experience|team of|we help|we specialize|leading)\b", re.I)
# split sentences, but not after Dr. / Mr. / No. / St. etc.
SENT_SPLIT = re.compile(r"\n|(?<!\bDr\.)(?<!\bMr\.)(?<!Mrs\.)(?<!\bMs\.)(?<!\bNo\.)(?<!\bSt\.)(?<!\bvs\.)"
                        r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")
SOCIAL = ("facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com")
SERVICE_PAGE = re.compile(r"service|solution|product|offer|treatment|feature|menu|package", re.I)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _dedupe(items, limit=None):
    seen, out = set(), []
    for i in items:
        k = re.sub(r"\W", "", i.lower())[:120]
        if k and k not in seen:
            seen.add(k)
            out.append(i)
    return out[:limit] if limit else out


# ---------------------------------------------------------------- fetching
# Full Chrome-like headers: many CDNs (Hostinger "hcdn", Cloudflare, Wix) refuse bare Python clients.
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


def _looks_like_html(ctype: str, text: str) -> bool:
    return "html" in (ctype or "").lower() or text.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))


async def _fetch(client: httpx.AsyncClient, url: str, errors: list | None = None) -> str | None:
    try:
        r = await client.get(url)
        if r.status_code == 200 and _looks_like_html(r.headers.get("content-type", ""), r.text):
            return r.text
        if errors is not None:
            errors.append(f"{url} -> HTTP {r.status_code}")
    except httpx.HTTPError as e:
        if errors is not None:
            errors.append(f"{url} -> {type(e).__name__}: {str(e)[:120]}")
    return None


async def _fetch_chrome(url: str, errors: list | None = None) -> str | None:
    """curl_cffi impersonates Chrome's TLS fingerprint - gets past most bot-blocking CDNs. pip install curl_cffi"""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        if errors is not None:
            errors.append("curl_cffi not installed")
        return None
    def _get():
        return creq.get(url, impersonate="chrome", timeout=20, allow_redirects=True)
    try:
        r = await asyncio.to_thread(_get)
        if r.status_code == 200 and _looks_like_html(r.headers.get("content-type", ""), r.text):
            return r.text
        if errors is not None:
            errors.append(f"{url} -> HTTP {r.status_code} (chrome mode)")
    except Exception as e:
        if errors is not None:
            errors.append(f"{url} -> {type(e).__name__} (chrome mode): {str(e)[:120]}")
    return None


def _url_variants(url: str) -> list[str]:
    p = urlparse(url)
    host = p.netloc
    alt = host[4:] if host.startswith("www.") else "www." + host
    path = p.path or "/"
    out = [url, f"{p.scheme}://{alt}{path}"]
    if p.scheme == "https":
        out.append(f"http://{host}{path}")
    return out


async def _render_js(url: str) -> str | None:
    """Fallback for JS-only sites (React/Wix/etc). Needs: pip install playwright && playwright install chromium"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch()
            page = await b.new_page(user_agent=UA)
            resp = await page.goto(url, wait_until="networkidle", timeout=30000)
            html = await page.content()
            await b.close()
            if resp is not None and resp.status >= 400:   # don't treat an error/block page as content
                return None
            return html
    except Exception:
        return None


# ---------------------------------------------------------------- parsing
def _json_ld(soup: BeautifulSoup) -> list[dict]:
    """schema.org data many sites embed: Organization / LocalBusiness / FAQPage / Product."""
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if "@graph" in d:
                    stack.extend(d["@graph"])
                out.append(d)
            elif isinstance(d, list):
                stack.extend(d)
    return out


def _faqs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    faqs = []
    for det in soup.find_all("details"):  # <details><summary>Q</summary>A</details>
        q = det.find("summary")
        if q:
            qt = _clean(q.get_text(" "))
            q.extract()
            faqs.append((qt, _clean(det.get_text(" "))[:400]))
    for h in soup.find_all(["h2", "h3", "h4", "h5", "strong", "dt", "button"]):  # heading ending in "?"
        qt = _clean(h.get_text(" "))
        if qt.endswith("?") and 10 < len(qt) < 200:
            nxt = h.find_next(["p", "dd", "div"])
            ans = _clean(nxt.get_text(" ")) if nxt else ""
            if 15 < len(ans) < 600 and not ans.endswith("?"):
                faqs.append((qt, ans))
    return faqs


def _blocks(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """(tag, text) for headings, paragraphs and list items in reading order."""
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    for tag in soup.select("nav, [role=navigation], .cookie, #cookie, .menu"):
        tag.decompose()
    out = []
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        if el.name == "li" and el.find(["ul", "ol"]):
            continue
        t = _clean(el.get_text(" "))
        if el.name.startswith("h") and 3 <= len(t) <= 120:
            out.append(("h", t))
        elif 20 <= len(t) <= 600:
            out.append((el.name, t))
    return out


def _services_from_page(blocks, is_service_page: bool) -> list[str]:
    """Service names = headings/list items under a 'services/what we do' heading, or anywhere on a service page."""
    items, active = [], is_service_page
    for tag, t in blocks:
        if tag == "h":
            low = t.lower()
            if re.search(r"service|what we (do|offer)|our (products|solutions|treatments|offerings)|we offer|specialit", low):
                active = True
                continue
            if active and len(t) <= 60 and not re.search(r"contact|testimonial|review|blog|news|faq|about", low):
                items.append(t)
            elif re.search(r"contact|testimonial|review|blog|news|faq|about", low):
                active = False
        elif tag == "li" and active and len(t) <= 160:
            items.append(t)
    return items


# ---------------------------------------------------------------- main
async def scrape_company(url: str, company_name: str = "") -> dict:
    url = normalize_url(url)
    base = urlparse(url)
    errors: list[str] = []
    use_chrome = False
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        home_html = None
        for candidate in _url_variants(url):
            home_html = await _fetch(client, candidate, errors)
            if not home_html:
                home_html = await _fetch_chrome(candidate, errors)
                use_chrome = bool(home_html)
            if home_html:
                url, base = candidate, urlparse(candidate)
                break
        if not home_html:
            home_html = await _render_js(url)
            if not home_html:
                errors.append("headless browser: not available or blocked "
                              "(run: python -m playwright install chromium)")
        if not home_html:
            print("[scraper] FAILED", url, "|", " ; ".join(errors))
            blocked = any(re.search(r"HTTP 40[13]|HTTP 429|HTTP 503", e) for e in errors)
            hint = (" The site is blocking automated visitors (bot protection)." if blocked
                    else " Check the address is correct and the site is online.")
            raise ValueError(f"Could not load website: {url}.{hint} [{errors[0] if errors else 'no response'}]")

        home = BeautifulSoup(home_html, "lxml")
        if len(_clean(home.get_text(" "))) < 300:  # probably a JS app shell
            rendered = await _render_js(url)
            if rendered:
                home_html, home = rendered, BeautifulSoup(rendered, "lxml")

        links = []
        for a in home.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0].split("?")[0]
            p = urlparse(href)
            if (p.netloc == base.netloc and KEY_PAGES.search(p.path) and href not in links
                    and href.rstrip("/") != url.rstrip("/") and not re.search(r"\.(pdf|jpg|png|zip)$", p.path, re.I)):
                links.append(href)
        # prioritise: about, services, pricing, contact, faq
        order = ["about", "service", "pricing", "contact", "faq"]
        links.sort(key=lambda l: next((i for i, k in enumerate(order) if k in l.lower()), 9))
        fetch_one = (lambda l: _fetch_chrome(l)) if use_chrome else (lambda l: _fetch(client, l))
        sub = await asyncio.gather(*[fetch_one(l) for l in links[:MAX_PAGES - 1]])

    pages = [(url, home_html)] + [(l, h) for l, h in zip(links, sub) if h]
    return extract_profile(pages, url, company_name)


def extract_profile(pages: list[tuple[str, str]], url: str, company_name: str = "") -> dict:
    p = {"website": url, "about": [], "services": [], "pricing": [], "faqs": [], "hours": [],
         "address": [], "emails": [], "phones": [], "socials": set(), "highlights": [],
         "pages_scraped": [], "description": "", "company_name": company_name}
    raw = ""

    for i, (page_url, html) in enumerate(pages):
        soup = BeautifulSoup(html, "lxml")
        p["pages_scraped"].append(page_url)

        if i == 0:
            def meta(n):
                t = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
                return _clean(t.get("content", "")) if t else ""
            title = _clean(soup.title.get_text()) if soup.title else ""
            p["company_name"] = (company_name or meta("og:site_name")
                                 or re.split(r"\s[|\-–:]\s", title)[0].strip() or urlparse(url).netloc)
            p["description"] = meta("description") or meta("og:description")

        # links: socials, tel:, mailto:
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if any(s in h for s in SOCIAL):
                p["socials"].add(h.split("?")[0])
            elif h.startswith("tel:"):
                raw += " " + h[4:]
            elif h.startswith("mailto:"):
                raw += " " + h[7:].split("?")[0]

        # structured data (most reliable)
        for d in _json_ld(soup):
            typ = str(d.get("@type", ""))
            if d.get("telephone"):
                raw += " " + str(d["telephone"])
            if d.get("email"):
                p["emails"].append(str(d["email"]).replace("mailto:", ""))
            addr = d.get("address")
            if isinstance(addr, dict):
                p["address"].append(", ".join(str(addr[k]) for k in ("streetAddress", "addressLocality",
                                    "addressRegion", "postalCode", "addressCountry") if addr.get(k)))
            elif isinstance(addr, str):
                p["address"].append(addr)
            oh = d.get("openingHours")
            if oh:
                p["hours"].extend(oh if isinstance(oh, list) else [oh])
            if d.get("priceRange"):
                p["pricing"].append(f"Price range: {d['priceRange']}")
            if typ in ("Organization", "LocalBusiness") or "Business" in typ:
                if d.get("description") and not p["description"]:
                    p["description"] = _clean(d["description"])
                for s in d.get("sameAs", []) if isinstance(d.get("sameAs"), list) else []:
                    p["socials"].add(s)
            if typ == "FAQPage":
                for q in d.get("mainEntity", []):
                    ans = q.get("acceptedAnswer", {})
                    a_txt = BeautifulSoup(str(ans.get("text", "")), "lxml").get_text(" ")
                    p["faqs"].append((_clean(q.get("name", "")), _clean(a_txt)[:400]))
            if typ in ("Service", "Product") and d.get("name"):
                p["services"].append(_clean(d["name"]))

        p["faqs"].extend(_faqs(BeautifulSoup(html, "lxml")))
        footer_text = " ".join(_clean(f.get_text(" ")) for f in soup.find_all(["footer", "address"]))
        raw += " " + soup.get_text(" ")

        blocks = _blocks(soup)
        is_service = bool(SERVICE_PAGE.search(urlparse(page_url).path))
        p["services"].extend(_services_from_page(blocks, is_service))

        # main readable text (trafilatura strips menus/ads/boilerplate)
        main = trafilatura.extract(html, include_comments=False, favor_precision=True) if trafilatura else None
        sentences = [s.lstrip("-•* ").strip() for s in SENT_SPLIT.split(main or "")]
        sentences = sentences or [t for tag, t in blocks if tag == "p"]

        is_about = "about" in page_url.lower() or "story" in page_url.lower()
        for s in sentences:
            s = _clean(s)
            if PRICE_RE.search(s):
                p["pricing"].append(s)
            if HOURS_RE.search(s) and len(s) < 160:
                p["hours"].append(s)
            if is_about or ABOUT_RE.search(s):
                p["about"].append(s)
            elif i == 0:
                p["highlights"].append(s)

        for t in re.split(r"\s{2,}|\|", footer_text):
            t = _clean(t)
            if ADDRESS_RE.search(t) and 15 < len(t) < 200 and "@" not in t:
                p["address"].append(t)
            if HOURS_RE.search(t) and len(t) < 160:
                p["hours"].append(t)

    # contact details
    p["emails"] += [e for e in EMAIL_RE.findall(raw)
                    if not re.search(r"\.(png|jpg|jpeg|webp|svg|gif)$|sentry|example\.com|wixpress", e, re.I)]
    seen_digits = set()
    for ph in PHONE_RE.findall(raw):
        d = re.sub(r"\D", "", ph)
        if 10 <= len(d) <= 13 and d[-10:] not in seen_digits and not re.fullmatch(r"(19|20)\d{8,}", d):
            seen_digits.add(d[-10:])
            p["phones"].append(ph.strip())

    # tidy
    p["about"] = _dedupe(p["about"], 10)
    p["highlights"] = _dedupe(p["highlights"], 10)
    p["services"] = _dedupe([s for s in p["services"] if len(s) > 3], 25)
    p["pricing"] = _dedupe(p["pricing"], 12)
    p["hours"] = _dedupe(p["hours"], 4)
    p["address"] = _dedupe(p["address"], 2)
    p["emails"] = _dedupe(p["emails"], 4)
    p["phones"] = p["phones"][:3]
    p["socials"] = sorted(p["socials"])[:6]
    faq_seen, faqs = set(), []
    for q, a in p["faqs"]:
        if q and a and q.lower() not in faq_seen:
            faq_seen.add(q.lower())
            faqs.append({"q": q, "a": a})
    p["faqs"] = faqs[:15]
    return p


def profile_text(p: dict, max_chars: int = 9000) -> str:
    """Structured company knowledge block for the voice agent prompt."""
    def sec(title, items, bullet="- "):
        return f"## {title}\n" + "\n".join(bullet + i for i in items) + "\n" if items else ""

    out = [f"Company: {p['company_name']}", f"Website: {p['website']}"]
    if p["description"]:
        out.append(f"Summary: {p['description']}")
    text = "\n".join(out) + "\n\n"
    text += sec("About us", p["about"])
    text += sec("Products / services", p["services"])
    text += sec("Prices mentioned on the site", p["pricing"])
    text += sec("Key highlights", p["highlights"])
    text += sec("Opening hours", p["hours"])
    text += sec("Address", p["address"])
    contact = ([f"Phone: {x}" for x in p["phones"]] + [f"Email: {x}" for x in p["emails"]])
    text += sec("Contact", contact)
    if p["faqs"]:
        text += "## FAQs\n" + "\n".join(f"Q: {f['q']}\nA: {f['a']}" for f in p["faqs"]) + "\n"
    return text[:max_chars]


# ---------------------------------------------------------------- mobile preview
async def mobile_screenshot(url: str, timeout_s: int = 20) -> str | None:
    """Phone-sized screenshot of the company site (base64 JPEG) for the mobile preview.
    Uses the same headless Chrome as _render_js. Returns None if unavailable."""
    import base64
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    async def _shot():
        async with async_playwright() as p:
            b = await p.chromium.launch()
            ctx = await b.new_context(
                viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                has_touch=True, user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"))
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                await page.wait_for_timeout(800)
                img = await page.screenshot(type="jpeg", quality=60,
                                            clip={"x": 0, "y": 0, "width": 390, "height": 1600},
                                            full_page=True)
            finally:
                await b.close()
            return "data:image/jpeg;base64," + base64.b64encode(img).decode()

    try:
        return await asyncio.wait_for(_shot(), timeout_s + 10)
    except Exception as e:
        print("[screenshot] skipped:", type(e).__name__, str(e)[:120])
        return None
