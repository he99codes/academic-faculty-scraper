"""
Data models and schemas for the Academic Faculty and Leadership Scraper.
Uses Pydantic v2 for data validation and schema normalization.
"""

from datetime import datetime, timezone
import json
import csv
import re
from typing import List, Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator


class FacultyMember(BaseModel):
    """Normalized schema representing a faculty member or academic leader."""
    name: str = Field(..., description="Full name of the faculty member")
    designation: str = Field(..., description="Exact academic or administrative title")
    department: Optional[str] = Field(None, description="Academic department, division, or school")
    profile_url: str = Field(..., description="Absolute URL to the individual's profile page")
    description: Optional[str] = Field(None, description="Research area, bio, or responsibilities summary")
    email: Optional[str] = Field(None, description="Public email address if available")
    phone: Optional[str] = Field(None, description="Public telephone or office extension")
    institution: str = Field(..., description="Name of the institution")
    source_url: str = Field(..., description="URL where this entry was scraped from")
    rank_score: int = Field(0, description="Hierarchy rank score (higher means higher leadership)")

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, v: str) -> str:
        if not v:
            return "Unknown"
        # Remove extra whitespace and strange unicode spaces
        v = re.sub(r"\s+", " ", str(v)).strip()
        # Clean trailing commas or pipes
        v = v.rstrip(",|:- ")
        return v

    @field_validator("designation", mode="before")
    @classmethod
    def clean_designation(cls, v: str) -> str:
        if not v:
            return "Faculty Member"
        v = re.sub(r"\s+", " ", str(v)).strip()
        return v.rstrip(",|:- ")

    @field_validator("description", mode="before")
    @classmethod
    def clean_description(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = re.sub(r"\s+", " ", str(v)).strip()
        return v[:1000] if len(v) > 1000 else v  # Cap length for concise summary

    @staticmethod
    def compute_hierarchy_rank(designation: str) -> int:
        """
        Calculates an institutional hierarchy rank score (0 - 100) based on designation.
        Prioritizes top leadership (President, Chancellor, Deans, HODs) down to regular faculty.
        """
        if not designation:
            return 10
        title_lower = designation.lower()

        # Tier 1: Executive Leadership (100)
        if any(kw in title_lower for kw in [
            "president", "chancellor", "vice-chancellor", "vice chancellor",
            "director", "provost", "rector", "principal"
        ]):
            return 100

        # Tier 2: Deans & Vice Provosts (85)
        if any(kw in title_lower for kw in [
            "dean", "vice-dean", "vice dean", "associate dean", "senior associate dean",
            "vice provost", "deputy director"
        ]):
            return 85

        # Tier 3: Heads of Department & Department Chairs (75)
        if any(kw in title_lower for kw in [
            "head of department", "head, department", "hod", "department chair",
            "department head", "dept head", "chair of department", "division head",
            "section head", "in-charge", "incharge", "convener", "program director"
        ]):
            return 75

        # Tier 4: Distinguished / Endowed Chairs (65)
        if any(kw in title_lower for kw in [
            "distinguished professor", "chair professor", "endowed professor",
            "institute professor", "regents professor", "emeritus professor", "emerita"
        ]):
            return 65

        # Tier 5: Full Professors (50)
        if "full professor" in title_lower or (
            "professor" in title_lower
            and not any(x in title_lower for x in ["assistant", "associate", "adjunct", "visiting"])
        ):
            return 50

        # Tier 6: Associate Professors (40)
        if "associate professor" in title_lower or "reader" in title_lower:
            return 40

        # Tier 7: Assistant Professors / Senior Lecturers (30)
        if "assistant professor" in title_lower or "senior lecturer" in title_lower:
            return 30

        # Tier 8: Other Faculty / Instructors / Researchers (20)
        if any(kw in title_lower for kw in ["lecturer", "instructor", "faculty", "fellow", "researcher", "scientist"]):
            return 20

        return 10


class InstitutionReport(BaseModel):
    """Aggregated output report containing all extracted faculty and metadata."""
    institution_name: str
    canonical_domain: str
    homepage_url: str
    discovered_directories: List[str] = Field(default_factory=list)
    total_faculty_extracted: int = 0
    faculty: List[FacultyMember] = Field(default_factory=list)
    scraped_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def sort_faculty(self) -> None:
        """Sort faculty members by hierarchy rank descending, then by name."""
        self.faculty.sort(key=lambda m: (-m.rank_score, m.name))

    def export_json(self, filepath: str) -> None:
        """Export report to a formatted JSON file."""
        self.sort_faculty()
        self.total_faculty_extracted = len(self.faculty)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=2, ensure_ascii=False)

    def export_jsonl(self, filepath: str) -> None:
        """Export faculty records as JSON Lines (.jsonl)."""
        self.sort_faculty()
        with open(filepath, "w", encoding="utf-8") as f:
            for member in self.faculty:
                f.write(json.dumps(member.model_dump(), ensure_ascii=False) + "\n")

    def export_csv(self, filepath: str) -> None:
        """Export faculty records to a flat CSV file."""
        self.sort_faculty()
        fieldnames = [
            "name", "designation", "rank_score", "department",
            "email", "phone", "profile_url", "description",
            "institution", "source_url"
        ]
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for member in self.faculty:
                row = member.model_dump()
                filtered_row = {k: row.get(k, "") for k in fieldnames}
                writer.writerow(filtered_row)
