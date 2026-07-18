import imaplib
import email
from email.header import decode_header
import re
import html
import hashlib
from html.parser import HTMLParser
from .base import BaseJobSource
from backend.logger import get_logger
from backend.status_manager import add_sync_error

logger = get_logger()

class EmailJobParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_anchor = False
        self.anchor_href = ""
        self.anchor_text = ""
        self.elements = []  # List of ('text', text) or ('link', href, text)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = ""
            for name, value in attrs:
                if name == "href":
                    href = value
            self.in_anchor = True
            self.anchor_href = href
            self.anchor_text = ""

    def handle_endtag(self, tag):
        if tag == "a" and self.in_anchor:
            self.in_anchor = False
            self.elements.append(('link', self.anchor_href, self.anchor_text.strip()))

    def handle_data(self, data):
        cleaned = data.strip()
        if not cleaned:
            return
        if self.in_anchor:
            self.anchor_text += data
        else:
            # Handle text outside anchors
            self.elements.append(('text', cleaned))

class GmailIMAPSource(BaseJobSource):
    def __init__(self, email_user, app_password, imap_server="imap.gmail.com", imap_port=993):
        self.email_user = email_user
        self.app_password = app_password
        self.imap_server = imap_server
        self.imap_port = imap_port

    def fetch_jobs(self, country: str, query: str) -> list[dict]:
        # country and query parameters are standard for BaseJobSource, 
        # but Gmail fetcher loads all job alerts matching predefined filters.
        # We will filter them internally.
        jobs = []
        mail = None
        try:
            logger.info(f"Gmail IMAP: Connecting to {self.imap_server}:{self.imap_port}...")
            mail = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
            mail.login(self.email_user, self.app_password)
            mail.select("inbox")
            
            # Search for unread emails
            status, messages = mail.search(None, 'UNSEEN')
            if status != "OK" or not messages[0]:
                logger.info("Gmail IMAP: No unread emails found.")
                return []

            email_ids = messages[0].split()
            logger.info(f"Gmail IMAP: Found {len(email_ids)} unread emails.")

            for e_id in email_ids:
                try:
                    # Fetch full email message
                    status, data = mail.fetch(e_id, '(RFC822)')
                    if status != "OK":
                        continue
                    
                    raw_email = data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    # Extract Sender
                    sender = msg.get("From", "")
                    sender_clean = self._clean_header(sender)
                    
                    # Extract Subject
                    subject = msg.get("Subject", "")
                    subject_clean = self._clean_header(subject)
                    
                    logger.info(f"Gmail IMAP: Processing email ID {e_id.decode()} | From: '{sender_clean}' | Subject: '{subject_clean}'")
                    
                    # Extract Body
                    body_html, body_text = self._extract_body(msg)
                    
                    # Determine source and parse jobs
                    email_jobs = []
                    if "linkedin" in sender_clean.lower() or "linkedin" in subject_clean.lower():
                        email_jobs = self._parse_linkedin_alert(body_html or body_text, subject_clean, country)
                    elif "indeed" in sender_clean.lower() or "indeed" in subject_clean.lower():
                        email_jobs = self._parse_indeed_alert(body_html or body_text, subject_clean, country)
                    else:
                        # Direct correspondence or general job email
                        job = self._parse_direct_email(sender_clean, subject_clean, body_text or body_html, country)
                        if job:
                            email_jobs = [job]
                    
                    if email_jobs:
                        logger.info(f"Gmail IMAP: Extracted {len(email_jobs)} job postings from email ID {e_id.decode()}")
                        jobs.extend(email_jobs)
                        
                    # Mark email as read/seen
                    # mail.store(e_id, '+FLAGS', '\\Seen') # Done automatically by IMAP fetch, but we can verify it
                except Exception as ex:
                    logger.error(f"Error parsing email ID {e_id.decode()}: {ex}", exc_info=True)
                    
            mail.logout()
        except Exception as e:
            err_msg = f"Failed to fetch jobs from Gmail IMAP: {e}"
            logger.error(err_msg, exc_info=True)
            add_sync_error("GmailIMAP", err_msg)
        finally:
            if mail:
                try:
                    mail.close()
                except:
                    pass
        return jobs

    def _clean_header(self, header_val):
        if not header_val:
            return ""
        decoded = decode_header(header_val)
        parts = []
        for part, encoding in decoded:
            if isinstance(part, bytes):
                try:
                    parts.append(part.decode(encoding or "utf-8", errors="ignore"))
                except:
                    parts.append(part.decode("latin-1", errors="ignore"))
            else:
                parts.append(str(part))
        return "".join(parts)

    def _extract_body(self, msg):
        body_html = ""
        body_text = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                
                # Skip attachments
                if "attachment" in content_disposition:
                    continue
                    
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_text += payload.decode("utf-8", errors="ignore")
                elif content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_html += payload.decode("utf-8", errors="ignore")
        else:
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                if content_type == "text/html":
                    body_html = payload.decode("utf-8", errors="ignore")
                else:
                    body_text = payload.decode("utf-8", errors="ignore")
                    
        return body_html, body_text

    def _parse_linkedin_alert(self, content, subject, country):
        parser = EmailJobParser()
        parser.feed(content)
        
        jobs = []
        for idx, el in enumerate(parser.elements):
            if el[0] == 'link':
                href = el[1]
                title = el[2]
                
                # Check for LinkedIn job link
                if "linkedin.com/jobs/view/" in href or "linkedin.com/comm/jobs/view/" in href:
                    if not title or len(title) < 3 or "view job" in title.lower() or "apply" in title.lower():
                        continue
                        
                    # Find surrounding text blocks for company and location
                    subseq = []
                    for offset in range(1, 5):
                        if idx + offset < len(parser.elements):
                            neighbor = parser.elements[idx + offset]
                            if neighbor[0] == 'text':
                                subseq.append(neighbor[1])
                            elif neighbor[0] == 'link' and ("linkedin.com/company" in neighbor[1] or "linkedin.com/comm/company" in neighbor[1]):
                                subseq.append(neighbor[2])
                    
                    company = "Unknown"
                    location = "Remote"
                    
                    for text in subseq:
                        text = text.replace('\xa0', ' ').strip()
                        if '•' in text:
                            parts = [p.strip() for p in text.split('•')]
                            if len(parts) >= 2:
                                company = parts[0]
                                location = parts[1]
                                break
                        elif ' | ' in text:
                            parts = [p.strip() for p in text.split('|')]
                            if len(parts) >= 2:
                                company = parts[0]
                                location = parts[1]
                                break
                                
                    if company == "Unknown" and subseq:
                        if len(subseq[0]) < 100:
                            company = subseq[0]
                        if len(subseq) > 1 and len(subseq[1]) < 100:
                            location = subseq[1]
                            
                    # Clean up company / location
                    company = company.strip()
                    location = location.strip()
                    
                    # Create job key
                    # Extract job id from link if possible
                    job_id_match = re.search(r'/view/(\d+)', href)
                    job_id = job_id_match.group(1) if job_id_match else hashlib.md5(href.encode()).hexdigest()[:12]
                    job_key = f"gmail-linkedin-{job_id}"
                    
                    # Use nearby text blocks as description/snippet
                    snippet = " ".join([s for s in subseq if len(s) > 15][:2])
                    if not snippet:
                        snippet = f"Job alert listing for {title} at {company} in {location}."
                        
                    jobs.append({
                        "job_key": job_key,
                        "title": title,
                        "company": company,
                        "location": location,
                        "country": country.upper(),
                        "url": href,
                        "description": snippet,
                        "source": "Gmail-LinkedIn"
                    })
        return jobs

    def _parse_indeed_alert(self, content, subject, country):
        parser = EmailJobParser()
        parser.feed(content)
        
        jobs = []
        for idx, el in enumerate(parser.elements):
            if el[0] == 'link':
                href = el[1]
                title = el[2]
                
                # Check for Indeed job link
                if "indeed.com/rc/clk" in href or "indeed.com/viewjob" in href or "indeed.com/pagead" in href:
                    if not title or len(title) < 3 or "view job" in title.lower() or "apply" in title.lower():
                        continue
                        
                    # Find surrounding text blocks for company and location
                    subseq = []
                    for offset in range(1, 5):
                        if idx + offset < len(parser.elements):
                            neighbor = parser.elements[idx + offset]
                            if neighbor[0] == 'text':
                                subseq.append(neighbor[1])
                            elif neighbor[0] == 'link' and ("indeed.com/cmp" in neighbor[1]):
                                subseq.append(neighbor[2])
                    
                    company = "Unknown"
                    location = "Remote"
                    
                    # Indeed layout parsing
                    if subseq:
                        if len(subseq[0]) < 100:
                            company = subseq[0]
                        if len(subseq) > 1 and len(subseq[1]) < 100:
                            location = subseq[1]
                            
                    company = company.strip()
                    location = location.strip()
                    
                    # Extract job id from link if possible
                    jk_match = re.search(r'[?&]jk=([a-zA-Z0-9]+)', href)
                    job_id = jk_match.group(1) if jk_match else hashlib.md5(href.encode()).hexdigest()[:12]
                    job_key = f"gmail-indeed-{job_id}"
                    
                    snippet = " ".join([s for s in subseq if len(s) > 15][:2])
                    if not snippet:
                        snippet = f"Indeed Job Alert listing: {title} at {company}."
                        
                    jobs.append({
                        "job_key": job_key,
                        "title": title,
                        "company": company,
                        "location": location,
                        "country": country.upper(),
                        "url": href,
                        "description": snippet,
                        "source": "Gmail-Indeed"
                    })
        return jobs

    def _parse_direct_email(self, sender, subject, content, country):
        # Fallback parser for single direct email listings (e.g. from recruiter)
        # Skip if subject doesn't look like a job or from standard mailing systems
        subject_lower = subject.lower()
        if "newsletter" in subject_lower or "unsubscribed" in subject_lower:
            return None
            
        # Extract company name from sender domain
        domain_match = re.search(r'@([a-zA-Z0-9.-]+)', sender)
        domain = domain_match.group(1) if domain_match else "Recruiter"
        company_name = domain.split('.')[0].title() if '.' in domain else domain
        
        # Exclude common consumer domains
        if company_name.lower() in ["gmail", "yahoo", "outlook", "hotmail", "protonmail", "icloud"]:
            company_name = "Independent Recruiter"
            
        # Title is the subject (clean up prefixes like Re:, Fwd:)
        title = re.sub(r'^(re|fwd|fwd\s*:|re\s*:)\s*', '', subject, flags=re.IGNORECASE).strip()
        if not title:
            title = "Job Opportunity"
            
        # Check if subject is relevant to software engineer
        # (This acts as a basic query filter so we don't sync spam)
        keywords = ["engineer", "developer", "programmer", "manager", "product", "recruiting", "job", "position", "role"]
        if not any(kw in subject_lower for kw in keywords):
            return None
            
        # Clean description body
        desc_clean = re.sub(r'<[^>]+>', ' ', content)
        desc_clean = html.unescape(desc_clean).strip()
        # Compress whitespaces
        desc_clean = re.sub(r'\s+', ' ', desc_clean)
        
        # Create a unique key using subject and sender hash
        hash_str = f"{sender}-{subject}"
        job_key = f"gmail-direct-{hashlib.md5(hash_str.encode()).hexdigest()[:12]}"
        
        # Link is a mailto: to easily reply to the recruiter!
        url = f"mailto:{sender}"
        
        return {
            "job_key": job_key,
            "title": title,
            "company": company_name,
            "location": "Remote",
            "country": country.upper(),
            "url": url,
            "description": desc_clean[:2000], # Cap size
            "source": "Gmail-Direct"
        }
