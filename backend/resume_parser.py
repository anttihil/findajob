import os
import re

class ResumeParser:
    def __init__(self, resumes_dir=None):
        if resumes_dir is None:
            self.resumes_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                "resumes"
            )
        else:
            self.resumes_dir = resumes_dir

    def parse_all(self):
        """
        Parses all .md files in the resumes directory.
        Returns a dictionary: {filename: {title, skills}}
        """
        if not os.path.exists(self.resumes_dir):
            print(f"Resumes directory not found at: {self.resumes_dir}")
            return {}
            
        resumes = {}
        for filename in os.listdir(self.resumes_dir):
            if filename.endswith(".md"):
                path = os.path.join(self.resumes_dir, filename)
                resumes[filename] = self.parse_file(path)
        return resumes

    def parse_file(self, path):
        """
        Parses a single resume markdown file.
        """
        skills = set()
        
        # Read the file
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # 1. Determine title: Use filename or first H1/H2 in the resume
        title = os.path.basename(path).replace(".md", "").replace("_", " ").title()
        # Fallback to H1/H2 match
        first_line = content.split('\n')[0]
        if first_line.startswith("# ") and "hiltunen" not in first_line.lower():
            title = first_line.replace("# ", "").strip()

        # 2. Extract Skills Section
        # Matches everything between "## Skills" and the next heading or end of file
        skills_match = re.search(r"## Skills\n(.*?)(?=\n##|$)", content, re.DOTALL | re.IGNORECASE)
        if skills_match:
            skills_block = skills_match.group(1)
            # Find bullet points: "* **Category:** Item1, Item2" or "* Item"
            bullets = re.findall(r"^\s*[\*\-]\s+(.*)$", skills_block, re.MULTILINE)
            for bullet in bullets:
                # Strip category prefix if present (e.g., "**Frontend:**")
                clean_bullet = re.sub(r"^\s*\*\*[^*]+:\*\*\s*", "", bullet)
                # Split by commas and semicolons
                parts = re.split(r",|;|\band\b", clean_bullet)
                for part in parts:
                    # Remove parenthetical notes like "(basic)", "(FastAPI/Flask)"
                    part_clean = re.sub(r"\([^)]+\)", "", part)
                    # Remove surrounding asterisks, dots, spaces
                    part_clean = part_clean.strip().strip("*").strip(".").strip()
                    if part_clean and len(part_clean) > 1:
                        skills.add(part_clean)
        
        # 3. Scan for specific high-value technologies in other sections to ensure they are captured
        tech_keywords = [
            "vLLM", "Ollama", "Kysely", "Terraform", "Ansible", "Gutenberg", "Glide.js", 
            "Docker", "AWS", "FastAPI", "React", "TypeScript", "Python", "PostgreSQL",
            "WebSocket", "WebSockets", "SSM", "VPC", "IAM", "S3", "EC2", "chrony", 
            "Trellix", "Qualys", "WordFence", "iptables", "Apache", "Nginx", "Jenkins",
            "GitHub Actions", "ZMK", "Gutenberg Blocks"
        ]
        for kw in tech_keywords:
            # Check for word boundary match case-insensitively, but store the proper-cased term
            if re.search(r"\b" + re.escape(kw) + r"\b", content, re.IGNORECASE):
                skills.add(kw)

        # Remove some generic words that might cause false positives
        stop_skills = {"sql", "html5", "css3", "api", "apis", "basic"}
        final_skills = []
        for s in skills:
            if s.lower() not in stop_skills:
                final_skills.append(s)

        return {
            "title": title,
            "skills": sorted(final_skills)
        }

if __name__ == "__main__":
    # Quick test
    parser = ResumeParser()
    res = parser.parse_all()
    for filename, info in res.items():
        print(f"Resume: {filename} ({info['title']})")
        print(f"Skills ({len(info['skills'])}): {', '.join(info['skills'][:10])}...")
