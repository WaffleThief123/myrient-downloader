import os
import re
import time
import zipfile
import requests
import requests.adapters
import sqlite3
import argparse
import sys
import threading
from urllib.parse import urljoin, unquote
from collections import deque
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
DEFAULT_USER_AGENT = "downloaded using https://github.com/WaffleThief123/myrient-downloader by a user who did not bother to modify the user agent"

CHUNK_SIZE = 1024 * 1024  # 1MB chunks for download streaming
MAX_RETRIES = 3

REGION_ALIASES = {
    'EU': 'Europe', 'JP': 'Japan', 'JPN': 'Japan',
    'AUS': 'Australia', 'KR': 'Korea', 'BR': 'Brazil',
    'CN': 'China', 'FR': 'France', 'DE': 'Germany',
    'HK': 'Hong Kong', 'IT': 'Italy', 'NL': 'Netherlands',
    'ES': 'Spain', 'SE': 'Sweden', 'CA': 'Canada',
}


# --- Database Manager ---

class DatabaseManager:
    """Thread-safe SQLite database manager using a single shared connection."""

    def __init__(self, db_file):
        self.db_file = db_file
        self._lock = threading.Lock()
        self._conn = None

    def initialize(self):
        """Create/open the database and ensure schema is up to date."""
        self._conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        cursor = self._conn.cursor()

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

        self._conn.commit()

    def file_exists(self, url, download_dir):
        """Check if a file URL already exists in the database and the file exists on disk."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT filename FROM downloads WHERE url = ?", (url,))
            result = cursor.fetchone()

        if result is None:
            return False

        filename = result[0]
        # ZIP files get extracted and deleted - trust the DB record
        if filename.lower().endswith(".zip"):
            return True

        local_path = os.path.join(download_dir, filename)
        return os.path.exists(local_path)

    def save_file(self, url, filename, download_dir):
        """Save the download record to the database."""
        local_path = os.path.join(download_dir, filename)
        full_path = os.path.abspath(local_path)
        file_size = os.path.getsize(local_path) if os.path.exists(local_path) else None

        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO downloads
                   (url, filename, full_path, download_date, file_size, status)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?)""",
                (url, filename, full_path, file_size, 'completed')
            )
            self._conn.commit()

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None


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
    parser.add_argument(
        "-r",
        "--region",
        nargs="*",
        default=None,
        help="Filter downloads to specific regions (e.g. -r USA EU JP). Aliases like EU=Europe, JP=Japan are supported. Env var: REGION (comma-separated)"
    )

    args = parser.parse_args()

    # Resolve region filter: CLI takes priority, then env var
    if args.region is not None:
        raw_regions = args.region
    else:
        env_region = os.getenv('REGION', '')
        raw_regions = [r.strip() for r in env_region.split(',') if r.strip()] if env_region else None

    if raw_regions:
        args.region = [REGION_ALIASES.get(r.upper(), r) for r in raw_regions]
    else:
        args.region = None

    # Update config with CLI overrides
    config['base_url'] = args.url
    config['download_dir'] = args.download_dir
    config['max_threads'] = args.threads
    config['timeout'] = args.timeout
    config['db_file'] = args.db_file
    config['user_agent'] = args.user_agent

    return args, config

def get_links(base_url, timeout, session):
    """Collect all file links under base_url using iterative BFS."""
    links = []
    queue = deque([base_url])
    visited = set()

    while queue:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        print(f"[INFO] Scanning directory: {url}")
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
        except Exception as e:
            print(f"[ERROR] Failed to list {url}: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

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
                if full_url not in visited:
                    queue.append(full_url)
            else:
                links.append(full_url)

    return links

def clean_filename(url, base_url):
    """Return a decoded, filesystem-safe filename path relative to base_url."""
    rel_path = url.replace(base_url, "", 1)
    rel_path = unquote(rel_path)
    rel_path = rel_path.strip("/")
    return rel_path

def matches_region(filename, regions):
    """Check if a filename's region tag matches any of the specified regions.

    Extracts the first parenthesized group (the region tag in No-Intro/Redump naming)
    and checks if any specified region appears in it (case-insensitive).
    """
    match = re.search(r'\(([^)]+)\)', filename)
    if not match:
        return False
    region_tag = match.group(1).lower()
    return any(r.lower() in region_tag for r in regions)

def download_file(url, config, db, session):
    """Download a single file if not already present in the database and on disk."""
    base_url = config['base_url']
    download_dir = config['download_dir']
    timeout = config['timeout']

    # Check if already downloaded
    if db.file_exists(url, download_dir):
        rel_path = clean_filename(url, base_url)
        print(f"[SKIP] {rel_path} already downloaded.")
        return rel_path

    rel_path = clean_filename(url, base_url)
    local_path = os.path.join(download_dir, rel_path)
    dir_path = os.path.dirname(local_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    for attempt in range(MAX_RETRIES):
        try:
            with session.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
            print(f"[OK]   {rel_path}")

            # Save to database after successful download
            db.save_file(url, rel_path, download_dir)
            return rel_path
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                print(f"[RETRY] {rel_path} - attempt {attempt + 1}/{MAX_RETRIES} failed: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[FAIL] {rel_path} - {e}")
                # Clean up partial download if it exists
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except OSError:
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

    # Validate required config
    if not config['base_url']:
        print("[ERROR] No base URL specified. Use -u/--url or set BASE_URL in .env")
        sys.exit(1)
    if not config['download_dir']:
        print("[ERROR] No download directory specified. Use -d/--download-dir or set DOWNLOAD_DIR in .env")
        sys.exit(1)

    # Ensure base_url ends with / for consistent URL handling
    if not config['base_url'].endswith('/'):
        config['base_url'] += '/'

    db = DatabaseManager(config['db_file'])
    db.initialize()

    # Create a shared HTTP session for connection pooling
    session = requests.Session()
    session.headers.update({'User-Agent': config['user_agent']})

    # Size the connection pool to match thread count
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=config['max_threads'],
        pool_maxsize=config['max_threads'],
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    try:
        print(f"[INFO] Fetching file list from {config['base_url']} ...")
        files = get_links(config['base_url'], config['timeout'], session)
        print(f"[INFO] Found {len(files)} files.")

        # Apply region filter if specified
        if args.region:
            total = len(files)
            files = [f for f in files if matches_region(unquote(f.split('/')[-1]), args.region)]
            print(f"[INFO] Region filter {args.region}: {len(files)}/{total} files matched.")

        # If -c, show the count and exit
        if args.count:
            print(len(files))
            sys.exit(0)

        # Normal download mode
        total_files = len(files)
        completed = 0
        print(f"[INFO] Starting download of {total_files} files with {config['max_threads']} threads...")
        with ThreadPoolExecutor(max_workers=config['max_threads']) as executor:
            futures = [executor.submit(download_file, url, config, db, session) for url in files]
            for future in as_completed(futures):
                completed += 1
                rel_path = future.result()
                if completed % 50 == 0 or completed == total_files:
                    print(f"[INFO] Progress: {completed}/{total_files} files processed.")
                if rel_path and rel_path.lower().endswith(".zip"):
                    zip_path = os.path.join(config['download_dir'], rel_path)
                    if os.path.exists(zip_path):
                        unzip_file(zip_path)

        print("[DONE] All downloads completed.")
    finally:
        session.close()
        db.close()

if __name__ == "__main__":
    main()
