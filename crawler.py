"""
Crawler & Discovery Module.
Discovers faculty directories, leadership pages, and departmental rosters
via homepage heuristic scoring and secondary targeted search dorks.
Handles dynamic JavaScript rendering, lazy-loading scroll, and pagination using Playwright.
"""

import asyncio
import logging
import random
import re
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger("crawler")

# Directory and leadership keywords with associated confidence scores
DIRECTORY_KEYWORDS = {
    # High confidence direct faculty rosters
    "faculty directory": 100,
    "faculty-directory": 100,
    "faculty_directory": 100,
    "our faculty": 95,
    "our-faculty": 95,
    "faculty members": 95,
    "faculty roster": 95,
    "professoren": 95,       # German
    "fakultaet": 95,          # German
    "corpo docente": 95,      # Italian/Portuguese
    "corpo-docente": 95,
    "profesores": 95,         # Spanish
    "enseignants": 95,        # French
    "professors": 90,
    "faculty": 85,

    # Key leadership and governance
    "leadership": 85,
    "academic leadership": 90,
    "heads of department": 90,
    "department heads": 90,
    "deans": 85,
    "administration": 75,
    "hod": 80,
    "chairs": 80,

    # General people directories
    "people": 70,
    "directory": 70,
    "academics": 60,
    "departments": 60,
    "schools": 55,
    "staff": 50,
}

# Slugs or file extensions to ignore
EXCLUDED_PATTERNS = [
    r"\.(pdf|jpg|jpeg|png|gif|svg|webp|zip|docx?|xlsx?|mp4|mp3)$",
]

# Non-academic subdomains to skip
EXCLUDED_SUBDOMAINS = {"news", "events", "sports", "athletics", "alumni", "giving", "press", "magazine", "shop", "store", "jobs", "careers", "hr", "visit", "uit"}

# Keywords that indicate non-faculty employee/internal portals
BANNED_KEYWORDS = {
    "gateway", "portal", "employee", "benefits", "payroll", "helpdesk",
    "parking", "visit", "emergency", "alumni", "giving", "athletics",
    "sports", "login", "signin", "logout", "auth", "register", "cart",
    "checkout", "privacy", "terms", "cookie", "disclaimer", "copyright",
    "accessibility", "stories", "calendar", "press-releases", "whois",
    "stanfordwho", "directory-view"
}


def is_valid_directory_url(url: str, base_domain: str) -> bool:
    """Verifies that a URL belongs to the institution domain and isn't an excluded asset."""
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http"):
        return False

    url_domain = parsed.netloc.lower()
    # Accept same domain or subdomains (e.g. cs.stanford.edu under stanford.edu)
    if not (url_domain == base_domain or url_domain.endswith("." + base_domain)):
        return False

    # Check for excluded subdomains (e.g. news.stanford.edu)
    sub_parts = url_domain.replace(base_domain, "").strip(".").split(".")
    for part in sub_parts:
        if part in EXCLUDED_SUBDOMAINS:
            return False

    path_lower = parsed.path.lower()
    full_str = f"{url_domain} {path_lower}"
    for kw in BANNED_KEYWORDS:
        if kw in full_str:
            return False

    for pattern in EXCLUDED_PATTERNS:
        if re.search(pattern, path_lower):
            return False

    return True


def score_link(href: str, anchor_text: str) -> int:
    """Computes a relevance score for a link based on its URL slug and anchor text."""
    combined = f"{href} {anchor_text}".lower()
    score = 0

    for kw, weight in DIRECTORY_KEYWORDS.items():
        if kw in combined:
            score = max(score, weight)

    # Massive bonus for dedicated directories and profile subdomains
    if "profiles." in href.lower():
        score += 80
    if "/faculty-directory" in href.lower() or "/faculty_directory" in href.lower():
        score += 70
    if "/directory" in href.lower() or "directory" in anchor_text.lower():
        score += 60
    if "/people" in href.lower() or "/faculty" in href.lower():
        score += 50
    if "our faculty" in anchor_text.lower() or "faculty roster" in anchor_text.lower():
        score += 50
    if "leadership" in anchor_text.lower() or "dean" in anchor_text.lower():
        score += 40

    return score


class AdaptiveCrawler:
    """
    Crawls an educational institution's website to discover directory and faculty pages,
    rendering dynamic SPAs with Playwright and extracting cleaned DOM content.
    """

    def __init__(
        self,
        headless: bool = True,
        max_directories: int = 5,
        page_timeout: int = 20,
        stealth_enabled: bool = True
    ):
        self.headless = headless
        self.max_directories = max_directories
        self.page_timeout = page_timeout
        self.stealth_enabled = stealth_enabled
        self.stealth = Stealth() if stealth_enabled else None

    async def _create_context(self, browser: Browser) -> BrowserContext:
        """Creates a browser context with realistic desktop headers and viewport."""
        return await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            has_touch=False,
            is_mobile=False,
        )

    async def discover_candidate_directories(
        self,
        base_domain: str,
        homepage_url: str
    ) -> List[str]:
        """
        Explores the homepage and navigations to discover faculty directory URLs.
        Falls back to targeted site-specific search queries if few direct links are found.
        """
        logger.info(f"Initiating adaptive directory discovery for domain: {base_domain}")
        candidate_scores: dict[str, int] = {}

        # 1. Fetch homepage and extract navigation menu links
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)
                context = await self._create_context(browser)
                page = await context.new_page()
                if self.stealth:
                    await self.stealth.apply_stealth_async(page)

                logger.info(f"Navigating to homepage: {homepage_url}")
                await page.goto(homepage_url, wait_until="domcontentloaded", timeout=self.page_timeout * 1000)
                await asyncio.sleep(random.uniform(1.0, 2.0))

                # Extract all <a> tags with text and href
                raw_links = await page.eval_on_selector_all(
                    "a[href]",
                    """elements => elements.map(e => ({
                        href: e.href,
                        text: (e.textContent || '').trim(),
                        aria: e.getAttribute('aria-label') || ''
                    }))"""
                )

                for link_item in raw_links:
                    href = link_item.get("href", "")
                    text = f"{link_item.get('text', '')} {link_item.get('aria', '')}".strip()

                    full_url = urljoin(homepage_url, href)
                    if is_valid_directory_url(full_url, base_domain):
                        score = score_link(full_url, text)
                        if score >= 60:
                            candidate_scores[full_url] = max(candidate_scores.get(full_url, 0), score)

                await context.close()
                await browser.close()
        except Exception as e:
            logger.warning(f"Error exploring homepage navigation: {e}")

        # 2. Targeted Search Discovery to find specialized directories and subdomains
        logger.info(f"Executing targeted search discovery for {base_domain} directories...")
        search_queries = [
            f"site:{base_domain} faculty directory",
            f"site:{base_domain} people faculty",
            f"site:{base_domain} leadership \"head of department\" OR \"dean\"",
        ]
        try:
            from domain_resolver import DomainResolver
            resolver = DomainResolver(headless=self.headless)
            for q in search_queries:
                search_results = await resolver.search_duckduckgo_http(q)
                if not search_results:
                    search_results = await resolver.search_playwright(q)

                for res_url in search_results:
                    if is_valid_directory_url(res_url, base_domain):
                        score = score_link(res_url, "") + 30  # Strong search intent bonus
                        candidate_scores[res_url] = max(candidate_scores.get(res_url, 0), score)
        except Exception as e:
            logger.warning(f"Targeted search discovery failed: {e}")

        # Expand sub-directories for profile portals (e.g. profiles.stanford.edu -> /browse/...)
        for cand_url in list(candidate_scores.keys()):
            if "profiles." in cand_url and cand_url.rstrip("/").count("/") <= 3:
                for sub in ["browse/school-of-engineering", "browse/school-of-medicine", "browse/graduate-school-of-business"]:
                    sub_url = urljoin(cand_url if cand_url.endswith("/") else cand_url + "/", sub)
                    candidate_scores[sub_url] = 120

        # Rank candidates by score descending
        sorted_candidates = sorted(candidate_scores.items(), key=lambda x: x[1], reverse=True)
        top_urls = [url for url, _ in sorted_candidates[:self.max_directories]]

        logger.info(f"Discovered {len(top_urls)} top directory candidate pages: {top_urls}")
        return top_urls

    async def render_and_harvest_page(
        self,
        url: str,
        max_scrolls: int = 4
    ) -> Tuple[str, str]:
        """
        Renders a directory page using Playwright:
        - Simulates mouse scroll for lazy-loading/infinite scroll.
        - Returns cleaned HTML content and the canonical landing URL.
        """
        logger.info(f"Rendering directory page: {url}")
        rendered_html = ""
        final_url = url

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await self._create_context(browser)
            page = await context.new_page()

            if self.stealth:
                await self.stealth.apply_stealth_async(page)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout * 1000)
                final_url = page.url

                # Human-like delay jitter
                await asyncio.sleep(random.uniform(1.2, 2.5))

                # Simulate incremental scrolls to trigger lazy loading
                for scroll_idx in range(max_scrolls):
                    await page.evaluate(f"window.scrollBy(0, window.innerHeight * 0.8);")
                    await asyncio.sleep(random.uniform(0.6, 1.2))

                # Check for "Load More" buttons or next pagination
                load_more_selectors = [
                    "button:has-text('Load More')",
                    "button:has-text('Show More')",
                    "a:has-text('Load More')",
                    ".load-more",
                ]
                for selector in load_more_selectors:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=1000):
                            logger.info(f"Found dynamic button '{selector}', clicking...")
                            await btn.click()
                            await asyncio.sleep(1.5)
                            break
                    except Exception:
                        pass

                # Get DOM content
                raw_html = await page.content()

                # Clean DOM: remove script, style, svg, noscript tags to reduce noise
                soup = BeautifulSoup(raw_html, "html.parser")
                for tag in soup(["script", "style", "noscript", "svg", "meta", "iframe"]):
                    tag.decompose()

                rendered_html = str(soup)

            except Exception as e:
                logger.error(f"Failed rendering {url}: {e}")
            finally:
                await context.close()
                await browser.close()

        return rendered_html, final_url
