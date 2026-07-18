import os
import sys
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_config, save_config
from backend.database import Database
from backend.resume_parser import ResumeParser
from backend.sources.link_generator import LinkGenerator
from sync import run_sync

app = FastAPI(title="Job Search Automation Dashboard")

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

# Database helper
def get_db():
    return Database()

# --- Request/Response Models ---
class StatusUpdate(BaseModel):
    status: str

class ConfigUpdate(BaseModel):
    countries: List[str]
    search_queries: List[str]
    min_match_score: int
    sources: dict
    adzuna_credentials: Optional[dict] = None

# --- API Routes ---

@app.get("/api/jobs")
def get_jobs(
    status: Optional[str] = None, 
    country: Optional[str] = None, 
    resume_match: Optional[str] = None,
    min_score: Optional[int] = None
):
    db = get_db()
    try:
        jobs = db.get_jobs(
            status=status, 
            country=country, 
            resume_match=resume_match, 
            min_score=min_score
        )
        return jobs
    finally:
        db.close()

@app.put("/api/jobs/{job_id}/status")
def update_job_status(job_id: int, payload: StatusUpdate):
    if payload.status not in ["unread", "saved", "applied", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid status value")
        
    db = get_db()
    try:
        success = db.update_job_status(job_id, payload.status)
        if not success:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"success": True, "status": payload.status}
    finally:
        db.close()

@app.get("/api/stats")
def get_stats():
    db = get_db()
    try:
        return db.get_stats()
    finally:
        db.close()

@app.get("/api/config")
def get_current_config():
    return load_config()

@app.post("/api/config")
def update_current_config(payload: ConfigUpdate):
    config = load_config()
    config["countries"] = payload.countries
    config["search_queries"] = payload.search_queries
    config["min_match_score"] = payload.min_match_score
    config["sources"] = payload.sources
    
    if payload.adzuna_credentials:
        config["adzuna_credentials"] = payload.adzuna_credentials
        
    save_config(config)
    return {"success": True, "config": config}

@app.get("/api/resumes")
def get_parsed_resumes():
    config = load_config()
    resumes_dir = os.path.join(BASE_DIR, "resumes")
    if config.get("resumes_dir"):
        resumes_dir = os.path.abspath(os.path.join(BASE_DIR, config["resumes_dir"]))
        
    parser = ResumeParser(resumes_dir)
    return parser.parse_all()

@app.get("/api/search-links")
def get_search_links(resume: str, country: str, query: str):
    config = load_config()
    resumes_dir = os.path.join(BASE_DIR, "resumes")
    if config.get("resumes_dir"):
        resumes_dir = os.path.abspath(os.path.join(BASE_DIR, config["resumes_dir"]))
        
    parser = ResumeParser(resumes_dir)
    resumes = parser.parse_all()
    
    if resume not in resumes:
        raise HTTPException(status_code=404, detail="Resume file not found")
        
    skills = resumes[resume]["skills"]
    links = LinkGenerator.generate_links(skills, country, query)
    return links

from backend.status_manager import load_sync_status, set_sync_progress

def bg_sync_task():
    run_sync()

@app.post("/api/sync")
def trigger_sync(background_tasks: BackgroundTasks):
    status = load_sync_status()
    if status.get("sync_in_progress"):
        return JSONResponse(status_code=409, content={"message": "Sync is already in progress"})
        
    set_sync_progress(True)
    background_tasks.add_task(bg_sync_task)
    return {"message": "Sync triggered in background"}

@app.get("/api/sync/status")
def get_sync_status():
    return load_sync_status()

# --- Digests API ---
from backend.digest import DigestGenerator

@app.get("/api/digests")
def list_digests():
    generator = DigestGenerator()
    return generator.list_digests()

@app.get("/api/digests/{filename}")
def get_digest_content(filename: str):
    generator = DigestGenerator()
    content = generator.get_digest_content(filename)
    if not content:
        raise HTTPException(status_code=404, detail="Digest not found")
    return {"content": content}

# --- Static Frontend Serving ---

# Ensure frontend directory exists
os.makedirs(FRONTEND_DIR, exist_ok=True)
os.makedirs(os.path.join(FRONTEND_DIR, "css"), exist_ok=True)
os.makedirs(os.path.join(FRONTEND_DIR, "js"), exist_ok=True)

# Mount static files (served under /static)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Catch-all endpoint to serve index.html at root '/'
@app.get("/")
def serve_home():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Frontend dashboard index.html not found yet.</h1>")

from fastapi.responses import HTMLResponse
