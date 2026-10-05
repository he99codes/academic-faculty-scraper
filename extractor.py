"""
Extractor Module.
Autonomous two-tier DOM clustering and heuristic extraction for academic faculty & leadership.
Extracts names, designations, departments, profile URLs, emails, and bios without hardcoded CSS selectors.
"""

import logging
import re
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from models import FacultyMember

logger = logging.getLogger("extractor")

# Academic titles & leadership designations regex
DESIGNATION_PATTERN = re.compile(
    r"\b("
    r"President|Chancellor|Vice[- ]?Chancellor|Director|Provost|Rector|Principal|"
    r"Dean(?: of [A-Za-z]+(?:\s+[A-Za-z]+){0,3})?|Vice[- ]?Dean|Associate Dean|Senior Associate Dean|"
    r"Head of Department|Department Head|Dept\.? Head|HOD|Department Chair|Division Head|Section Head|"
    r"In[- ]?Charge|Convener|Program Director|"
    r"Distinguished Professor|Endowed Professor|Institute Professor|Chair Professor|Regents Professor|"
    r"Emeritus Professor|Emerita|"
    r"Full Professor|Professor(?: of [A-Za-z]+(?:\s+[A-Za-z]+){0,3})?|"
    r"Associate Professor(?: of [A-Za-z]+(?:\s+[A-Za-z]+){0,3})?|"
    r"Assistant Professor(?: of [A-Za-z]+(?:\s+[A-Za-z]+){0,3})?|"
    r"Senior Lecturer|Lecturer|Reader|Faculty Fellow|Research Scientist|Fellow|Instructor"
    r")\b",
    re.IGNORECASE
)

# Honorific prefixes
NAME_PREFIXES = re.compile(r"^(?:Dr\.?|Prof\.?|Professor|Dean|Mr\.?|Ms\.?|Mrs\.?|Shri\.?|Rev\.?)\s+", re.IGNORECASE)

# Academic degrees or suffixes following names
NAME_SUFFIXES = re.compile(r",?\s*(?:Ph\.?D\.?|M\.?D\.?|FNA|FASc|FNASc|FNAE|O\.?B\.?E\.?|C\.?B\.?E\.?|F\.?R\.?S\.?|M\.?S\.?|M\.?Tech\.?|B\.?Tech\.?)$", re.IGNORECASE)

# Email address extraction
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")

# Non-personal generic emails to exclude
GENERIC_EMAILS = {"info@", "contact@", "admin@", "support@", "webmaster@", "help@", "office@", "privacy@", "feedback@"}

# Phone number extraction
PHONE_PATTERN = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b")

# Navigation and non-name phrases to blacklist
NAME_BLACKLIST = {
    "read more", "view profile", "profile", "contact", "about", "about us",
    "department", "academics", "faculty", "research", "publications",
    "home", "next", "previous", "next page", "previous page", "search",
    "back to top", "overview", "faculty directory", "our people", "leadership team",
    "all profiles", "last name", "first name", "by name", "view all", "sort by",
    "filter by", "filter", "filters", "departments", "programs", "centers and institutes",
    "programs, centers and institutes", "divisions", "browse", "people directory"
}

# Organizational/institutional words that cannot be person names
ORGANIZATIONAL_WORDS = {
    "research", "computing", "center", "centre", "institute", "institutes", "laboratory", "lab",
    "office", "department", "dept", "departments", "university", "school", "college", "council",
    "board", "committee", "service", "services", "science", "sciences", "engineering",
    "humanities", "studies", "administration", "affairs", "initiative", "program", "programs",
    "division", "divisions", "foundation", "library", "press", "news", "stories", "alumni",
    "students", "admissions", "athletics", "technology", "development", "relations",
    "profile", "profiles", "overview", "directory", "filter", "filters", "browse", "gateway"
}


def is_probable_name(text: str) -> bool:
    """
    Validates if a text string looks like a person's name:
    - 2 to 5 words
    - Starts with uppercase letters or honorific
    - Not an organization, department, or generic button label
    """
    if not text:
        return False
    text = text.strip()
    if len(text) < 3 or len(text) > 60:
        return False

    if text.lower() in NAME_BLACKLIST:
        return False

    # Check for honorific
    has_prefix = bool(NAME_PREFIXES.search(text))

    # Tokenize
    clean_text = NAME_PREFIXES.sub("", text)
    clean_text = NAME_SUFFIXES.sub("", clean_text)
    words = [w for w in re.split(r"\s+", clean_text) if w]

    if not (1 <= len(words) <= 5):
        return False

    # Check for organizational words (reject if not explicitly prefixed with Dr./Prof.)
    lower_words = {w.lower() for w in words}
    if not has_prefix and bool(lower_words & ORGANIZATIONAL_WORDS):
        return False

    # Most words should be capitalized
    capitalized_words = sum(1 for w in words if w[0].isupper() or w[0] in "'\"")
    return has_prefix or (capitalized_words >= len(words) * 0.75 and len(words) >= 2)


def extract_email(text: str, element: Tag) -> Optional[str]:
    """Finds email address from text or mailto link."""
    # Check mailto links first
    for a in element.select("a[href^='mailto:']"):
        href = a.get("href", "")
        clean_email = href.replace("mailto:", "").split("?")[0].strip()
        if clean_email and not any(gen in clean_email.lower() for gen in GENERIC_EMAILS):
            return clean_email

    # Check regex on text
    matches = EMAIL_PATTERN.findall(text)
    for m in matches:
        if not any(gen in m.lower() for gen in GENERIC_EMAILS):
            return m
    return None


def extract_phone(text: str, element: Tag) -> Optional[str]:
    """Finds phone number from tel link or regex."""
    for a in element.select("a[href^='tel:']"):
        href = a.get("href", "")
        return href.replace("tel:", "").strip()

    matches = PHONE_PATTERN.findall(text)
    if matches:
        return matches[0].strip()
    return None


class FacultyExtractor:
    """
    Two-Tier Extractor for identifying faculty and leadership cards.
    Tier 1: Detect recurring DOM clusters/cards or directory tables.
    Tier 2: Parse and normalize fields into Pydantic models.
    """

    def __init__(self, institution_name: str):
        self.institution_name = institution_name

    def find_repeating_containers(self, soup: BeautifulSoup) -> List[List[Tag]]:
        """
        Discovers recurring parent containers and clusters of cards.
        Identifies groupings with 3 or more structurally similar children.
        """
        candidate_clusters: List[List[Tag]] = []

        # 1. Search for table rows (tabular directories)
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if len(rows) >= 3:
                candidate_clusters.append(rows[1:])  # Exclude header row

        # 2. Search for repeating child cards (grid/flex containers)
        # Scan container elements that commonly wrap lists of items
        container_tags = soup.find_all(["div", "ul", "section", "ol", "main"])
        for container in container_tags:
            children = [c for c in container.children if isinstance(c, Tag)]
            if len(children) >= 3:
                # Group children by tag name and primary CSS class
                tag_groups: Dict[str, List[Tag]] = {}
                for c in children:
                    tag_name = c.name
                    classes = "-".join(sorted(c.get("class", [])))
                    key = f"{tag_name}:{classes}"
                    tag_groups.setdefault(key, []).append(c)

                for key, items in tag_groups.items():
                    if len(items) >= 3:
                        # Test if the cluster contains signals of faculty (names, titles, or profiles)
                        sample_text = " ".join(item.get_text() for item in items[:5])
                        if DESIGNATION_PATTERN.search(sample_text) or "Dr." in sample_text or "Prof." in sample_text:
                            candidate_clusters.append(items)

        # Sort clusters by size descending
        candidate_clusters.sort(key=lambda cluster: len(cluster), reverse=True)
        return candidate_clusters

    def extract_from_card(self, card: Tag, source_url: str) -> Optional[FacultyMember]:
        """
        Extracts a single faculty member from a DOM card, item, or table row.
        """
        card_text = card.get_text(separator=" ", strip=True)
        if len(card_text) < 10:
            return None

        # --- Name Detection ---
        name: Optional[str] = None
        profile_url: Optional[str] = None

        # Look in headings, strong tags, or primary links first
        header_candidates = card.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
        for h in header_candidates:
            h_text = h.get_text(strip=True)
            if is_probable_name(h_text):
                name = h_text
                # If header has or is wrapped in an <a> link, use it as profile URL
                a_tag = h if h.name == "a" else (h.find("a") or h.find_parent("a"))
                if a_tag and a_tag.get("href"):
                    profile_url = urljoin(source_url, a_tag["href"])
                break

        # If not found in headers, inspect plain <a> links
        if not name:
            for a in card.find_all("a"):
                if a.find(["h1", "h2", "h3", "h4", "h5", "div", "p"]):
                    continue
                a_text = a.get_text(strip=True)
                if is_probable_name(a_text):
                    name = a_text
                    if a.get("href"):
                        profile_url = urljoin(source_url, a["href"])
                    break

        if not name:
            # Check for honorific in beginning of card text
            match = re.search(r"\b((?:Dr\.?|Prof\.?)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", card_text)
            if match:
                name = match.group(1).strip()

        if not name:
            return None

        # Clean name
        name = NAME_PREFIXES.sub(lambda m: m.group(0).strip() + " ", name)
        name = re.sub(r"\s+", " ", name).strip()

        # --- Designation & Department Detection ---
        designation: Optional[str] = None
        role_tag = card.find(class_=re.compile(r"\b(role|title|position|designation)\b", re.I))
        if role_tag:
            role_text = role_tag.get_text(strip=True)
            desig_match = DESIGNATION_PATTERN.search(role_text)
            if desig_match:
                designation = desig_match.group(0).strip()
            elif 3 < len(role_text) < 60:
                designation = role_text

        if not designation:
            desig_match = DESIGNATION_PATTERN.search(card_text)
            if desig_match:
                designation = desig_match.group(0).strip()

        # Fallback designation if academic prefixes were present in name
        if not designation:
            if "Prof." in name or "Professor" in name:
                designation = "Professor"
            elif "Dean" in name:
                designation = "Dean"
            else:
                designation = "Faculty Member"

        # Search for department signals
        department: Optional[str] = None
        dept_match = re.search(
            r"\b(Department of [A-Za-z\s&]+|School of [A-Za-z\s&]+|Division of [A-Za-z\s&]+|"
            r"[A-Za-z\s&]+ Engineering|[A-Za-z\s&]+ Sciences|[A-Za-z\s&]+ Studies)\b",
            card_text
        )
        if dept_match:
            dept_candidate = dept_match.group(0).strip()
            if len(dept_candidate) < 60:
                department = dept_candidate

        # --- Profile URL Fallback ---
        if not profile_url:
            for a in card.find_all("a", href=True):
                href = a["href"]
                # Favor links containing typical profile paths
                if any(x in href.lower() for x in ["faculty", "people", "profile", "person", "staff", "bio", "~"]):
                    profile_url = urljoin(source_url, href)
                    break
        if not profile_url:
            profile_url = source_url

        # --- Email & Phone ---
        email = extract_email(card_text, card)
        phone = extract_phone(card_text, card)

        # --- Description / Bio Summary ---
        paragraphs = [p.get_text(strip=True) for p in card.find_all(["p", "span"]) if len(p.get_text(strip=True)) > 25]
        description = None
        if paragraphs:
            # Pick longest paragraph not containing the email
            sorted_paras = sorted(paragraphs, key=len, reverse=True)
            for p_cand in sorted_paras:
                if not (email and email in p_cand):
                    description = p_cand[:500]
                    break

        # Compute rank score
        rank_score = FacultyMember.compute_hierarchy_rank(designation)

        return FacultyMember(
            name=name,
            designation=designation,
            department=department,
            profile_url=profile_url,
            description=description,
            email=email,
            phone=phone,
            institution=self.institution_name,
            source_url=source_url,
            rank_score=rank_score
        )

    def extract_from_html(self, html: str, source_url: str) -> List[FacultyMember]:
        """
        Parses page HTML, discovers repeating cards, and yields normalized FacultyMember objects.
        """
        soup = BeautifulSoup(html, "html.parser")
        clusters = self.find_repeating_containers(soup)
        extracted: List[FacultyMember] = []
        seen_keys: Set[str] = set()

        logger.info(f"Identified {len(clusters)} repeating DOM card clusters in {source_url}")

        for cluster in clusters:
            for card in cluster:
                try:
                    member = self.extract_from_card(card, source_url)
                    if member:
                        # Deduplication key based on normalized name + email/profile
                        key = f"{member.name.lower()}:{member.email or member.profile_url}"
                        if key not in seen_keys:
                            seen_keys.add(key)
                            extracted.append(member)
                except Exception as e:
                    logger.debug(f"Error parsing card: {e}")

        logger.info(f"Extracted {len(extracted)} valid faculty members from {source_url}")
        return extracted
