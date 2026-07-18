import uvicorn
import os
import sys

# Ensure backend directory is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from backend.database import Database
from backend.config import load_config

def main():
    print("==================================================")
    print("   Job Search Automation Dashboard Booting up...  ")
    print("==================================================")
    
    # 1. Initialize DB and create schema if missing
    db = Database()
    db.close()
    print("✓ Database initialized.")
    
    # 2. Check for resumes
    config = load_config()
    resumes_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), config.get("resumes_dir", "../resumes")))
    if not os.path.exists(resumes_dir) or not os.listdir(resumes_dir):
        print(f"⚠ WARNING: Resumes directory is empty or not found at: {resumes_dir}")
        print("  Please place your .md resume files inside it so the engine can parse them.")
    else:
        print(f"✓ Found resume directory: {resumes_dir} ({len(os.listdir(resumes_dir))} files)")
        
    print("\nStarting web server on http://localhost:8000...")
    print("Press Ctrl+C to stop.")
    
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)

if __name__ == "__main__":
    main()
