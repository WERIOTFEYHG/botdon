"""
ربات دانلودر تلگرام - نسخه نهایی
پشتیبانی از: اینستاگرام، توییتر/X، یوتیوب، پینترست، تیک‌تاک، ردیت
با قابلیت دانلود یوتیوب با بالاترین کیفیت (4K/8K)
"""
import os
import re
import json
import time
import uuid
import signal
import shutil
import base64
import secrets
import sqlite3
import asyncio
import logging
import subprocess
from html import escape as html_escape
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests
import yt_dlp
from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# ============================== لاگر ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024

# ============================== گلوبال ==============================
ADMIN_IDS: set[int] = {7714450221}  # ← آیدی عددی خودت رو اینجا بذار
admin_states: Dict[int, str] = {}
pending_youtube: Dict[str, dict] = {}
DB_PATH = "/data/bot_data.db"


# ============================== Config ==============================
@dataclass(frozen=True)
class Config:
    bot_token: str
    max_file_size_mb: int
    download_dir: str
    max_concurrent_downloads: int
    rate_limit_seconds: float
    admin_ids: set[int] = field(default_factory=set)
    db_path: str = "bot_data.db"
    hikerapi_key: Optional[str] = None
    cobalt_instance: str = "https://api.cobalt.tools"


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("❌ BOT_TOKEN تنظیم نشده!")

    db_path = os.getenv("DB_PATH", "/data/bot_data.db")
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    admin_ids = ADMIN_IDS.copy()
    env_ids = os.getenv("ADMIN_IDS", "")
    for part in env_ids.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            admin_ids.add(int(part))

    logger.info(f"👥 ادمین‌ها: {admin_ids}")

    return Config(
        bot_token=bot_token,
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "200")),
        download_dir=os.getenv("DOWNLOAD_DIR", "/tmp/downloads"),
        max_concurrent_downloads=int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")),
        rate_limit_seconds=float(os.getenv("RATE_LIMIT_SECONDS", "2")),
        admin_ids=admin_ids,
        db_path=db_path,
        hikerapi_key=os.getenv("HIKERAPI_KEY", "").strip() or None,
        cobalt_instance=os.getenv("COBALT_INSTANCE", "https://api.cobalt.tools"),
    )


# ============================== دیتابیس ==============================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def db_init():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_seen TEXT, last_active TEXT);
            CREATE TABLE IF NOT EXISTS downloads (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, platform TEXT, url TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS force_sub_channels (chat_id TEXT PRIMARY KEY, title TEXT, invite_link TEXT, added_at TEXT);
            CREATE TABLE IF NOT EXISTS channel_verifications (user_id INTEGER, chat_id TEXT, verified_at TEXT, PRIMARY KEY(user_id, chat_id));
        """)
    logger.info("✅ دیتابیس آماده شد")

def _now(): return datetime.now(timezone.utc).isoformat()

def db_record_user(uid: int):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO users VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_active=?", 
                        (uid, _now(), _now(), _now()))
    except: pass

def db_record_download(uid: int, platform: str, url: str = ""):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO downloads (user_id, platform, url, created_at) VALUES (?,?,?,?)", 
                        (uid, platform, url, _now()))
    except: pass

def db_get_stats():
    with get_db() as conn:
        return (conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0])

def db_add_channel(chat_id, title, link):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO force_sub_channels VALUES (?,?,?,?)", 
                    (chat_id, title, link, _now()))

def db_remove_channel(chat_id):
    with get_db() as conn:
        conn.execute("DELETE FROM force_sub_channels WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM channel_verifications WHERE chat_id=?", (chat_id,))

def db_list_channels():
    with get_db() as conn:
        return conn.execute("SELECT chat_id, title, invite_link FROM force_sub_channels").fetchall()

def db_record_verification(uid, chat_id):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO channel_verifications VALUES (?,?,?)", 
                    (uid, chat_id, _now()))

def db_verified_count(chat_id):
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM channel_verifications WHERE chat_id=?", 
                           (chat_id,)).fetchone()[0]


# ============================== تشخیص پلتفرم ==============================
PLATFORM_PATTERNS = {
    "instagram": re.compile(r"(instagram\.com|instagr\.am)", re.I),
    "pinterest": re.compile(r"(pinterest\.[a-z.]+|pin\.it)", re.I),
    "tiktok": re.compile(r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)", re.I),
    "twitter": re.compile(r"(twitter\.com|x\.com)", re.I),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)", re.I),
    "reddit": re.compile(r"(reddit\.com|redd\.it)", re.I),
}

def extract_url(text): 
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None

def detect_platform(url):
    for p, pat in PLATFORM_PATTERNS.items():
        if pat.search(url): return p
    return None

def build_caption(text):
    if not text or not text.strip(): return "✅ دانلود شد"
    text = text.strip()
    if len(text) > 950: text = text[:950] + "…"
    return html_escape(text, quote=False)[:CAPTION_LIMIT]


# ============================== دانلودر ==============================
class DownloadError(Exception): 
    pass

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
}
MediaItem = tuple[str, str]  # (path, type)

class Downloader:
    def __init__(self, download_dir: str, max_file_size_mb: int, cobalt_instance: str = "https://api.cobalt.tools"):
        self.download_dir = download_dir
        self.max_size = max_file_size_mb * 1024 * 1024
        self.hikerapi_key = os.getenv("HIKERAPI_KEY", "").strip() or None
        self.cobalt_instance = cobalt_instance.rstrip("/")
        os.makedirs(download_dir, exist_ok=True)

    def _find_file(self, fid: str) -> Optional[str]:
        if not os.path.exists(self.download_dir):
            return None
        for f in os.listdir(self.download_dir):
            if f.startswith(fid):
                return os.path.join(self.download_dir, f)
        return None

    @staticmethod
    def _friendly_error(msg: str) -> str:
        msg = msg.lower()
        if "private" in msg: return "🔒 پست خصوصیه"
        if "not available" in msg: return "🚫 محتوا در دسترس نیست"
        if "sign in" in msg or "login" in msg: return "🔑 نیاز به لاگین داره"
        if "too large" in msg: return "📦 حجم فایل زیاده"
        return "❌ نتونستم دانلود کنم"

    def _save(self, resp, path: str) -> bool:
        try:
            total = 0
            with open(path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    total += len(chunk)
                    if total > self.max_size:
                        f.close()
                        os.remove(path)
                        return False
                    f.write(chunk)
            if total == 0:
                os.remove(path)
                return False
            return True
        except:
            if os.path.exists(path): 
                os.remove(path)
            return False

    # ==================== یوتیوب ====================
    
    def _youtube_cobalt(self, url: str, fid: str):
        """Cobalt API - بالاترین کیفیت"""
        try:
            api_url = f"{self.cobalt_instance}/api/json"
            payload = {
                "url": url,
                "vQuality": "max",
                "vCodec": "h264",
                "aFormat": "best",
                "filenamePattern": "basic",
            }
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            
            resp = requests.post(api_url, json=payload, headers=headers, timeout=30)
            if resp.status_code != 200:
                return [], None
            
            data = resp.json()
            if data.get("status") == "error":
                return [], None
            
            download_url = data.get("url")
            if not download_url:
                return [], None
            
            media_resp = requests.get(download_url, headers=_HEADERS, timeout=120, stream=True)
            content_type = media_resp.headers.get("Content-Type", "")
            ext = ".mp4" if "video" in content_type or "octet-stream" in content_type else ".jpg"
            path = os.path.join(self.download_dir, f"{fid}{ext}")
            
            if self._save(media_resp, path):
                size = os.path.getsize(path)
                logger.info(f"✅ Cobalt: {size//1024//1024}MB")
                mtype = "video" if ext == ".mp4" else "photo"
                return [(path, mtype)], None
        except Exception as e:
            logger.warning(f"Cobalt: {e}")
        return [], None

    def _youtube_ytdlp(self, url: str, fid: str):
        """yt-dlp - بالاترین کیفیت ممکن"""
        output = os.path.join(self.download_dir, f"{fid}.%(ext)s")
        ydl_opts = {
            "outtmpl": output,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": self.max_size,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "restrictfilenames": True,
            "format": "bestvideo*+bestaudio/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "web"],
                }
            },
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                
                base = os.path.splitext(filepath)[0]
                for ext in [".mp4", ".webm", ".mkv"]:
                    if os.path.exists(base + ext):
                        filepath = base + ext
                        break
                
                if not os.path.exists(filepath):
                    filepath = self._find_file(fid)
                
                if not filepath or not os.path.exists(filepath):
                    raise DownloadError("فایلی دانلود نشد")
                
                size = os.path.getsize(filepath)
                if size == 0:
                    os.remove(filepath)
                    raise DownloadError("فایل خالیه")
                if size > self.max_size:
                    os.remove(filepath)
                    raise DownloadError(f"حجم {size//1024//1024}MB از حد مجاز بیشتره")
                
                title = info.get("title", "").strip()
                logger.info(f"✅ yt-dlp: {size//1024//1024}MB")
                return [(filepath, "video")], title
        except Exception as e:
            logger.warning(f"yt-dlp: {e}")
        return [], None

    def _youtube_piped(self, url: str, fid: str):
        """Piped API"""
        try:
            video_id = None
            for pattern in [r"v=([a-zA-Z0-9_-]{11})", r"youtu\.be/([a-zA-Z0-9_-]{11})"]:
                match = re.search(pattern, url)
                if match:
                    video_id = match.group(1)
                    break
            
            if not video_id:
                return [], None
            
            for piped_api in ["https://pipedapi.kavin.rocks", "https://piped-api.garudalinux.org"]:
                try:
                    resp = requests.get(f"{piped_api}/streams/{video_id}", headers=_HEADERS, timeout=15)
                    if resp.status_code != 200:
                        continue
                    
                    data = resp.json()
                    video_streams = data.get("videoStreams", [])
                    if not video_streams:
                        continue
                    
                    # بالاترین کیفیت
                    best = max(video_streams, key=lambda x: int(x.get("quality", "0").replace("p", "") or "0"))
                    video_url = best.get("url")
                    if not video_url:
                        continue
                    
                    media_resp = requests.get(video_url, headers=_HEADERS, timeout=120, stream=True)
                    path = os.path.join(self.download_dir, f"{fid}.mp4")
                    
                    if self._save(media_resp, path):
                        size = os.path.getsize(path)
                        logger.info(f"✅ Piped: {size//1024//1024}MB")
                        return [(path, "video")], data.get("title", "").strip()
                except:
                    continue
        except Exception as e:
            logger.warning(f"Piped: {e}")
        return [], None

    def _download_youtube(self, url: str, fid: str):
        """دانلود یوتیوب با چند روش"""
        for method in [self._youtube_cobalt, self._youtube_ytdlp, self._youtube_piped]:
            try:
                items, caption = method(url, fid)
                if items:
                    return items, caption
            except:
                continue
        
        raise DownloadError("❌ نتونستم ویدیو رو دانلود کنم")

    # ==================== بقیه پلتفرم‌ها ====================

    def _try_hikerapi(self, url: str, fid: str):
        if not self.hikerapi_key:
            return [], None
        try:
            resp = requests.get(
                "https://api.hikerapi.com/v2/media/info/by/url",
                params={"url": url},
                headers={"x-access-key": self.hikerapi_key},
                timeout=20,
            )
            if resp.status_code != 200:
                return [], None
            data = resp.json()
        except:
            return [], None

        caption = (data.get("caption") or {}).get("text") if isinstance(data.get("caption"), dict) else None
        media = data.get("carousel_media") or [data]
        results = []
        for i, item in enumerate(media[:10]):
            if item.get("video_versions"):
                media_url = item["video_versions"][0].get("url")
                mtype, ext = "video", ".mp4"
            else:
                candidates = (item.get("image_versions2") or {}).get("candidates") or []
                media_url = candidates[0].get("url") if candidates else None
                mtype, ext = "photo", ".jpg"
            
            if not media_url:
                continue
            path = os.path.join(self.download_dir, f"{fid}_{i}{ext}")
            try:
                r = requests.get(media_url, headers=_HEADERS, timeout=30, stream=True)
                if self._save(r, path):
                    results.append((path, mtype))
            except:
                pass
        return results, caption

    def _try_ytdlp_generic(self, url: str, fid: str):
        output = os.path.join(self.download_dir, f"{fid}.%(ext)s")
        ydl_opts = {
            "outtmpl": output,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": self.max_size,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "restrictfilenames": True,
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                
                base = os.path.splitext(filepath)[0]
                for ext in [".mp4", ".webm", ".mkv"]:
                    if os.path.exists(base + ext):
                        filepath = base + ext
                        break
                
                if not os.path.exists(filepath):
                    filepath = self._find_file(fid)
                
                if not filepath or not os.path.exists(filepath):
                    raise DownloadError("فایلی دانلود نشد")
                
                size = os.path.getsize(filepath)
                if size == 0:
                    os.remove(filepath)
                    raise DownloadError("فایل خالیه")
                if size > self.max_size:
                    os.remove(filepath)
                    raise DownloadError(f"حجم {size//1024//1024}MB زیاده")
                
                ext = os.path.splitext(filepath)[1].lower()
                mtype = "video" if ext in (".mp4", ".mov", ".webm", ".mkv") else "photo"
                caption = info.get("title", "").strip() or None
                
                return [(filepath, mtype)], caption
        except:
            return [], None

    def _try_gallerydl(self, url: str, fid: str):
        cmd = ["gallery-dl", "-g", "--no-download"]
        cmd.append(url)
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            urls = [l.strip() for l in result.stdout.splitlines() if l.strip().startswith("http")][:10]
        except:
            return [], None
        
        results = []
        for i, u in enumerate(urls):
            ext = ".mp4" if any(u.lower().endswith(e) for e in (".mp4", ".mov", ".webm")) else ".jpg"
            path = os.path.join(self.download_dir, f"{fid}_{i}{ext}")
            try:
                r = requests.get(u, headers=_HEADERS, timeout=30, stream=True)
                if self._save(r, path):
                    mtype = "video" if ext == ".mp4" else "photo"
                    results.append((path, mtype))
            except:
                pass
        return results, None

    def _try_og(self, url: str, fid: str):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15, allow_redirects=True)
            img = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', r.text, re.I)
            if not img:
                return [], None
            
            img_url = img.group(1).replace("&amp;", "&")
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            
            resp = requests.get(img_url, headers=_HEADERS, timeout=20, stream=True)
            ct = resp.headers.get("Content-Type", "")
            ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
            path = os.path.join(self.download_dir, f"{fid}{ext}")
            
            if self._save(resp, path):
                return [(path, "photo")], None
        except:
            pass
        return [], None

    # ==================== متد اصلی ====================

    def _download_sync(self, url: str, platform: str):
        fid = str(uuid.uuid4())[:8]
        logger.info(f"📥 {platform} | {url[:80]}")

        # یوتیوب
        if platform == "youtube":
            return self._download_youtube(url, fid)

        # اینستاگرام با HikerAPI
        if platform == "instagram" and self.hikerapi_key:
            items, cap = self._try_hikerapi(url, fid)
            if items:
                return items, cap

        # yt-dlp
        items, cap = self._try_ytdlp_generic(url, fid)
        if items:
            return items, cap

        # gallery-dl
        items, cap = self._try_gallerydl(url, fid)
        if items:
            return items, cap

        # OG
        items, cap = self._try_og(url, fid)
        if items:
            return items, cap

        raise DownloadError("❌ هیچ روشی جواب نداد")

    async def download(self, url: str, platform: str):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, url, platform)

    # ==================== یوتیوب: کیفیت‌ها ====================

    def list_qualities(self, url: str):
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
        }
        
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except:
            return [{"height": 720, "approx_size_mb": None}], None

        formats = info.get("formats", [])
        heights = {}
        for f in formats:
            h = f.get("height")
            if not h or h == 0:
                continue
            
            if h not in heights:
                heights[h] = {"size": 0, "has_audio": False}
            
            size = f.get("filesize") or f.get("filesize_approx") or 0
            if size > heights[h]["size"]:
                heights[h]["size"] = size
            
            if f.get("acodec") and f["acodec"] != "none":
                heights[h]["has_audio"] = True

        results = []
        for h in sorted(heights.keys(), reverse=True)[:10]:
            info_h = heights[h]
            size_mb = round(info_h["size"] / 1024 / 1024) if info_h["size"] else None
            audio_icon = "🔊" if info_h["has_audio"] else "🔇"
            
            results.append({
                "height": h,
                "approx_size_mb": size_mb,
                "has_audio": info_h["has_audio"],
                "audio_icon": audio_icon,
            })

        return results, info.get("title")

    def download_quality(self, url: str, height: int):
        fid = str(uuid.uuid4())[:8]
        
        # اول cobalt
        items, caption = self._youtube_cobalt(url, fid)
        if items:
            return items, caption
        
        # بعد yt-dlp با کیفیت انتخابی
        output = os.path.join(self.download_dir, f"{fid}.%(ext)s")
        ydl_opts = {
            "outtmpl": output,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": self.max_size,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "restrictfilenames": True,
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            
            base = os.path.splitext(filepath)[0]
            for ext in [".mp4", ".webm", ".mkv"]:
                if os.path.exists(base + ext):
                    filepath = base + ext
                    break
            
            if not os.path.exists(filepath):
                filepath = self._find_file(fid)
            
            if not filepath or not os.path.exists(filepath):
                raise DownloadError("فایلی دانلود نشد")
            
            size = os.path.getsize(filepath)
            if size == 0:
                os.remove(filepath)
                raise DownloadError("فایل خالیه")
            if size > self.max_size:
                os.remove(filepath)
                raise DownloadError(f"حجم {size//1024//1024}MB از حد مجاز بیشتره! کیفیت پایین‌تر رو انتخاب کن")
            
            title = info.get("title", "").strip()
            logger.info(f"✅ کیفیت {height}p: {size//1024//1024}MB")
            return [(filepath, "video")], title

    async def download_yt_quality(self, url: str, height: int):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.download_quality, url, height)

    @staticmethod
    def cleanup(items):
        for path, _ in items:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except:
                pass


# ============================== محدودیت و میدلور ==============================
class DownloadLimiter:
    def __init__(self, n): 
        self._sem = asyncio.Semaphore(n)
    async def __aenter__(self): 
        await self._sem.acquire()
        return self
    async def __aexit__(self, *a): 
        self._sem.release()

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, sec): 
        self.sec = sec
        self._last = {}
    async def __call__(self, handler, event: Message, data):
        uid = event.from_user.id if event.from_user else None
        if uid and uid not in ADMIN_IDS:
            now = time.monotonic()
            if now - self._last.get(uid, 0) < self.sec:
                await event.answer(f"⏳ {self.sec - (now - self._last[uid]):.1f} ثانیه صبر کن")
                return
            self._last[uid] = now
        return await handler(event, data)


# ============================== عضویت اجباری ==============================
async def get_unjoined(bot, uid):
    unjoined = []
    for chat_id, title, link in db_list_channels():
        try:
            member = await bot.get_chat_member(chat_id, uid)
            if member.status in ("left", "kicked"):
                unjoined.append((chat_id, title, link))
        except:
            pass
    return unjoined

def membership_kb(unjoined):
    rows = []
    for cid, title, link in unjoined:
        url = link or (f"https://t.me/{cid.lstrip('@')}" if cid.startswith("@") else None)
        if url:
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ عضو شدم", callback_data="checksub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================== هندلرها ==============================
router = Router(name="main")

@router.message(CommandStart())
async def start(msg: Message):
    db_record_user(msg.from_user.id)
    admin_hint = "\n📊 /admin پنل مدیریت" if msg.from_user.id in ADMIN_IDS else ""
    await msg.answer(f"👋 سلام!\n\n🔗 لینک بفرست تا دانلود کنم{admin_hint}")

async def send_media(msg: Message, items, caption):
    cap = build_caption(caption)
    if len(items) == 1:
        path, mtype = items[0]
        if mtype == "photo":
            await msg.answer_photo(FSInputFile(path), caption=cap)
        else:
            try:
                await msg.answer_video(FSInputFile(path), caption=cap)
            except:
                await msg.answer_document(FSInputFile(path), caption=cap)
    else:
        media = []
        for i, (p, t) in enumerate(items):
            if t == "photo":
                media.append(InputMediaPhoto(media=FSInputFile(p), caption=cap if i == 0 else None))
            else:
                media.append(InputMediaVideo(media=FSInputFile(p), caption=cap if i == 0 else None))
        try:
            await msg.answer_media_group(media)
        except:
            for p, t in items:
                if t == "photo":
                    await msg.answer_photo(FSInputFile(p))
                else:
                    await msg.answer_video(FSInputFile(p))

@router.callback_query(F.data == "checksub")
async def checksub(cb: CallbackQuery):
    unjoined = await get_unjoined(cb.bot, cb.from_user.id)
    if unjoined:
        await cb.answer("⚠️ هنوز عضو نشدی!", show_alert=True)
        return
    for cid, _, _ in db_list_channels():
        db_record_verification(cb.from_user.id, cid)
    await cb.answer("✅ تایید شد!", show_alert=True)
    await cb.message.edit_text("✅ حالا لینک بفرست")

@router.callback_query(F.data.startswith("ytcancel:"))
async def ytcancel(cb: CallbackQuery):
    pending_youtube.pop(cb.data.split(":")[1], None)
    await cb.answer("لغو شد")
    await cb.message.edit_text("❌ لغو شد")

@router.callback_query(F.data.startswith("ytq:"))
async def ytquality(cb: CallbackQuery, downloader: Downloader, limiter: DownloadLimiter):
    try:
        _, sid, h = cb.data.split(":")
        height = int(h)
    except:
        await cb.answer("خطا", show_alert=True)
        return
    
    entry = pending_youtube.get(sid)
    if not entry:
        await cb.answer("⏰ منقضی شده", show_alert=True)
        await cb.message.edit_text("⏰ منقضی شد. دوباره لینک رو بفرست")
        return

    await cb.answer()
    await cb.message.edit_text(f"⏳ دانلود {height}p...")
    items = []
    try:
        async with limiter:
            items, cap = await downloader.download_yt_quality(entry["url"], height)
        await send_media(cb.message, items, cap or entry.get("title"))
        db_record_download(cb.from_user.id, "youtube", entry["url"])
        await cb.message.delete()
    except DownloadError as e:
        await cb.message.edit_text(f"❌ {e}")
    except Exception:
        logger.exception("خطای یوتیوب")
        await cb.message.edit_text("❌ خطا در دانلود")
    finally:
        Downloader.cleanup(items)
        pending_youtube.pop(sid, None)

@router.message(F.text)
async def handle_link(msg: Message, downloader: Downloader, limiter: DownloadLimiter, config: Config):
    uid = msg.from_user.id
    db_record_user(uid)
    text = msg.text or ""

    # ادمین - افزودن کانال
    if uid in ADMIN_IDS and admin_states.get(uid) == "awaiting_add_channel":
        admin_states.pop(uid)
        raw = text.strip()
        try:
            chat = await msg.bot.get_chat(raw)
            me = await msg.bot.me()
            member = await msg.bot.get_chat_member(chat.id, me.id)
            if member.status not in ("administrator", "creator"):
                await msg.answer("⚠️ ربات ادمین نیست!")
                return
            title = chat.title or raw
            link = f"https://t.me/{chat.username}" if chat.username else None
            db_add_channel(str(chat.id), title, link)
            await msg.answer(f"✅ {title} اضافه شد")
        except Exception as e:
            await msg.answer(f"❌ خطا: {e}")
        return

    # عضویت اجباری
    if uid not in ADMIN_IDS:
        unjoined = await get_unjoined(msg.bot, uid)
        if unjoined:
            await msg.answer("🔒 عضو کانال‌ها شو 👇", reply_markup=membership_kb(unjoined))
            return

    url = extract_url(text)
    if not url:
        await msg.answer("❌ لینک بفرست")
        return

    platform = detect_platform(url)
    if not platform:
        await msg.answer("❌ پلتفرم پشتیبانی نمیشه")
        return

    # یوتیوب
    if platform == "youtube":
        status = await msg.answer("⏳ بررسی کیفیت‌ها...")
        try:
            qualities, title = await asyncio.get_running_loop().run_in_executor(None, downloader.list_qualities, url)
        except:
            await status.edit_text("❌ خطا در دریافت اطلاعات ویدیو")
            return

        if not qualities:
            await status.edit_text("❌ کیفیتی پیدا نشد")
            return

        sid = secrets.token_hex(4)
        pending_youtube[sid] = {"url": url, "title": title, "created_at": time.time()}

        rows = []
        for q in qualities:
            size = f" (~{q['approx_size_mb']}MB)" if q["approx_size_mb"] else ""
            audio = q.get("audio_icon", "")
            rows.append([InlineKeyboardButton(
                text=f"{audio} {q['height']}p{size}",
                callback_data=f"ytq:{sid}:{q['height']}",
            )])
        rows.append([InlineKeyboardButton(text="❌ لغو", callback_data=f"ytcancel:{sid}")])

        header = f"🎥 {title}\n\n" if title else ""
        await status.edit_text(f"{header}کیفیت:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return

    # بقیه پلتفرم‌ها
    status = await msg.answer("⏳ دانلود...")
    items = []
    try:
        async with limiter:
            items, cap = await downloader.download(url, platform)
        await send_media(msg, items, cap)
        db_record_download(uid, platform, url)
        await status.delete()
    except DownloadError as e:
        await status.edit_text(f"❌ {e}")
    except Exception:
        logger.exception("خطای دانلود")
        await status.edit_text("❌ خطا در دانلود")
    finally:
        Downloader.cleanup(items)


# ============================== پنل ادمین ==============================
def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📋 کانال‌ها", callback_data="admin:list")],
        [InlineKeyboardButton(text="➕ افزودن", callback_data="admin:add")],
        [InlineKeyboardButton(text="➖ حذف", callback_data="admin:remove")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ بازگشت", callback_data="admin:back")]])

@router.message(Command("admin"))
async def admin_cmd(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer(f"⛔️ دسترسی نداری\nآیدی تو: {msg.from_user.id}")
        return
    admin_states.pop(msg.from_user.id, None)
    await msg.answer("🛠 پنل مدیریت", reply_markup=admin_kb())

@router.callback_query(F.data.startswith("admin:"))
async def admin_cb(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔️", show_alert=True)
        return

    act = cb.data.split(":", 1)[1]
    await cb.answer()

    if act == "stats":
        u, d = db_get_stats()
        chs = db_list_channels()
        text = f"📊 آمار:\n👤 کاربران: {u}\n⬇️ دانلودها: {d}\n\n📢 کانال‌ها: {len(chs)}"
        if chs:
            text += "\n" + "\n".join(f"• {t}" for _, t, _ in chs)
        await cb.message.edit_text(text, reply_markup=back_kb())

    elif act == "list":
        chs = db_list_channels()
        text = "📋 کانال‌ها:\n" + "\n".join(f"• {t} (`{c}`)" for c, t, _ in chs) if chs else "⚠️ کانالی نیست"
        await cb.message.edit_text(text, reply_markup=back_kb())

    elif act == "add":
        admin_states[cb.from_user.id] = "awaiting_add_channel"
        await cb.message.edit_text("📝 آیدی کانال رو بفرست", reply_markup=back_kb())

    elif act == "remove":
        chs = db_list_channels()
        if not chs:
            await cb.message.edit_text("⚠️ کانالی نیست", reply_markup=back_kb())
        else:
            rows = [[InlineKeyboardButton(text=f"🗑 {t}", callback_data=f"admin:rm:{c}")] for c, t, _ in chs]
            rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="admin:back")])
            await cb.message.edit_text("🗑 کدوم حذف بشه؟", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    elif act.startswith("rm:"):
        db_remove_channel(act.split(":")[1])
        await cb.message.edit_text("✅ حذف شد", reply_markup=back_kb())

    elif act == "back":
        admin_states.pop(cb.from_user.id, None)
        await cb.message.edit_text("🛠 پنل مدیریت", reply_markup=admin_kb())


# ============================== اجرا ==============================
async def main():
    global ADMIN_IDS, DB_PATH
    
    config = load_config()
    ADMIN_IDS = config.admin_ids
    DB_PATH = config.db_path
    
    os.makedirs(config.download_dir, exist_ok=True)
    shutil.rmtree(config.download_dir, ignore_errors=True)
    os.makedirs(config.download_dir, exist_ok=True)
    
    db_init()
    
    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    
    downloader = Downloader(config.download_dir, config.max_file_size_mb, config.cobalt_instance)
    limiter = DownloadLimiter(config.max_concurrent_downloads)
    
    dp["downloader"] = downloader
    dp["limiter"] = limiter
    dp["config"] = config
    dp.message.middleware(ThrottlingMiddleware(config.rate_limit_seconds))
    dp.include_router(router)
    
    await bot.delete_webhook(drop_pending_updates=True)
    
    me = await bot.get_me()
    logger.info(f"🤖 @{me.username} | ادمین‌ها: {ADMIN_IDS}")
    logger.info(f"📦 حداکثر حجم: {config.max_file_size_mb}MB")
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
