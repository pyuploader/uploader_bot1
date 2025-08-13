# bot.py
# Multi-site crawler & Telegram uploader for .mp4 and .pdf
# Works on Pydroid 3 (Android) and can be hosted on Render.
# --- HOW TO USE (quick) ---
# 1) Put your BOT_TOKEN and TARGET_CHAT_ID below (or set as environment vars).
# 2) Add your source URLs/IPs in START_URLS.
# 3) Run: python bot.py
#    -> It will crawl, download, and upload all files; then sleep and repeat.

import os
import time
import json
import hashlib
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from telegram import Bot, InputFile

# ---------------- CONFIG ----------------
BOT_TOKEN       = os.getenv("TG_BOT_TOKEN", "PASTE_YOUR_NEW_BOT_TOKEN_HERE")
TARGET_CHAT_ID  = os.getenv("TARGET_CHAT_ID", "PASTE_NUMERIC_CHAT_ID_HERE")   # e.g. -1001234567890  (NOT an invite link)

# Add all your IPs/domains here (with http:// or https://)
START_URLS = [
    "http://13.126.104.168",
    # "http://ANOTHER_IP_OR_DOMAIN",
    # "https://ONE_MORE_SITE",
]

# File types to fetch
WANTED_EXTS = [".mp4", ".pdf"]

# Crawl limits & behavior
MAX_PAGES_PER_SITE = 500        # safety cap to avoid infinite crawl
REQUEST_TIMEOUT    = 25         # seconds
USER_AGENT         = "Mozilla/5.0 (uploader-bot)"

# Downloads & state
DOWNLOAD_DIR = Path("downloads")
SENT_DB_PATH = Path("sent_files.json")   # stores hashes of file URLs that were already uploaded

# Schedule
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL", "3600"))  # 1 hour default
RUN_ONCE          = os.getenv("RUN_ONCE", "0") == "1"        # set RUN_ONCE=1 to do one pass and exit
# ----------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("uploader")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
if not SENT_DB_PATH.exists():
    SENT_DB_PATH.write_text(json.dumps({"sent": []}, ensure_ascii=False))

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def load_sent() -> set:
    try:
        data = json.loads(SENT_DB_PATH.read_text() or "{}")
        return set(data.get("sent", []))
    except Exception:
        return set()

def save_sent(sent: set):
    SENT_DB_PATH.write_text(json.dumps({"sent": list(sent)}, ensure_ascii=False))

def same_host(url: str, host: str) -> bool:
    try:
        return urlparse(url).netloc == host
    except Exception:
        return False

def normalize_link(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except Exception:
        return ""

def is_wanted_file(url: str) -> bool:
    lower = url.lower()
    return any(lower.endswith(ext) for ext in WANTED_EXTS)

def fetch_html(url: str) -> str:
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
            return r.text
    except Exception as e:
        log.warning(f"Fetch failed {url}: {e}")
    return ""

def crawl_site(start_url: str) -> list:
    """Breadth-first crawl limited to same host, gather .mp4/.pdf links."""
    host = urlparse(start_url).netloc
    to_visit = [start_url]
    seen_pages = set()
    found_files = []

    while to_visit and len(seen_pages) < MAX_PAGES_PER_SITE:
        url = to_visit.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)

        html = fetch_html(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")

        # Find file links in <a>, <source>, <video>
        tags = []
        tags.extend(soup.find_all("a", href=True))
        tags.extend(soup.find_all("source", src=True))
        tags.extend(soup.find_all("video", src=True))

        for tag in tags:
            href = tag.get("href") or tag.get("src")
            if not href:
                continue
            abs_url = normalize_link(url, href)
            if not abs_url:
                continue
            if is_wanted_file(abs_url):
                found_files.append(abs_url)

        # Follow internal links
        for a in soup.find_all("a", href=True):
            nxt = normalize_link(url, a["href"])
            if nxt and same_host(nxt, host) and nxt not in seen_pages and nxt not in to_visit:
                to_visit.append(nxt)

    # Deduplicate while keeping order
    dedup = []
    seen = set()
    for f in found_files:
        if f not in seen:
            dedup.append(f)
            seen.add(f)
    return dedup

def download_file(file_url: str, dest_dir: Path) -> Path | None:
    name = Path(urlparse(file_url).path).name or f"{sha1(file_url)}"
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        with session.get(file_url, stream=True, timeout=REQUEST_TIMEOUT) as r:
            if r.status_code != 200:
                log.warning(f"Download failed {file_url} ({r.status_code})")
                return None
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        f.write(chunk)
            tmp.rename(dest)
            return dest
    except Exception as e:
        log.warning(f"Download error {file_url}: {e}")
        return None

def send_to_telegram(bot: Bot, path: Path) -> bool:
    try:
        if path.suffix.lower() == ".mp4":
            with open(path, "rb") as f:
                bot.send_video(chat_id=TARGET_CHAT_ID, video=InputFile(f, filename=path.name))
        else:
            with open(path, "rb") as f:
                bot.send_document(chat_id=TARGET_CHAT_ID, document=InputFile(f, filename=path.name))
        return True
    except Exception as e:
        log.error(f"Telegram send failed for {path.name}: {e}")
        return False

def process_all():
    if not BOT_TOKEN or "PASTE_YOUR_NEW_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise SystemExit("ERROR: Set BOT_TOKEN (env TG_BOT_TOKEN or edit file).")
    if not TARGET_CHAT_ID or "PASTE_NUMERIC_CHAT_ID_HERE" in str(TARGET_CHAT_ID):
        raise SystemExit("ERROR: Set TARGET_CHAT_ID to your numeric group id.")

    bot = Bot(token=BOT_TOKEN)
    sent = load_sent()
    total_new = 0

    for start in START_URLS:
        log.info(f"Scanning: {start}")
        files = crawl_site(start)
        log.info(f"Found {len(files)} candidate files at {start}")

        for file_url in files:
            file_id = sha1(file_url)
            if file_id in sent:
                continue
            local = download_file(file_url, DOWNLOAD_DIR)
            if not local:
                continue
            ok = send_to_telegram(bot, local)
            if ok:
                sent.add(file_id)
                total_new += 1
                save_sent(sent)
                log.info(f"Uploaded: {local.name}")
            else:
                log.warning(f"Will retry later: {file_url}")

    log.info(f"Cycle complete. New uploads this cycle: {total_new}")

def main():
    while True:
        process_all()
        if RUN_ONCE:
            break
        log.info(f"Sleeping {POLL_INTERVAL_SEC}s...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
