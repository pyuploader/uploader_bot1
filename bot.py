# bot.py
import os
import asyncio
import logging
import aiohttp
import aiofiles
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from telegram import InputFile
from telegram.ext import ApplicationBuilder, CommandHandler
from aiohttp import ClientSession

# CONFIG from environment
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")             # set in Render secrets
TARGET_CHAT = os.environ.get("TARGET_CHAT_ID")           # numeric id like -1001234567890 or @channelname
SOURCE_URL = os.environ.get("SOURCE_URL", "http://13.126.104.168")  # base URL to crawl
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3600"))  # seconds between checks (default 1 hour)
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "downloads"))
SENT_DB = Path(os.environ.get("SENT_DB", "sent_files.json"))

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uploader-bot")

# Ensure folders exist
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
if not SENT_DB.exists():
    SENT_DB.write_text(json.dumps({"sent": []}))

# Helpers
async def fetch_html(session: ClientSession, url: str, timeout=30):
    async with session.get(url, timeout=timeout) as resp:
        if resp.status == 200:
            return await resp.text()
        else:
            logger.warning(f"Failed to fetch {url} (status {resp.status})")
            return ""

def file_hash_name(url: str):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()

def load_sent():
    try:
        return set(json.loads(SENT_DB.read_text())["sent"])
    except Exception:
        return set()

def save_sent(sent_set):
    SENT_DB.write_text(json.dumps({"sent": list(sent_set)}))

async def download_file(session: ClientSession, url: str, dest: Path):
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                tmp = dest.with_suffix(dest.suffix + ".part")
                f = await aiofiles.open(tmp, "wb")
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    await f.write(chunk)
                await f.close()
                tmp.rename(dest)
                return dest
            else:
                logger.warning(f"Download failed {url} status {resp.status}")
                return None
    except Exception as e:
        logger.exception(f"Error downloading {url}: {e}")
        return None

async def find_mp4_links(session: ClientSession, base_url: str):
    parsed_base = urlparse(base_url)
    host = parsed_base.netloc
    seen_pages = set()
    to_visit = [base_url]
    found_files = set()

    while to_visit:
        url = to_visit.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        logger.info(f"Crawling: {url}")
        html = await fetch_html(session, url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")

        # find links to mp4 files
        for tag in soup.find_all(["a", "source", "video"]):
            href = tag.get("href") or tag.get("src")
            if not href:
                continue
            abs_link = urljoin(url, href)
            if abs_link.lower().endswith(".mp4"):
                found_files.add(abs_link)

        # find internal links to follow
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"])
            p = urlparse(link)
            if p.netloc == host:
                if link not in seen_pages and link not in to_visit:
                    to_visit.append(link)

    return sorted(found_files)

# Telegram sending
async def send_file_to_telegram(app, file_path: Path, caption: str = ""):
    bot = app.bot
    try:
        logger.info(f"Sending {file_path.name} to {TARGET_CHAT}")
        with open(file_path, "rb") as f:
            await bot.send_video(chat_id=TARGET_CHAT, video=InputFile(f, filename=file_path.name), caption=caption)
        logger.info(f"Sent {file_path.name}")
        return True
    except Exception:
        logger.exception("Failed sending file to Telegram")
        return False

# Main workflow
async def crawl_and_upload(app):
    sent = load_sent()
    async with aiohttp.ClientSession() as session:
        mp4_links = await find_mp4_links(session, SOURCE_URL)
        logger.info(f"Found {len(mp4_links)} mp4 links")
        for link in mp4_links:
            link_id = file_hash_name(link)
            if link_id in sent:
                logger.info(f"Already sent: {link}")
                continue
            parsed = urlparse(link)
            filename = Path(parsed.path).name or f"{link_id}.mp4"
            dest = DOWNLOAD_DIR / filename
            if not dest.exists():
                logger.info(f"Downloading {link} to {dest}")
                res = await download_file(session, link, dest)
                if not res:
                    logger.warning(f"Skipping {link} due to download failure")
                    continue
            ok = await send_file_to_telegram(app, dest)
            if ok:
                sent.add(link_id)
                save_sent(sent)
            else:
                logger.warning(f"Failed to upload {link}; will retry next run")

async def periodic_worker(app):
    while True:
        try:
            await crawl_and_upload(app)
        except Exception:
            logger.exception("Error in crawl_and_upload")
        logger.info(f"Sleeping for {POLL_INTERVAL} seconds")
        await asyncio.sleep(POLL_INTERVAL)

# command to check bot status
async def status_cmd(update, context):
    await update.message.reply_text("Uploader bot is running. Source: {}\nTarget: {}".format(SOURCE_URL, TARGET_CHAT))

def main():
    if not TG_BOT_TOKEN or not TARGET_CHAT:
        logger.error("TG_BOT_TOKEN and TARGET_CHAT must be set in environment")
        return
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", status_cmd))
    async def post_init(a):
        a.create_task(periodic_worker(a))
    app.post_init = post_init
    logger.info("Starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()