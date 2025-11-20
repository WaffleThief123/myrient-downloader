# Myrient ROM Downloader

A Python script for downloading ROM files from Myrient archives with SQLite-based download tracking to prevent re-downloading files.

## Features

- **Recursive directory scanning**: Automatically discovers all files in the target directory structure
- **SQLite download tracking**: Tracks downloaded files in a local database to prevent re-downloads
- **Concurrent downloads**: Multi-threaded downloads for faster performance (configurable)
- **Automatic ZIP extraction**: Automatically extracts ZIP files and removes the archive after extraction
- **Resume capability**: Skips files that have already been downloaded (checked against both database and filesystem)
- **Comprehensive tracking**: Records URL, filename, full path, download date/time, file size, and status

## Requirements

- Python 3.6+
- Required packages:
  - `requests`
  - `beautifulsoup4`
  - `python-dotenv` (optional, for .env file support)

Install dependencies:
```bash
pip install requests beautifulsoup4 python-dotenv
```

## Configuration

The script supports configuration via `.env` file and/or command-line flags. Command-line flags override `.env` values.

### Environment File (.env)

Create a `.env` file in the same directory as the script (copy from `env.example`):

```bash
cp env.example .env
```

Edit `.env` with your preferred settings:

```env
# Base URL to download from
BASE_URL=https://myrient.erista.me/files/No-Intro/Nintendo%20-%20Game%20Boy%20Advance/

# Directory to save downloaded files
DOWNLOAD_DIR=./Nintendo_GameBoyAdvance

# Number of concurrent download threads
MAX_THREADS=8

# Request timeout in seconds
TIMEOUT=20

# SQLite database file path
DB_FILE=downloads.db

# User agent string for HTTP requests
USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36
```

**Note**: If `python-dotenv` is not installed, the script will still work but won't load `.env` files. All configuration must then be done via command-line flags or will use defaults.

## Usage

### Basic Usage

Download all files from the configured URL (uses `.env` or defaults):
```bash
python myrient-dl.py
```

### Command-Line Options

All configuration options can be overridden via command-line flags:

```bash
# Download from a specific URL
python myrient-dl.py -u "https://myrient.erista.me/files/No-Intro/Nintendo%20-%20Game%20Boy/"

# Specify download directory
python myrient-dl.py -d "./MyROMs"

# Set number of concurrent threads
python myrient-dl.py -t 16

# Set timeout
python myrient-dl.py --timeout 30

# Specify database file
python myrient-dl.py --db-file "./my_downloads.db"

# Set custom user agent
python myrient-dl.py --user-agent "MyCustomAgent/1.0"

# Combine multiple options
python myrient-dl.py -u "https://example.com/roms/" -d "./ROMs" -t 4 --timeout 30
```

### Count Files

Get the total count of files available for download:
```bash
python myrient-dl.py -c
# or
python myrient-dl.py --count
```

### Full Help

View all available options:
```bash
python myrient-dl.py --help
```

### Configuration Priority

Configuration values are loaded in this order (later values override earlier ones):
1. Default values (hardcoded in script)
2. `.env` file values (if `python-dotenv` is installed)
3. Command-line flag values (highest priority)

## Database Schema

The script creates a SQLite database (`downloads.db`) with the following schema:

| Column | Type | Description |
|--------|------|-------------|
| `url` | TEXT (PRIMARY KEY) | The full URL of the downloaded file |
| `filename` | TEXT | Relative path/filename of the file |
| `full_path` | TEXT | Absolute path where the file is stored |
| `download_date` | TIMESTAMP | Date and time when the file was downloaded |
| `file_size` | INTEGER | Size of the file in bytes |
| `status` | TEXT | Download status (default: 'completed') |

## How It Works

1. **Initialization**: Creates/opens the SQLite database and initializes the schema
2. **Discovery**: Recursively scans the target URL to find all downloadable files
3. **Download Check**: For each file, checks if it exists in the database AND on disk
4. **Download**: Downloads files that haven't been downloaded yet using concurrent threads
5. **Tracking**: Saves download information to the database after successful download
6. **Extraction**: Automatically extracts ZIP files and removes the archive

## Features in Detail

### Download Tracking

The script uses a two-layer check to determine if a file should be downloaded:
- Checks the SQLite database for the URL
- Verifies the file actually exists on disk

This ensures that even if the database is cleared, files won't be re-downloaded if they still exist on disk (and vice versa).

### Error Handling

- Failed downloads are logged and partial files are cleaned up
- Network errors are caught and reported
- Invalid ZIP files are detected and reported

### File Organization

Files are saved maintaining the directory structure from the source URL, making it easy to navigate the downloaded files.

## Example Output

```
[INFO] Fetching file list from https://myrient.erista.me/files/No-Intro/Nintendo%20-%20Game%20Boy%20Advance/ ...
[INFO] Scanning directory: https://myrient.erista.me/files/No-Intro/Nintendo%20-%20Game%20Boy%20Advance/
[INFO] Found 1234 files.
[SKIP] subfolder/file1.zip already downloaded.
[OK]   subfolder/file2.zip
[INFO] Extracted and deleted ./Nintendo_GameBoyAdvance/subfolder/file2.zip
[OK]   subfolder/file3.zip
...
[DONE] All downloads completed.
```

## Notes

- The script automatically creates the download directory if it doesn't exist
- ZIP files are extracted to the same directory as the ZIP file
- The database file (`downloads.db`) is created in the same directory as the script
- The script respects the directory structure of the source URL

## License

This script is provided as-is for personal use.

