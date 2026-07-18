import unittest
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.resume_parser import ResumeParser
from backend.matcher import JobMatcher

class TestJobSearchAutomation(unittest.TestCase):
    
    def setUp(self):
        self.project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Target real resumes directory for testing if it exists
        self.resumes_dir = os.path.join(os.path.dirname(self.project_dir), "resumes")
        
    def test_resume_parser_execution(self):
        """Verify resume parser can run and find files, producing expected shapes."""
        if not os.path.exists(self.resumes_dir):
            self.skipTest("Resumes directory not found at " + self.resumes_dir)
            
        parser = ResumeParser(self.resumes_dir)
        resumes = parser.parse_all()
        
        self.assertGreater(len(resumes), 0, "Parser did not find any resume files.")
        
        for filename, data in resumes.items():
            self.assertIn("title", data)
            self.assertIn("skills", data)
            self.assertIsInstance(data["skills"], list)
            # Ensure skills are extracted
            self.assertGreater(len(data["skills"]), 0, f"No skills extracted from {filename}")
            
    def test_matcher_scoring(self):
        """Verify matching score calculations are correct and deterministic."""
        dummy_resumes = {
            "fullstack.md": {
                "title": "Fullstack AI Engineer",
                "skills": ["Python", "React", "Docker", "vLLM", "PostgreSQL", "AWS"]
            },
            "devops.md": {
                "title": "Platform DevOps Engineer",
                "skills": ["Docker", "Terraform", "Ansible", "AWS", "Bash", "nginx"]
            }
        }
        
        matcher = JobMatcher(dummy_resumes)
        
        # Test 1: High match score on title & description
        title = "Python and React Developer with Docker"
        description = "We are looking for a Python developer with React experience who can manage Docker containers in AWS."
        best_resume, score, matched = matcher.evaluate_job(title, description)
        
        self.assertEqual(best_resume, "fullstack.md")
        # Python, React, Docker are in title and desc -> 25 * 3 = 75 points
        # AWS is in desc -> 5 points
        # Total score should cap/normalize to high value
        self.assertGreaterEqual(score, 75)
        self.assertIn("Python", matched)
        self.assertIn("React", matched)
        self.assertIn("Docker", matched)
        self.assertIn("AWS", matched)
        self.assertNotIn("vLLM", matched)
        
        # Test 2: DevOps matches
        title = "DevOps Platform Engineer"
        description = "Skills: Terraform, Ansible, and Docker. Experience with nginx is a plus."
        best_resume, score, matched = matcher.evaluate_job(title, description)
        
        self.assertEqual(best_resume, "devops.md")
        self.assertIn("Terraform", matched)
        self.assertIn("Ansible", matched)
        self.assertIn("Docker", matched)
        self.assertIn("nginx", matched)
        self.assertNotIn("Bash", matched)

if __name__ == "__main__":
    unittest.main()
