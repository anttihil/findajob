import urllib.request
import urllib.error
import time
import random
from backend.logger import get_logger

logger = get_logger()

def safe_request(url, headers=None, timeout=12, max_retries=3, base_delay=2):
    """
    Performs an HTTP request with exponential backoff for transient errors.
    
    Transient errors retried:
      - HTTP 429 (Too Many Requests)
      - HTTP 500, 502, 503, 504 (Server errors)
      - URLError (DNS, Connection timed out)
      - TimeoutError / ConnectionResetError
      
    Non-transient errors (failed immediately):
      - HTTP 400, 401, 403, 404 (Client configuration/auth issues)
    """
    if headers is None:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        }
        
    req = urllib.request.Request(url, headers=headers)
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.debug(f"HTTP: Requesting {url} (Attempt {attempt}/{max_retries})")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status = response.getcode()
                logger.debug(f"HTTP: Success status {status} from {url}")
                return response.read(), status
                
        except urllib.error.HTTPError as e:
            status = e.code
            # Handle rate limiting or server errors with backoff
            if status in [429, 500, 502, 503, 504]:
                if attempt == max_retries:
                    logger.error(f"HTTP: Maximum retries reached. Request to {url} failed with status {status} ({e.reason})")
                    raise e
                    
                # Exponential backoff with jitter
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.1, 1.0)
                logger.warning(f"HTTP: Transient status {status} ({e.reason}) from {url}. Retrying in {delay:.2f}s...")
                time.sleep(delay)
            else:
                # Client configuration errors: don't retry, fail immediately
                logger.error(f"HTTP: Permanent client error status {status} ({e.reason}) from {url}. Retrying will not help.")
                raise e
                
        except (urllib.error.URLError, TimeoutError, ConnectionResetError) as e:
            reason = getattr(e, 'reason', str(e))
            if attempt == max_retries:
                logger.error(f"HTTP: Maximum retries reached. Request to {url} failed. Error: {reason}")
                raise e
                
            # Network issue backoff
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.1, 1.0)
            logger.warning(f"HTTP: Connection failure ({reason}) to {url}. Retrying in {delay:.2f}s...")
            time.sleep(delay)
            
    # Fallback (should not be reached due to raising in loops)
    raise RuntimeError("HTTP request failed after maximum retries")
