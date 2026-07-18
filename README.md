# CareerRadar: Job Search Automation

A modern, streamlined job search automation tool that pulls job alert emails directly from Gmail via IMAP, matches listings against markdown resume profiles, and compiles matching positions into daily reports and a web dashboard.

## Features
* **Gmail Ingestion**: Securely reads job alert emails (LinkedIn, Indeed) via IMAP.
* **Resume Matching**: Evaluates job descriptions against markdown resume profiles in `/resumes` using a scoring algorithm.
* **Daily Digests**: Automatically creates formatted Markdown summaries of daily job matches.
* **Web Dashboard**: Displays unread, saved, applied, and rejected positions in a beautiful web app.

## Installation & Setup
This project uses **[uv](https://github.com/astral-sh/uv)** for fast package and project management.

1. **Install uv** (if not already installed):
   ```bash
   curl -LsSf https://astral-sh.uv.cache.src.sh/install.sh | sh
   ```

2. **Sync dependencies**:
   ```bash
   uv sync
   ```

3. **Configure Environment**:
   Create a `.env` file in the project root:
   ```env
   GMAIL_EMAIL="your.email@gmail.com"
   GMAIL_APP_PASSWORD="your-16-char-app-password"
   ```

4. **Run the Dashboard**:
   ```bash
   uv run run.py
   ```
