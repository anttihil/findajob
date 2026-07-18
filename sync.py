import os
import sys
from datetime import datetime

# Add the project root to path so we can import from backend
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from backend.config import load_config
from backend.resume_parser import ResumeParser
from backend.database import Database
from backend.matcher import JobMatcher
from backend.logger import get_logger
from backend.status_manager import set_sync_progress, add_sync_error

logger = get_logger()

def run_sync():
    logger.info("=========================================")
    logger.info("Starting job search synchronization run (Gmail IMAP)...")
    logger.info("=========================================")
    
    # 1. Update status to active
    set_sync_progress(True)
    
    total_fetched = 0
    total_new = 0
    total_evaluated = 0
    
    db = None
    try:
        config = load_config()
        db = Database()
        
        # 1. Parse resumes
        resumes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resumes")
        if config.get("resumes_dir"):
            resumes_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), config["resumes_dir"]))
            
        logger.info(f"Reading resumes from directory: {resumes_dir}")
        parser = ResumeParser(resumes_dir)
        resumes = parser.parse_all()
        
        if not resumes:
            logger.error("No resumes found in the resumes directory. Aborting sync.")
            add_sync_error("Parser", "No resumes found in the resumes directory.")
            return
            
        logger.info(f"Loaded {len(resumes)} resumes successfully:")
        for filename, info in resumes.items():
            logger.info(f"  Profile: {filename} | Title: '{info['title']}' | Skills: {len(info['skills'])}")

        # 2. Setup Matcher
        matcher = JobMatcher(resumes)
        
        # 3. Connect to Gmail and Fetch Email Job Listings
        gmail_conf = config.get("gmail_imap", {})
        if not gmail_conf.get("enabled", False):
            logger.warning("Gmail IMAP is disabled in settings. Skipping sync.")
            add_sync_error("GmailIMAP", "Gmail IMAP is disabled in settings.")
            return

        email_addr = os.environ.get("GMAIL_EMAIL") or gmail_conf.get("email")
        app_pass = os.environ.get("GMAIL_APP_PASSWORD")
        
        if not email_addr or not app_pass:
            logger.error("Gmail IMAP is enabled but GMAIL_EMAIL and/or GMAIL_APP_PASSWORD are not set.")
            add_sync_error("GmailIMAP", "Missing credentials (GMAIL_EMAIL / GMAIL_APP_PASSWORD).")
            return

        from backend.sources.gmail_imap import GmailIMAPSource
        scraper = GmailIMAPSource(
            email_user=email_addr,
            app_password=app_pass,
            imap_server=gmail_conf.get("imap_server", "imap.gmail.com"),
            imap_port=gmail_conf.get("imap_port", 993)
        )
        
        logger.info("Connecting to Gmail IMAP to fetch job alert emails...")
        # Fetching jobs (default country US, query will be ignored by IMAP internally)
        jobs = scraper.fetch_jobs(country="US", query="")
        total_fetched = len(jobs)
        
        min_score = config.get("min_match_score", 15)
        seen_keys = set()
        
        for job in jobs:
            key = job["job_key"]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            
            total_evaluated += 1
            
            # Match evaluation
            best_resume, score, matched_skills = matcher.evaluate_job(
                job["title"], 
                job["description"]
            )
            
            if score < min_score:
                continue
                
            job["match_score"] = score
            job["matched_skills"] = matched_skills
            job["resume_match"] = best_resume
            
            is_new = db.add_job(job)
            if is_new:
                total_new += 1
                logger.info(f"  [NEW MATCH - {score}%] '{job['title']}' at {job['company']} (Matched: {best_resume})")
                
        logger.info("=========================================")
        logger.info("Job search synchronization run finished.")
        logger.info(f"Total postings scraped across sources: {total_fetched}")
        logger.info(f"Total unique postings evaluated: {total_evaluated}")
        logger.info(f"Total new matching positions saved (score >= {min_score}%): {total_new}")
        logger.info("=========================================\n")
        
        # 4. Digest Generation
        if config.get("digest", {}).get("enabled", True):
            try:
                from backend.digest import DigestGenerator
                generator = DigestGenerator()
                digest_file = generator.generate_digest(hours_ago=24)
                if digest_file:
                    logger.info(f"Digest generated successfully: {digest_file}")
                else:
                    logger.info("No high-matching jobs found for digest during this timeframe.")
            except Exception as e:
                logger.error(f"Failed to generate digest: {e}", exc_info=True)
                add_sync_error("Digest", f"Generation error: {e}")
                
    except Exception as e:
        logger.error(f"Critical error in sync run: {e}", exc_info=True)
        add_sync_error("Engine", f"Critical run failure: {e}")
    finally:
        if db:
            db.close()
        # Mark sync as completed with statistics
        set_sync_progress(False, total_fetched, total_evaluated, total_new)

if __name__ == "__main__":
    run_sync()
