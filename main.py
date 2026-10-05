"""
Main CLI Entry Point.
Coordinates autonomous domain resolution, directory discovery, dynamic crawling,
two-tier card extraction, leadership prioritization, and formatted exports.

Usage:
    python main.py --institution "Stanford University" --limit 50 --output faculty.json
"""

import argparse
import asyncio
import logging
import os
import sys
from typing import List, Set

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from models import FacultyMember, InstitutionReport
from domain_resolver import DomainResolver
from crawler import AdaptiveCrawler
from extractor import FacultyExtractor

# Ensure UTF-8 console handling on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("academic_scraper")
console = Console(force_terminal=True, legacy_windows=False)


async def run_pipeline(
    institution_name: str,
    limit: int = 50,
    output_path: str = "faculty.json",
    headless: bool = True,
    max_dirs: int = 5
) -> InstitutionReport:
    """
    Executes the end-to-end autonomous scraping pipeline for a given institution.
    """
    console.print(Panel(
        f"[bold cyan]Academic Faculty & Leadership Scraper[/bold cyan]\n"
        f"Institution: [bold yellow]{institution_name}[/bold yellow] | "
        f"Target Limit: [bold green]{limit}[/bold green] | "
        f"Output: [bold white]{output_path}[/bold white]",
        border_style="cyan"
    ))

    # ---------------------------------------------------------
    # STEP 1: Autonomous Domain Resolution
    # ---------------------------------------------------------
    with console.status("[bold green]Step 1/5: Resolving official institution domain...[/bold green]"):
        resolver = DomainResolver(headless=headless)
        res = await resolver.resolve(institution_name)

    if not res:
        console.print(f"[bold red]Error:[/bold red] Failed to resolve official domain for '{institution_name}'.")
        sys.exit(1)

    canonical_domain, homepage_url = res
    console.print(f"[bold green][+][/bold green] Canonical Domain: [bold green]{canonical_domain}[/bold green]")
    console.print(f"[bold green][+][/bold green] Homepage: [dim]{homepage_url}[/dim]")

    # ---------------------------------------------------------
    # STEP 2: Adaptive Directory Discovery
    # ---------------------------------------------------------
    with console.status("[bold green]Step 2/5: Discovering faculty directories & leadership pages...[/bold green]"):
        crawler = AdaptiveCrawler(headless=headless, max_directories=max_dirs)
        directory_urls = await crawler.discover_candidate_directories(canonical_domain, homepage_url)

    if not directory_urls:
        console.print(f"[yellow]Warning:[/yellow] No specialized directory pages found. Attempting root homepage.")
        directory_urls = [homepage_url]
    else:
        console.print(f"[bold green][+][/bold green] Discovered [bold cyan]{len(directory_urls)}[/bold cyan] candidate directory page(s):")
        for d in directory_urls:
            console.print(f"  * [blue]{d}[/blue]")

    # ---------------------------------------------------------
    # STEP 3 & 4: Ingestion and Hybrid Extraction
    # ---------------------------------------------------------
    all_faculty: List[FacultyMember] = []
    seen_keys: Set[str] = set()
    extractor = FacultyExtractor(institution_name=institution_name)

    for idx, dir_url in enumerate(directory_urls, 1):
        with console.status(f"[bold green]Step 3-4/5: Rendering & extracting ({idx}/{len(directory_urls)}): {dir_url}...[/bold green]"):
            rendered_html, final_url = await crawler.render_and_harvest_page(dir_url)
            if not rendered_html:
                continue

            extracted_members = extractor.extract_from_html(rendered_html, final_url)
            for m in extracted_members:
                # Deduplicate by normalized name and email/profile URL
                dedup_key = f"{m.name.lower()}:{m.email or m.profile_url}"
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    all_faculty.append(m)

        console.print(f"  -> Accumulated [bold green]{len(all_faculty)}[/bold green] unique faculty records.")
        if len(all_faculty) >= limit:
            break

    # ---------------------------------------------------------
    # STEP 5: Post-Processing, Hierarchy Sorting & Export
    # ---------------------------------------------------------
    with console.status("[bold green]Step 5/5: Normalizing, hierarchy ranking and exporting...[/bold green]"):
        report = InstitutionReport(
            institution_name=institution_name,
            canonical_domain=canonical_domain,
            homepage_url=homepage_url,
            discovered_directories=directory_urls,
            total_faculty_extracted=len(all_faculty),
            faculty=all_faculty
        )

        # Sort by hierarchy (Leadership first)
        report.sort_faculty()

        # Apply limit
        if len(report.faculty) > limit:
            report.faculty = report.faculty[:limit]
            report.total_faculty_extracted = len(report.faculty)

        # Determine file format from output extension
        ext = os.path.splitext(output_path)[1].lower()
        if ext == ".csv":
            report.export_csv(output_path)
        elif ext == ".jsonl":
            report.export_jsonl(output_path)
        else:
            report.export_json(output_path)

    console.print(f"\n[bold green][+] Scrape Complete![/bold green] Saved {len(report.faculty)} records to [bold white]{output_path}[/bold white]\n")

    # Render summary table in console
    table = Table(title=f"Top Leadership & Faculty - {institution_name}", show_header=True, header_style="bold magenta")
    table.add_column("Rank", style="dim", width=6)
    table.add_column("Name", style="bold cyan")
    table.add_column("Designation", style="green")
    table.add_column("Department", style="yellow")
    table.add_column("Email", style="blue")
    table.add_column("Profile Link", style="dim")

    for f in report.faculty[:15]:  # Display top 15 in console table
        table.add_row(
            str(f.rank_score),
            f.name,
            f.designation,
            f.department or "-",
            f.email or "-",
            f.profile_url
        )

    console.print(table)
    if len(report.faculty) > 15:
        console.print(f"[dim]... and {len(report.faculty) - 15} more in {output_path}[/dim]\n")

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Autonomous Global Academic Faculty & Leadership Scraper",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--institution",
        type=str,
        required=True,
        help="Name of the educational institution (e.g. 'Stanford University', 'IIT Bombay')"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of faculty records to extract"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="faculty.json",
        help="Output filepath (.json, .jsonl, or .csv)"
    )
    parser.add_argument(
        "--max-dirs",
        type=int,
        default=5,
        help="Maximum candidate directory pages to crawl"
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser visibly (default is headless)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose debug logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    asyncio.run(run_pipeline(
        institution_name=args.institution,
        limit=args.limit,
        output_path=args.output,
        headless=not args.no_headless,
        max_dirs=args.max_dirs
    ))


if __name__ == "__main__":
    main()
