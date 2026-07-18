import re

class JobMatcher:
    def __init__(self, resumes_dict):
        """
        resumes_dict: dict of {resume_filename: {title, skills}}
        """
        self.resumes = resumes_dict

    def evaluate_job(self, title, description):
        """
        Evaluates a job listing against all resumes.
        Returns a tuple: (best_resume_filename, best_score, matched_skills)
        """
        if not self.resumes:
            return None, 0, []

        best_resume = None
        best_score = 0
        best_skills = []

        title_lower = title.lower()
        desc_lower = description.lower()

        for filename, resume_info in self.resumes.items():
            skills = resume_info["skills"]
            matched_skills = []
            score_acc = 0

            for skill in skills:
                skill_lower = skill.lower()
                
                # Check for word boundary match
                # Handle potential special characters in skills like Glide.js or ReactJS
                # If skill has special chars, we escape them, but we still want word boundary protection
                if '.' in skill_lower or '/' in skill_lower or '-' in skill_lower:
                    pattern = re.escape(skill_lower)
                else:
                    pattern = r'\b' + re.escape(skill_lower) + r'\b'

                if re.search(pattern, desc_lower):
                    matched_skills.append(skill)
                    
                    # Boost if the skill is prominently featured in the job title
                    if re.search(pattern, title_lower):
                        score_acc += 25
                    else:
                        score_acc += 5
            
            # Normalize the score (we want a percentage style score from 0 to 100)
            # Give a base score for the density of matches relative to resume size, 
            # and a scale factor for absolute matches.
            if matched_skills:
                # Calculate score:
                # 1. Base score for each match: 5-8 points
                # 2. Add title boosts
                # 3. Maximum score capped at 100
                total_score = min(100, score_acc)
            else:
                total_score = 0

            if total_score > best_score:
                best_score = total_score
                best_resume = filename
                best_skills = matched_skills

        # If no resume scored above 0, match it to the first resume with score 0
        if best_score == 0 and self.resumes:
            best_resume = list(self.resumes.keys())[0]
            best_skills = []

        return best_resume, best_score, best_skills
