import os
import zipfile
import requests
import sqlite3
import argparse
import sys
from urllib.parse import urljoin, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

# Try to import python-dotenv, but make it optional
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env file support is optional

# --- Default Configuration ---
DEFAULT_BASE_URL = ""
DEFAULT_DOWNLOAD_DIR = ""
DEFAULT_MAX_THREADS = 8
DEFAULT_TIMEOUT = 20
DEFAULT_DB_FILE = "downloads.db"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

# --- Helper Functions ---

def load_config():
    """Load configuration from environment variables with defaults."""
    config = {
        'base_url': os.getenv('BASE_URL', DEFAULT_BASE_URL),
        'download_dir': os.getenv('DOWNLOAD_DIR', DEFAULT_DOWNLOAD_DIR),
        'max_threads': int(os.getenv('MAX_THREADS', DEFAULT_MAX_THREADS)),
        'timeout': int(os.getenv('TIMEOUT', DEFAULT_TIMEOUT)),
        'db_file': os.getenv('DB_FILE', DEFAULT_DB_FILE),
        'user_agent': os.getenv('USER_AGENT', DEFAULT_USER_AGENT),
    }
    return config

def parse_args(config):
    """Parse command-line arguments and override config values."""
    parser = argparse.ArgumentParser(
        description='Download ROM files from Myrient archives with SQLite tracking.'
    )
    parser.add_argument(
        "-c",
        "--count",
        action="store_true",
        help="Print total count of files to download and exit."
    )
    parser.add_argument(
        "-u",
        "--url",
        type=str,
        default=config['base_url'],
        help="Base URL to download from (default: from .env or script default)"
    )
    parser.add_argument(
        "-d",
        "--download-dir",
        type=str,
        default=config['download_dir'],
        help="Directory to save downloaded files (default: from .env or ./Nintendo_GameBoyAdvance)"
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=config['max_threads'],
        help="Number of concurrent download threads (default: from .env or 8)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=config['timeout'],
        help="Request timeout in seconds (default: from .env or 20)"
    )
    parser.add_argument(
        "--db-file",
        type=str,
        default=config['db_file'],
        help="SQLite database file path (default: from .env or downloads.db)"
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default=config['user_agent'],
        help="User agent string for HTTP requests (default: from .env or default browser UA)"
    )
    
    args = parser.parse_args()
    
    # Update config with CLI overrides
    config['base_url'] = args.url
    config['download_dir'] = args.download_dir
    config['max_threads'] = args.threads
    config['timeout'] = args.timeout
    config['db_file'] = args.db_file
    config['user_agent'] = args.user_agent
    
    return args, config

def initialize_db(db_file):
    """Initialize the SQLite database and create the 'downloads' table if it doesn't exist."""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS downloads (
        url TEXT PRIMARY KEY,
        filename TEXT,
        full_path TEXT,
        download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        file_size INTEGER,
        status TEXT DEFAULT 'completed'
    )
    ''')

    # Migrate existing databases: add full_path column if it doesn't exist
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN full_path TEXT")
    except sqlite3.OperationalError:
        # Column already exists, which is fine
        pass

    conn.commit()
    conn.close()

def file_exists_in_db(url, db_file, download_dir):
    """Check if a file URL already exists in the database and the file exists on disk."""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    cursor.execute("SELECT filename FROM downloads WHERE url = ?", (url,))
    result = cursor.fetchone()
    conn.close()
    
    if result is None:
        return False
    
    # Also verify the file actually exists on disk
    filename = result[0]
    local_path = os.path.join(download_dir, filename)
    return os.path.exists(local_path)

def save_file_to_db(url, filename, db_file, download_dir):
    """Save the URL, filename, full path, and timestamp of the successfully downloaded file into the database."""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    local_path = os.path.join(download_dir, filename)
    full_path = os.path.abspath(local_path)  # Store absolute path
    file_size = os.path.getsize(local_path) if os.path.exists(local_path) else None

    # Use INSERT OR REPLACE with explicit timestamp to update download_date on re-downloads
    cursor.execute(
        """INSERT OR REPLACE INTO downloads 
           (url, filename, full_path, download_date, file_size, status) 
           VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?)""",
        (url, filename, full_path, file_size, 'completed')
    )

    conn.commit()
    conn.close()

def get_links(url, base_url, timeout, user_agent):
    """Recursively collect all files under base_url."""
    print(f"[INFO] Scanning directory: {url}")
    try:
        headers = {'User-Agent': user_agent}
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Failed to list {url}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]

        # Skip parent dirs, query links, and index pages
        if href in ("../", "./", "/", "index.html", "index.htm"):
            continue
        if "?" in href:
            continue

        full_url = urljoin(url, href)

        # Only recurse within the base path
        if not full_url.startswith(base_url):
            continue

        if href.endswith("/"):
            links.extend(get_links(full_url, base_url, timeout, user_agent))
        else:
            links.append(full_url)

    return links

def clean_filename(url, base_url):
    """Return a decoded, filesystem-safe filename path relative to base_url."""
    rel_path = url.replace(base_url, "")
    rel_path = unquote(rel_path)
    rel_path = rel_path.strip("/")
    return rel_path

def download_file(url, config):
    """Download a single file if not already present in the database and on disk."""
    base_url = config['base_url']
    download_dir = config['download_dir']
    db_file = config['db_file']
    timeout = config['timeout']
    user_agent = config['user_agent']
    
    # Check if already downloaded
    if file_exists_in_db(url, db_file, download_dir):
        rel_path = clean_filename(url, base_url)
        print(f"[SKIP] {rel_path} already downloaded.")
        return rel_path

    rel_path = clean_filename(url, base_url)
    local_path = os.path.join(download_dir, rel_path)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    try:
        headers = {'User-Agent': user_agent}
        with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        print(f"[OK]   {rel_path}")

        # Save to database after successful download
        save_file_to_db(url, rel_path, db_file, download_dir)
        return rel_path
    except Exception as e:
        print(f"[FAIL] {rel_path} - {e}")
        # Clean up partial download if it exists
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except:
                pass
        return None

def unzip_file(zip_path):
    """Unzip the file and delete the ZIP afterward."""
    try:
        if zipfile.is_zipfile(zip_path):
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(os.path.dirname(zip_path))
            os.remove(zip_path)
            print(f"[INFO] Extracted and deleted {zip_path}")
        else:
            print(f"[WARN] {zip_path} is not a valid zip file.")
    except Exception as e:
        print(f"[ERROR] Failed to unzip {zip_path}: {e}")

def main():
    # Load config from .env file
    config = load_config()
    
    # Parse CLI arguments (will override .env values)
    args, config = parse_args(config)

    initialize_db(config['db_file'])

    print(f"[INFO] Fetching file list from {config['base_url']} ...")
    files = get_links(config['base_url'], config['base_url'], config['timeout'], config['user_agent'])
    print(f"[INFO] Found {len(files)} files.")

    # If -c, show the count and exit
    if args.count:
        print(len(files))
        sys.exit(0)

    # Normal download mode
    with ThreadPoolExecutor(max_workers=config['max_threads']) as executor:
        futures = [executor.submit(download_file, url, config) for url in files]
        for future in as_completed(futures):
            rel_path = future.result()
            if rel_path and rel_path.endswith(".zip"):
                zip_path = os.path.join(config['download_dir'], rel_path)
                unzip_file(zip_path)

    print("[DONE] All downloads completed.")

if __name__ == "__main__":
    main()