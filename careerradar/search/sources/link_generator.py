import urllib.parse

class LinkGenerator:
    COUNTRY_LOCATIONS = {
        "US": "United States",
        "FI": "Finland",
        "SE": "Sweden",
        "NO": "Norway",
        "DK": "Denmark"
    }
    
    INDEED_DOMAINS = {
        "US": "www.indeed.com",
        "FI": "fi.indeed.com",
        "SE": "se.indeed.com",
        "NO": "no.indeed.com",
        "DK": "dk.indeed.com"
    }

    @staticmethod
    def generate_links(skills, country, query):
        """
        Generates LinkedIn and Indeed search links based on keywords and target country.
        """
        location = LinkGenerator.COUNTRY_LOCATIONS.get(country, country)
        indeed_domain = LinkGenerator.INDEED_DOMAINS.get(country, "www.indeed.com")
        
        # Select the top 4-5 skills to keep the search query concise and highly relevant
        # Filter for known major tech terms
        major_skills = [
            s for s in skills 
            if s.lower() in [
                "python", "typescript", "react", "fastapi", "docker", "terraform", "aws", 
                "postgresql", "ansible", "kubernetes", "django", "node.js", "php", "web sockets",
                "vllm", "ollama", "devops"
            ]
        ]
        if not major_skills:
            major_skills = skills[:4]
        else:
            major_skills = major_skills[:4]

        # Formulate boolean search keyword query
        # Example: "(Software Engineer) AND (Python OR React OR Docker)"
        keywords = f'"{query}"'
        if major_skills:
            skills_or = " OR ".join([f'"{s}"' for s in major_skills])
            keywords += f" AND ({skills_or})"
            
        encoded_keywords = urllib.parse.quote(keywords)
        encoded_location = urllib.parse.quote(location)
        encoded_query = urllib.parse.quote(query)

        # LinkedIn Job Search URL
        linkedin_url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_keywords}&location={encoded_location}"
        
        # Indeed Search URL
        indeed_url = f"https://{indeed_domain}/jobs?q={encoded_keywords}&l={encoded_location}"
        
        return {
            "linkedin": linkedin_url,
            "indeed": indeed_url,
            "search_query_used": keywords,
            "location_used": location
        }
