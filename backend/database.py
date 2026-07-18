import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jobs.db")

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT UNIQUE,
                title TEXT NOT NULL,
                company TEXT,
                location TEXT,
                country TEXT,
                url TEXT UNIQUE,
                description TEXT,
                source TEXT,
                match_score INTEGER DEFAULT 0,
                matched_skills TEXT, -- JSON string
                resume_match TEXT, -- resume filename
                status TEXT DEFAULT 'unread', -- 'unread', 'saved', 'applied', 'rejected'
                date_found TEXT,
                date_applied TEXT
            )
        """)
        self.conn.commit()

    def add_job(self, job_data):
        """
        Inserts a job. Returns True if inserted, False if it already exists.
        If it exists and the match score is higher, updates it.
        """
        cursor = self.conn.cursor()
        
        # Check if job exists by url or job_key
        job_key = job_data.get("job_key") or job_data.get("url")
        cursor.execute("SELECT id, match_score, status FROM jobs WHERE job_key = ? OR url = ?", (job_key, job_data.get("url")))
        row = cursor.fetchone()
        
        skills_json = json.dumps(job_data.get("matched_skills", []))
        now_str = datetime.now().isoformat()
        
        if row:
            # Job exists. If the match score is higher, or it was unread, update it.
            existing_id = row["id"]
            existing_score = row["match_score"]
            new_score = job_data.get("match_score", 0)
            
            if new_score > existing_score:
                cursor.execute("""
                    UPDATE jobs SET 
                        title = ?, company = ?, location = ?, country = ?, 
                        description = ?, match_score = ?, matched_skills = ?, 
                        resume_match = ?
                    WHERE id = ?
                """, (
                    job_data["title"], job_data.get("company"), job_data.get("location"),
                    job_data.get("country"), job_data.get("description"), new_score,
                    skills_json, job_data.get("resume_match"), existing_id
                ))
                self.conn.commit()
            return False
            
        # New job, insert it
        try:
            cursor.execute("""
                INSERT INTO jobs (
                    job_key, title, company, location, country, url, 
                    description, source, match_score, matched_skills, 
                    resume_match, status, date_found
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?)
            """, (
                job_key,
                job_data["title"],
                job_data.get("company"),
                job_data.get("location"),
                job_data.get("country"),
                job_data.get("url"),
                job_data.get("description"),
                job_data.get("source"),
                job_data.get("match_score", 0),
                skills_json,
                job_data.get("resume_match"),
                now_str
            ))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_jobs(self, status=None, country=None, resume_match=None, min_score=None):
        cursor = self.conn.cursor()
        query = "SELECT * FROM jobs WHERE 1=1"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if country:
            query += " AND country = ?"
            params.append(country)
        if resume_match:
            query += " AND resume_match = ?"
            params.append(resume_match)
        if min_score is not None:
            query += " AND match_score >= ?"
            params.append(min_score)

        query += " ORDER BY match_score DESC, date_found DESC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        jobs = []
        for r in rows:
            job = dict(r)
            job["matched_skills"] = json.loads(r["matched_skills"]) if r["matched_skills"] else []
            jobs.append(job)
        return jobs

    def update_job_status(self, job_id, status):
        cursor = self.conn.cursor()
        now_str = datetime.now().isoformat() if status == "applied" else None
        
        if status == "applied":
            cursor.execute("UPDATE jobs SET status = ?, date_applied = ? WHERE id = ?", (status, now_str, job_id))
        else:
            cursor.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        self.conn.commit()
        return cursor.rowcount > 0

    def get_stats(self):
        cursor = self.conn.cursor()
        stats = {}
        
        # Total counts by status
        cursor.execute("SELECT status, COUNT(*) as count FROM jobs GROUP BY status")
        stats["status_counts"] = {r["status"]: r["count"] for r in cursor.fetchall()}
        for s in ["unread", "saved", "applied", "rejected"]:
            if s not in stats["status_counts"]:
                stats["status_counts"][s] = 0
                
        # Total crawled
        cursor.execute("SELECT COUNT(*) FROM jobs")
        stats["total_jobs"] = cursor.fetchone()[0]
        
        # Average match score
        cursor.execute("SELECT AVG(match_score) FROM jobs WHERE match_score > 0")
        avg = cursor.fetchone()[0]
        stats["avg_match_score"] = round(avg, 1) if avg else 0
        
        # Counts by country
        cursor.execute("SELECT country, COUNT(*) as count FROM jobs GROUP BY country")
        stats["country_counts"] = {r["country"]: r["count"] for r in cursor.fetchall() if r["country"]}
        
        # Counts by resume matching
        cursor.execute("SELECT resume_match, COUNT(*) as count FROM jobs GROUP BY resume_match")
        stats["resume_counts"] = {r["resume_match"]: r["count"] for r in cursor.fetchall() if r["resume_match"]}
        
        return stats

    def close(self):
        self.conn.close()
