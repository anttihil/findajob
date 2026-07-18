import unittest
import os
import sys
import tempfile
import shutil
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.sources.gmail_imap import GmailIMAPSource, EmailJobParser
from backend.digest import DigestGenerator
from backend.database import Database
from backend.matcher import JobMatcher

class TestGmailAndDigest(unittest.TestCase):
    
    def test_email_job_parser_linkedin(self):
        """Test EmailJobParser on LinkedIn job alert HTML"""
        html_content = """
        <html>
            <body>
                <table>
                    <tr>
                        <td>
                            <a href="https://www.linkedin.com/jobs/view/123456789/">Software Engineer</a>
                        </td>
                    </tr>
                    <tr>
                        <td>Google</td>
                    </tr>
                    <tr>
                        <td>Mountain View, CA • 1 day ago</td>
                    </tr>
                </table>
            </body>
        </html>
        """
        parser = EmailJobParser()
        parser.feed(html_content)
        
        # Verify elements are parsed
        links = [el for el in parser.elements if el[0] == 'link']
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][1], "https://www.linkedin.com/jobs/view/123456789/")
        self.assertEqual(links[0][2], "Software Engineer")

    def test_gmail_imap_source_linkedin_parsing(self):
        """Test that GmailIMAPSource parses LinkedIn alerts into job listings"""
        source = GmailIMAPSource("test@gmail.com", "fake_pass")
        
        email_html = """
        <html>
            <body>
                <table class="job-card">
                    <tr>
                        <td>
                            <a href="https://www.linkedin.com/jobs/view/999888/">Senior Fullstack Engineer</a>
                        </td>
                    </tr>
                    <tr>
                        <td>Netflix • Los Gatos, CA</td>
                    </tr>
                    <tr>
                        <td>We are looking for a Senior Fullstack Engineer with Python and React skills.</td>
                    </tr>
                </table>
            </body>
        </html>
        """
        
        jobs = source._parse_linkedin_alert(email_html, "LinkedIn Job Alert", "US")
        
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["title"], "Senior Fullstack Engineer")
        self.assertEqual(job["company"], "Netflix")
        self.assertEqual(job["location"], "Los Gatos, CA")
        self.assertEqual(job["url"], "https://www.linkedin.com/jobs/view/999888/")
        self.assertEqual(job["source"], "Gmail-LinkedIn")
        self.assertEqual(job["job_key"], "gmail-linkedin-999888")

    def test_gmail_imap_source_indeed_parsing(self):
        """Test that GmailIMAPSource parses Indeed alerts into job listings"""
        source = GmailIMAPSource("test@gmail.com", "fake_pass")
        
        email_html = """
        <html>
            <body>
                <div class="job">
                    <a href="https://www.indeed.com/viewjob?jk=indeedjk123">AI Engineer</a>
                    <br>
                    <span>OpenAI</span>
                    <br>
                    <span>San Francisco, CA</span>
                    <p>Build AI tools using Python, PyTorch and Large Language Models.</p>
                </div>
            </body>
        </html>
        """
        
        jobs = source._parse_indeed_alert(email_html, "Indeed Job Alert", "US")
        
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["title"], "AI Engineer")
        self.assertEqual(job["company"], "OpenAI")
        self.assertEqual(job["location"], "San Francisco, CA")
        self.assertEqual(job["url"], "https://www.indeed.com/viewjob?jk=indeedjk123")
        self.assertEqual(job["source"], "Gmail-Indeed")
        self.assertEqual(job["job_key"], "gmail-indeed-indeedjk123")

    def test_gmail_imap_source_direct_parsing(self):
        """Test parsing of direct recruiter emails"""
        source = GmailIMAPSource("test@gmail.com", "fake_pass")
        
        body_text = "Hi, we are looking for a DevOps Engineer with Kubernetes and Terraform experience at Amazon. Let me know if you are interested!"
        job = source._parse_direct_email("recruiter@amazon.com", "DevOps Engineer Role", body_text, "US")
        
        self.assertIsNotNone(job)
        self.assertEqual(job["title"], "DevOps Engineer Role")
        self.assertEqual(job["company"], "Amazon")
        self.assertEqual(job["location"], "Remote")
        self.assertEqual(job["url"], "mailto:recruiter@amazon.com")
        self.assertEqual(job["source"], "Gmail-Direct")
        self.assertTrue(job["job_key"].startswith("gmail-direct-"))

    @patch("backend.digest.load_config")
    def test_digest_generation(self, mock_load_config):
        """Test DigestGenerator queries database and creates a markdown report"""
        # Create a temp directory for testing
        temp_dir = tempfile.mkdtemp()
        temp_db = os.path.join(temp_dir, "test_jobs.db")
        
        import backend.database
        original_db_path = backend.database.DB_PATH
        backend.database.DB_PATH = temp_db
        
        mock_load_config.return_value = {
            "digest": {
                "enabled": True,
                "min_score_for_digest": 40
            }
        }
        
        # Initialize temp DB schema
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
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
                matched_skills TEXT,
                resume_match TEXT,
                status TEXT DEFAULT 'unread',
                date_found TEXT,
                date_applied TEXT
            )
        """)
        
        # Insert a matching job
        now_str = datetime.now().isoformat()
        cursor.execute("""
            INSERT INTO jobs (job_key, title, company, location, country, url, description, source, match_score, date_found)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("key-1", "React Developer", "Facebook", "Menlo Park", "US", "http://facebook.com/job", "React skills", "TestScraper", 85, now_str))
        
        # Insert a non-matching job (low score)
        cursor.execute("""
            INSERT INTO jobs (job_key, title, company, location, country, url, description, source, match_score, date_found)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("key-2", "Sales Rep", "Oracle", "Austin", "US", "http://oracle.com/job", "Sales experience", "TestScraper", 20, now_str))
        
        conn.commit()
        conn.close()
        
        try:
            generator = DigestGenerator()
            generator.output_dir = os.path.join(temp_dir, "digests")
            
            filepath = generator.generate_digest(hours_ago=2)
            
            self.assertTrue(os.path.exists(filepath))
            with open(filepath, "r") as f:
                content = f.read()
                
            self.assertIn("Job Search Digest", content)
            self.assertIn("React Developer", content)
            self.assertIn("Facebook", content)
            self.assertIn("85%", content)
            self.assertNotIn("Sales Rep", content)  # Low score excluded
            
            # Test listing digests
            digests = generator.list_digests()
            self.assertEqual(len(digests), 1)
            self.assertEqual(digests[0]["filename"], os.path.basename(filepath))
            
            # Test content retrieval
            content_retrieved = generator.get_digest_content(os.path.basename(filepath))
            self.assertEqual(content_retrieved, content)
            
        finally:
            backend.database.DB_PATH = original_db_path
            shutil.rmtree(temp_dir)

if __name__ == "__main__":
    unittest.main()
