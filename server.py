"""
FastAPI Web Server & Interactive Dashboard.
Provides REST API endpoints and an interactive web interface for running,
monitoring, and exporting faculty scraping jobs in real-time.
"""

import asyncio
from datetime import datetime, timezone
import io
import csv
import json
import logging
import os
import uuid
from typing import Dict, List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

from models import FacultyMember, InstitutionReport
from domain_resolver import DomainResolver
from crawler import AdaptiveCrawler
from extractor import FacultyExtractor

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scraper_server")

app = FastAPI(title="Academic Faculty & Leadership Scraper Server", version="1.0.0")

# In-memory store for scraping jobs
JOBS: Dict[str, dict] = {}


class ScrapeRequest(BaseModel):
    institution: str
    limit: int = 25
    max_dirs: int = 5


def add_job_log(job_id: str, message: str, step: Optional[int] = None):
    """Helper to append timestamped logs to job progress."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "message": message, "step": step}
    if job_id in JOBS:
        JOBS[job_id]["logs"].append(entry)
        if step is not None:
            JOBS[job_id]["current_step"] = step


async def background_scrape_runner(job_id: str, institution: str, limit: int, max_dirs: int):
    """Executes the scraping pipeline asynchronously in the background."""
    try:
        JOBS[job_id]["status"] = "running"
        add_job_log(job_id, f"Initiating scrape for '{institution}'", step=1)

        # 1. Resolve domain
        resolver = DomainResolver(headless=True)
        add_job_log(job_id, f"Step 1/5: Searching & resolving official domain...", step=1)
        res = await resolver.resolve(institution)

        if not res:
            raise Exception(f"Could not resolve official domain for '{institution}'")

        canonical_domain, homepage_url = res
        JOBS[job_id]["domain"] = canonical_domain
        JOBS[job_id]["homepage"] = homepage_url
        add_job_log(job_id, f"Identified official domain: {canonical_domain} ({homepage_url})", step=2)

        # 2. Discover directories
        crawler = AdaptiveCrawler(headless=True, max_directories=max_dirs)
        add_job_log(job_id, f"Step 2/5: Scanning navigation menus & directory dorks...", step=2)
        directory_urls = await crawler.discover_candidate_directories(canonical_domain, homepage_url)

        if not directory_urls:
            add_job_log(job_id, "No direct directory found, exploring root portal...", step=2)
            directory_urls = [homepage_url]
        else:
            add_job_log(job_id, f"Discovered {len(directory_urls)} directory landing page(s)", step=3)

        JOBS[job_id]["directories"] = directory_urls

        # 3 & 4. Ingestion & Extraction
        all_faculty: List[FacultyMember] = []
        seen_keys = set()
        extractor = FacultyExtractor(institution_name=institution)

        for idx, dir_url in enumerate(directory_urls, 1):
            add_job_log(job_id, f"Step 3-4/5: Rendering & harvesting ({idx}/{len(directory_urls)}): {dir_url}", step=3)
            rendered_html, final_url = await crawler.render_and_harvest_page(dir_url)
            if not rendered_html:
                continue

            extracted_members = extractor.extract_from_html(rendered_html, final_url)
            for m in extracted_members:
                dedup_key = f"{m.name.lower()}:{m.email or m.profile_url}"
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    all_faculty.append(m)

            add_job_log(job_id, f"Total unique profiles found so far: {len(all_faculty)}", step=4)
            if len(all_faculty) >= limit:
                break

        # 5. Normalization & Hierarchy Ranking
        add_job_log(job_id, "Step 5/5: Computing leadership hierarchy ranking...", step=5)
        report = InstitutionReport(
            institution_name=institution,
            canonical_domain=canonical_domain,
            homepage_url=homepage_url,
            discovered_directories=directory_urls,
            total_faculty_extracted=len(all_faculty),
            faculty=all_faculty
        )
        report.sort_faculty()

        if len(report.faculty) > limit:
            report.faculty = report.faculty[:limit]
            report.total_faculty_extracted = len(report.faculty)

        JOBS[job_id]["result"] = report.model_dump()
        JOBS[job_id]["status"] = "completed"
        add_job_log(job_id, f"Scrape completed successfully with {len(report.faculty)} profiles!", step=5)

    except Exception as e:
        logger.error(f"Error in job {job_id}: {e}", exc_info=True)
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)
        add_job_log(job_id, f"Error: {str(e)}")


@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest, background_tasks: BackgroundTasks):
    """Enqueues a new autonomous scraping task."""
    institution = req.institution.strip()
    if not institution:
        raise HTTPException(status_code=400, detail="Institution name is required")

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        "id": job_id,
        "institution": institution,
        "status": "pending",
        "current_step": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "logs": [],
        "domain": None,
        "homepage": None,
        "directories": [],
        "result": None,
        "error": None
    }

    background_tasks.add_task(
        background_scrape_runner,
        job_id,
        institution,
        req.limit,
        req.max_dirs
    )

    return {"job_id": job_id, "status": "pending", "message": f"Job queued for '{institution}'"}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Returns current status, live logs, and results of a job."""
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]


@app.get("/api/export/{job_id}")
async def export_job_data(job_id: str, format: str = Query("json", regex="^(json|csv|jsonl)$")):
    """Exports scraping results to downloadable JSON or CSV format."""
    if job_id not in JOBS or not JOBS[job_id].get("result"):
        raise HTTPException(status_code=404, detail="Job results not ready")

    res = JOBS[job_id]["result"]
    institution_slug = "".join(c if c.isalnum() else "_" for c in res["institution_name"].lower()).strip("_")

    if format == "csv":
        output = io.StringIO()
        fieldnames = [
            "name", "designation", "rank_score", "department",
            "email", "phone", "profile_url", "description",
            "institution", "source_url"
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for member in res.get("faculty", []):
            writer.writerow({k: member.get(k, "") for k in fieldnames})
        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={institution_slug}_faculty.csv"}
        )

    elif format == "jsonl":
        output = io.StringIO()
        for member in res.get("faculty", []):
            output.write(json.dumps(member, ensure_ascii=False) + "\n")
        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename={institution_slug}_faculty.jsonl"}
        )

    else:
        # Default JSON
        content = json.dumps(res, indent=2, ensure_ascii=False)
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={institution_slug}_faculty.json"}
        )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "jobs_count": len(JOBS)}


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Renders the sleek, interactive scraping control dashboard."""
    return HTML_DASHBOARD


HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Autonomous Academic Faculty Scraper</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
    body { font-family: 'Inter', sans-serif; }
    .glass { background: rgba(17, 24, 39, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
    .pulse-ring { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col antialiased selection:bg-indigo-500 selection:text-white">

  <!-- Header -->
  <header class="border-b border-slate-800/80 bg-slate-900/60 sticky top-0 z-50 backdrop-blur-md">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="h-10 w-10 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center shadow-lg shadow-indigo-500/20">
          <i class="fa-solid fa-graduation-cap text-white text-lg"></i>
        </div>
        <div>
          <h1 class="text-lg font-bold bg-gradient-to-r from-white via-slate-200 to-indigo-300 bg-clip-text text-transparent">
            Academic Faculty Scraper
          </h1>
          <p class="text-xs text-slate-400 font-medium">Autonomous Global Crawler & Hierarchy Ranker</p>
        </div>
      </div>
      <div class="flex items-center space-x-3">
        <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 animate-pulse"></span> Server Live (Port 8000)
        </span>
      </div>
    </div>
  </header>

  <!-- Main Container -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-1 w-full space-y-8">

    <!-- Search Form Card -->
    <div class="glass rounded-2xl p-6 sm:p-8 shadow-2xl shadow-indigo-950/30">
      <div class="max-w-3xl">
        <h2 class="text-xl sm:text-2xl font-bold tracking-tight text-white mb-2">Search & Scrape Any University</h2>
        <p class="text-sm text-slate-400 mb-6">Enter any educational institution name worldwide. The autonomous engine resolves the domain, explores directories, and extracts faculty leadership without credentials.</p>
        
        <form id="scrapeForm" class="space-y-4">
          <div class="flex flex-col sm:flex-row gap-3">
            <div class="relative flex-1">
              <div class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-slate-400">
                <i class="fa-solid fa-magnifying-glass"></i>
              </div>
              <input 
                type="text" 
                id="institutionInput" 
                required
                placeholder="e.g. Stanford University, IIT Bombay, ETH Zurich, Oxford..." 
                class="w-full pl-10 pr-4 py-3 bg-slate-900/90 border border-slate-700/80 rounded-xl text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition"
              >
            </div>
            <button 
              type="submit" 
              id="submitBtn"
              class="px-6 py-3 bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white font-semibold rounded-xl shadow-lg shadow-indigo-600/25 flex items-center justify-center space-x-2 transition transform active:scale-95 disabled:opacity-50"
            >
              <i class="fa-solid fa-bolt"></i>
              <span>Start Scrape</span>
            </button>
          </div>

          <!-- Advanced Controls -->
          <div class="pt-2 flex flex-wrap items-center gap-6 text-xs text-slate-400">
            <div class="flex items-center space-x-2">
              <label for="limitInput" class="font-medium text-slate-300">Profile Limit:</label>
              <input type="number" id="limitInput" min="5" max="100" value="20" class="w-16 px-2 py-1 bg-slate-900 border border-slate-700 rounded-lg text-white text-center">
            </div>
            <div class="flex items-center space-x-2">
              <label for="maxDirsInput" class="font-medium text-slate-300">Max Directories:</label>
              <input type="number" id="maxDirsInput" min="1" max="10" value="4" class="w-16 px-2 py-1 bg-slate-900 border border-slate-700 rounded-lg text-white text-center">
            </div>
            <div class="flex items-center space-x-1.5 text-slate-400">
              <i class="fa-solid fa-shield-halved text-indigo-400"></i>
              <span>Stealth Mode Enabled</span>
            </div>
          </div>
        </form>
      </div>
    </div>

    <!-- Live Job Progress Card (Hidden initially) -->
    <div id="progressCard" class="hidden glass rounded-2xl p-6 sm:p-8 space-y-5 border border-indigo-500/20 shadow-xl">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-800/80 pb-4">
        <div>
          <div class="flex items-center space-x-3">
            <span id="statusSpinner" class="flex h-3 w-3 relative">
              <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-75"></span>
              <span class="relative inline-flex rounded-full h-3 w-3 bg-indigo-500"></span>
            </span>
            <h3 id="jobTitle" class="text-lg font-bold text-white">Scraping in progress...</h3>
          </div>
          <p id="jobDomainSub" class="text-xs text-slate-400 mt-1">Connecting to search engines and discovering canonical portal</p>
        </div>
        <div id="badgeStatus" class="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
          Running
        </div>
      </div>

      <!-- Stepper Visualizer -->
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
        <div id="stepBox1" class="p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-slate-400 transition">
          <div class="font-semibold text-white mb-1">1. Domain Search</div>
          <div class="text-[11px] text-slate-500">Resolve canonical portal</div>
        </div>
        <div id="stepBox2" class="p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-slate-400 transition">
          <div class="font-semibold text-white mb-1">2. Directory Hunt</div>
          <div class="text-[11px] text-slate-500">Navigation & dorks</div>
        </div>
        <div id="stepBox3" class="p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-slate-400 transition">
          <div class="font-semibold text-white mb-1">3. Dynamic Ingestion</div>
          <div class="text-[11px] text-slate-500">Playwright rendering</div>
        </div>
        <div id="stepBox4" class="p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-slate-400 transition">
          <div class="font-semibold text-white mb-1">4. Cluster Extraction</div>
          <div class="text-[11px] text-slate-500">Hierarchy & schema</div>
        </div>
      </div>

      <!-- Live Terminal Log Box -->
      <div class="rounded-xl bg-slate-950 p-4 border border-slate-800 font-mono text-xs max-h-48 overflow-y-auto space-y-1.5" id="logContainer">
        <div class="text-slate-500">[System] Awaiting instructions...</div>
      </div>
    </div>

    <!-- Results Section (Hidden initially) -->
    <div id="resultsCard" class="hidden space-y-6">
      
      <!-- Summary Bar -->
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 glass rounded-2xl p-6">
        <div>
          <h3 id="resultTitle" class="text-xl font-bold text-white">Extracted Faculty</h3>
          <p id="resultMeta" class="text-xs text-slate-400 mt-1">Showing all structured profiles with hierarchy scoring</p>
        </div>
        <div class="flex items-center space-x-3">
          <a id="downloadCsvBtn" href="#" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-xl text-xs font-semibold text-white flex items-center space-x-1.5 transition">
            <i class="fa-solid fa-file-csv text-emerald-400"></i>
            <span>Download CSV</span>
          </a>
          <a id="downloadJsonBtn" href="#" class="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 rounded-xl text-xs font-semibold text-white flex items-center space-x-1.5 shadow-md shadow-indigo-600/20 transition">
            <i class="fa-solid fa-code text-indigo-200"></i>
            <span>Download JSON</span>
          </a>
        </div>
      </div>

      <!-- Filter and Faculty Table -->
      <div class="glass rounded-2xl overflow-hidden shadow-2xl">
        <div class="p-4 border-b border-slate-800 flex items-center justify-between gap-4">
          <div class="relative w-72">
            <input 
              type="text" 
              id="filterInput" 
              placeholder="Search by name, role, department..." 
              class="w-full pl-8 pr-3 py-1.5 bg-slate-900 border border-slate-700/80 rounded-lg text-xs text-white placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            >
            <i class="fa-solid fa-filter absolute left-2.5 top-2.5 text-[11px] text-slate-500"></i>
          </div>
          <span id="recordCount" class="text-xs font-medium text-slate-400">0 Profiles</span>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-slate-900/80 text-slate-400 uppercase tracking-wider text-[11px] border-b border-slate-800">
              <tr>
                <th class="py-3.5 px-4 font-semibold">Rank</th>
                <th class="py-3.5 px-4 font-semibold">Faculty / Leader</th>
                <th class="py-3.5 px-4 font-semibold">Designation</th>
                <th class="py-3.5 px-4 font-semibold">Department</th>
                <th class="py-3.5 px-4 font-semibold">Contact / Email</th>
                <th class="py-3.5 px-4 font-semibold text-right">Profile</th>
              </tr>
            </thead>
            <tbody id="facultyTableBody" class="divide-y divide-slate-800/50">
              <!-- Dynamically populated -->
            </tbody>
          </table>
        </div>
      </div>

    </div>

  </main>

  <script>
    let activeJobId = null;
    let pollInterval = null;
    let currentFacultyData = [];

    const scrapeForm = document.getElementById('scrapeForm');
    const institutionInput = document.getElementById('institutionInput');
    const limitInput = document.getElementById('limitInput');
    const maxDirsInput = document.getElementById('maxDirsInput');
    const submitBtn = document.getElementById('submitBtn');

    const progressCard = document.getElementById('progressCard');
    const jobTitle = document.getElementById('jobTitle');
    const jobDomainSub = document.getElementById('jobDomainSub');
    const badgeStatus = document.getElementById('badgeStatus');
    const logContainer = document.getElementById('logContainer');

    const resultsCard = document.getElementById('resultsCard');
    const resultTitle = document.getElementById('resultTitle');
    const resultMeta = document.getElementById('resultMeta');
    const facultyTableBody = document.getElementById('facultyTableBody');
    const recordCount = document.getElementById('recordCount');
    const filterInput = document.getElementById('filterInput');
    const downloadCsvBtn = document.getElementById('downloadCsvBtn');
    const downloadJsonBtn = document.getElementById('downloadJsonBtn');

    scrapeForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const institution = institutionInput.value.trim();
      if (!institution) return;

      submitBtn.disabled = true;
      progressCard.classList.remove('hidden');
      resultsCard.classList.add('hidden');
      logContainer.innerHTML = '';
      currentFacultyData = [];

      jobTitle.innerText = `Scraping "${institution}"`;
      jobDomainSub.innerText = 'Queued autonomous pipeline...';
      badgeStatus.className = 'inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30';
      badgeStatus.innerText = 'Running';

      try {
        const resp = await fetch('/api/scrape', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            institution: institution,
            limit: parseInt(limitInput.value) || 25,
            max_dirs: parseInt(maxDirsInput.value) || 4
          })
        });

        const data = await resp.json();
        activeJobId = data.job_id;
        startPolling();
      } catch (err) {
        alert('Failed to start scraping task: ' + err);
        submitBtn.disabled = false;
      }
    });

    function startPolling() {
      if (pollInterval) clearInterval(pollInterval);
      pollInterval = setInterval(async () => {
        if (!activeJobId) return;

        try {
          const resp = await fetch(`/api/jobs/${activeJobId}`);
          const job = await resp.json();

          // Render Logs
          logContainer.innerHTML = job.logs.map(l => 
            `<div><span class="text-indigo-400">[${l.time}]</span> <span class="text-slate-300">${escapeHtml(l.message)}</span></div>`
          ).join('');
          logContainer.scrollTop = logContainer.scrollHeight;

          // Update Steps
          updateSteps(job.current_step);

          if (job.domain) {
            jobDomainSub.innerText = `Domain: ${job.domain} | Landing: ${job.homepage}`;
          }

          if (job.status === 'completed') {
            clearInterval(pollInterval);
            submitBtn.disabled = false;
            badgeStatus.className = 'inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
            badgeStatus.innerText = 'Completed';
            renderResults(job);
          } else if (job.status === 'failed') {
            clearInterval(pollInterval);
            submitBtn.disabled = false;
            badgeStatus.className = 'inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-rose-500/20 text-rose-300 border border-rose-500/30';
            badgeStatus.innerText = 'Failed';
          }
        } catch (e) {
          console.error(e);
        }
      }, 1500);
    }

    function updateSteps(step) {
      for (let s = 1; s <= 4; s++) {
        const box = document.getElementById(`stepBox${s}`);
        if (!box) continue;
        if (s <= step) {
          box.className = 'p-3 rounded-xl bg-indigo-950/40 border border-indigo-500/40 text-indigo-200 transition';
        } else {
          box.className = 'p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-slate-500 transition';
        }
      }
    }

    function renderResults(job) {
      if (!job.result || !job.result.faculty) return;
      resultsCard.classList.remove('hidden');
      currentFacultyData = job.result.faculty;

      resultTitle.innerText = `${job.result.institution_name} Leadership & Faculty`;
      resultMeta.innerText = `Extracted from ${job.result.canonical_domain} across ${job.result.discovered_directories.length} directory portal(s)`;

      downloadCsvBtn.href = `/api/export/${job.id}?format=csv`;
      downloadJsonBtn.href = `/api/export/${job.id}?format=json`;

      displayFacultyRows(currentFacultyData);
    }

    function displayFacultyRows(facultyList) {
      recordCount.innerText = `${facultyList.length} Profile(s)`;
      if (!facultyList.length) {
        facultyTableBody.innerHTML = `<tr><td colspan="6" class="py-8 text-center text-slate-500">No profiles found matching criteria</td></tr>`;
        return;
      }

      facultyTableBody.innerHTML = facultyList.map(f => {
        let rankBadge = '';
        if (f.rank_score >= 85) {
          rankBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-purple-500/20 text-purple-300 border border-purple-500/30">L-${f.rank_score}</span>`;
        } else if (f.rank_score >= 70) {
          rankBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-blue-500/20 text-blue-300 border border-blue-500/30">HOD-${f.rank_score}</span>`;
        } else {
          rankBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">P-${f.rank_score}</span>`;
        }

        return `
          <tr class="hover:bg-slate-900/50 transition">
            <td class="py-3 px-4">${rankBadge}</td>
            <td class="py-3 px-4">
              <div class="font-semibold text-white">${escapeHtml(f.name)}</div>
              ${f.description ? `<div class="text-[11px] text-slate-400 line-clamp-1 max-w-xs mt-0.5">${escapeHtml(f.description)}</div>` : ''}
            </td>
            <td class="py-3 px-4 text-slate-200 font-medium">${escapeHtml(f.designation)}</td>
            <td class="py-3 px-4 text-slate-400">${escapeHtml(f.department || '—')}</td>
            <td class="py-3 px-4">
              ${f.email ? `<a href="mailto:${escapeHtml(f.email)}" class="text-indigo-400 hover:underline flex items-center space-x-1"><i class="fa-regular fa-envelope text-[10px]"></i><span>${escapeHtml(f.email)}</span></a>` : '<span class="text-slate-600">—</span>'}
            </td>
            <td class="py-3 px-4 text-right">
              <a href="${escapeHtml(f.profile_url)}" target="_blank" class="inline-flex items-center space-x-1 text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 px-2.5 py-1 rounded-lg transition text-[11px]">
                <span>View</span>
                <i class="fa-solid fa-arrow-up-right-from-square text-[9px]"></i>
              </a>
            </td>
          </tr>
        `;
      }).join('');
    }

    filterInput.addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase().trim();
      if (!q) {
        displayFacultyRows(currentFacultyData);
        return;
      }
      const filtered = currentFacultyData.filter(f => 
        (f.name && f.name.toLowerCase().includes(q)) ||
        (f.designation && f.designation.toLowerCase().includes(q)) ||
        (f.department && f.department.toLowerCase().includes(q)) ||
        (f.email && f.email.toLowerCase().includes(q))
      );
      displayFacultyRows(filtered);
    });

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
