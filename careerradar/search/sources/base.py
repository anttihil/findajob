from abc import ABC, abstractmethod

class BaseJobSource(ABC):
    @abstractmethod
    def fetch_jobs(self, country: str, query: str) -> list[dict]:
        """
        Fetches job listings from the source.
        
        country: Two-letter country code (US, SE, NO, DK, FI).
        query: The search term (e.g. 'Software Engineer').
        
        Returns a list of dictionaries with the following structure:
        {
            "job_key": str (unique key for deduplication),
            "title": str,
            "company": str,
            "location": str,
            "country": str,
            "url": str,
            "description": str,
            "source": str
        }
        """
        pass
