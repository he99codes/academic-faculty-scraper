"""
Domain Resolver Module.
Resolves institution name to its official canonical domain and homepage URL
using credential-free search engines (DuckDuckGo, Bing) with Playwright fallback.
"""

import asyncio
import base64
import logging
import re
from typing import List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

# Setup module logger
logger = logging.getLogger("domain_resolver")

# Known non-official aggregator and social media domains to ignore
AGGREGATOR_BLOCKLIST: Set[str] = {
    "wikipedia.org", "en.wikipedia.org", "m.wikipedia.org",
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "reddit.com", "quora.com",
    "usnews.com", "topuniversities.com", "timeshighereducation.com",
    "shiksha.com", "collegedunia.com", "niche.com", "edurank.org",
    "glassdoor.com", "indeed.com", "britannica.com", "crunchbase.com",
    "coursera.org", "edx.org", "udemy.com", "medium.com",
    "github.com", "researchgate.net", "academia.edu", "google.com",
    "bing.com", "duckduckgo.com", "yahoo.com"
}

# TLDs strongly associated with academic and higher education institutions globally
ACADEMIC_TLDS: Set[str] = {
    ".edu", ".ac.uk", ".ac.in", ".edu.in", ".ac.jp", ".edu.au",
    ".ac.za", ".edu.cn", ".ac.kr", ".edu.sg", ".edu.pk", ".ac.nz",
    ".ac.at", ".ac.be", ".ac.cr", ".ac.il", ".ac.th", ".edu.hk",
    ".edu.my", ".edu.ph", ".edu.tr", ".edu.tw", ".edu.vn"
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def is_aggregator(url: str) -> bool:
    """Checks if the given URL belongs to an aggregator, social network, or directory."""
    try:
        domain = urlparse(url).netloc.lower()
        # Remove leading www. or m.
        domain = re.sub(r"^(www\.|m\.)", "", domain)
        for blocked in AGGREGATOR_BLOCKLIST:
            if domain == blocked or domain.endswith("." + blocked):
                return True
        return False
    except Exception:
        return True


def decode_bing_url(url: str) -> str:
    """Decodes Bing tracking/redirect URLs (ck/a?...) into destination target URL."""
    try:
        parsed = urlparse(url)
        if "bing.com" in parsed.netloc and "/ck/a" in parsed.path:
            qs = parse_qs(parsed.query)
            u_param = qs.get("u", [""])[0]
            if u_param.startswith("a1"):
                b64 = u_param[2:]
                b64 += "=" * (-len(b64) % 4)
                return base64.urlsafe_b64decode(b64).decode("utf-8", errors="ignore")
    except Exception:
        pass
    return url


def score_domain_match(url: str, institution_name: str, rank_position: int = 0) -> float:
    """
    Scores how likely a domain is the official home of the given institution.
    Considers academic TLDs, keyword containment, composite abbreviations, and search position.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    score = 0.0

    # Search engine organic ranking bonus (top search results are highest quality)
    score += max(0.0, 30.0 - rank_position * 5.0)

    # Academic TLD bonuses
    for tld in ACADEMIC_TLDS:
        if domain.endswith(tld):
            score += 40.0
            break
    if domain.endswith(".edu") or ".ac." in domain or ".edu." in domain:
        score += 30.0

    # Subdomain depth penalty for main institutional portal
    clean_host = domain.replace("www.", "")
    host_parts = clean_host.split(".")
    if len(host_parts) > 3:  # e.g. facultyaffairs.iiti.ac.in
        score -= 25.0

    # Extract clean alphanumeric tokens from institution name
    tokens = [t.lower() for t in re.findall(r"\b[A-Za-z0-9]+\b", institution_name)]
    tokens_filtered = [t for t in tokens if t not in {"of", "the", "and", "for", "in", "at", "university", "institute", "college", "school"}]

    # Match tokens in domain
    domain_clean = re.sub(r"[^a-z0-9]", "", domain)
    for token in tokens_filtered:
        if len(token) >= 3 and token in domain_clean:
            score += 25.0

    # Composite acronym matching: e.g. 'iit' + 'b' -> 'iitb', 'eth' + 'z' -> 'ethz'
    if len(tokens) >= 2:
        comp = (tokens[0] + tokens[1][0]).lower()
        if comp in domain_clean:
            score += 40.0

        acronym = "".join(t[0] for t in tokens).lower()
        if len(acronym) >= 3 and acronym in domain_clean:
            score += 35.0

    # Penalty for deep non-root paths (official homepages are almost always root / or /en)
    if len(path.strip("/").split("/")) > 2:
        score -= 15.0

    return score


class DomainResolver:
    """
    Resolves institution names to canonical official domains via multi-engine headless search.
    """

    def __init__(self, headless: bool = True, timeout: int = 15):
        self.headless = headless
        self.timeout = timeout

    async def search_duckduckgo_http(self, query: str) -> List[str]:
        """Performs a fast, lightweight search via DuckDuckGo HTML without browser overhead."""
        urls: List[str] = []
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        endpoints = [
            ("https://html.duckduckgo.com/html/", {"q": query}),
            ("https://lite.duckduckgo.com/lite/", {"q": query})
        ]

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for url, data in endpoints:
                try:
                    resp = await client.post(url, data=data, headers=headers)
                    if resp.status_code == 200 and resp.text:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for a in soup.select(".result__url, .result-link, a.result__snippet"):
                            href = a.get("href")
                            if href and href.startswith("http"):
                                urls.append(href)
                            elif href and "/l/?uddg=" in href:
                                # DDG click wrapper
                                parsed = urlparse(href)
                                qs = parse_qs(parsed.query)
                                actual_url = qs.get("uddg", [""])[0]
                                if actual_url:
                                    urls.append(actual_url)
                        if urls:
                            break
                except Exception as e:
                    logger.debug(f"DDG HTTP search error on {url}: {e}")
        return urls

    async def search_playwright(self, query: str) -> List[str]:
        """
        Headless browser search (Bing / DuckDuckGo) with stealth masking.
        Used as high-reliability fallback when simple HTTP search is throttled.
        """
        urls: List[str] = []
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)
                context = await browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    viewport={"width": 1280, "height": 800}
                )
                page = await context.new_page()

                # Search Bing
                search_url = f"https://www.bing.com/search?q={httpx.URL('', params={'q': query}).params['q']}"
                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    raw_links = await page.eval_on_selector_all(
                        "li.b_algo h2 a",
                        "elements => elements.map(e => e.href)"
                    )
                    for link in raw_links:
                        decoded = decode_bing_url(link)
                        if decoded.startswith("http"):
                            urls.append(decoded)
                except Exception as e:
                    logger.debug(f"Playwright Bing search failed: {e}")

                await context.close()
                await browser.close()
        except Exception as e:
            logger.warning(f"Playwright search failed: {e}")
        return urls

    async def resolve_canonical_url(self, raw_url: str) -> Tuple[str, str]:
        """
        Follows HTTP redirects to determine the true canonical domain and final landing URL.
        """
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
                resp = await client.get(raw_url)
                final_url = str(resp.url)
        except Exception:
            final_url = raw_url

        parsed = urlparse(final_url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        canonical_base = f"{parsed.scheme}://{parsed.netloc}"
        return domain, canonical_base

    async def resolve(self, institution_name: str) -> Optional[Tuple[str, str]]:
        """
        Public resolver method:
        Given an institution name, returns (canonical_domain, homepage_url).
        """
        logger.info(f"Resolving official domain for: '{institution_name}'")
        queries = [
            f"{institution_name} official website",
            f"{institution_name} site:.edu OR site:.ac.*",
        ]

        candidate_urls: List[str] = []

        # 1. First attempt: Fast HTTP-based search
        for q in queries:
            results = await self.search_duckduckgo_http(q)
            for r in results:
                if not is_aggregator(r):
                    candidate_urls.append(r)
            if candidate_urls:
                break

        # 2. Second attempt: Headless browser fallback if HTTP returned no valid candidates
        if not candidate_urls:
            logger.info("HTTP search produced no valid candidates. Falling back to Playwright headless search...")
            for q in queries:
                results = await self.search_playwright(q)
                for r in results:
                    if not is_aggregator(r):
                        candidate_urls.append(r)
                if candidate_urls:
                    break

        if not candidate_urls:
            logger.error(f"Could not resolve official domain for '{institution_name}'")
            return None

        # 3. Score and rank candidates
        scored_candidates = []
        for pos, url in enumerate(candidate_urls):
            score = score_domain_match(url, institution_name, rank_position=pos)
            scored_candidates.append((score, url))

        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        best_url = scored_candidates[0][1]
        logger.info(f"Top candidate URL selected: {best_url} (Score: {scored_candidates[0][0]})")

        # 4. Resolve redirects and get canonical base
        canonical_domain, canonical_base = await self.resolve_canonical_url(best_url)
        logger.info(f"Resolved canonical domain: {canonical_domain} ({canonical_base})")
        return canonical_domain, canonical_base
