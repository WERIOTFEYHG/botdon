"""
ربات دانلودر تلگرام - اینستاگرام / توییتر(X) / یوتیوب / پینترست / تیک‌تاک / ردیت
نسخه بهینه با دانلود واقعی یوتیوب و پست‌های تصویری
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
from pathlib import Path
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
ADMIN_IDS: set[int] = {7714450221}  # ← آیدی خودت
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
    youtube_request_ttl: int = 300


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("❌ BOT_TOKEN تنظیم نشده!")

    db_path = os.getenv("DB_PATH", "/data/bot_data.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # ترکیب ادمین‌ها
    admin_ids = ADMIN_IDS.copy()
    env_ids = os.getenv("ADMIN_IDS", "")
    for part in env_ids.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            admin_ids.add(int(part))

    logger.info(f"👥 ادمین‌ها: {admin_ids}")

    return Config(
        bot_token=bot_token,
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "50")),
        download_dir=os.getenv("DOWNLOAD_DIR", "/tmp/downloads"),
        max_concurrent_downloads=int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")),
        rate_limit_seconds=float(os.getenv("RATE_LIMIT_SECONDS", "2")),
        admin_ids=admin_ids,
        db_path=db_path,
        hikerapi_key=os.getenv("HIKERAPI_KEY", "").strip() or None,
        youtube_request_ttl=int(os.getenv("YOUTUBE_REQUEST_TTL", "300")),
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

def _now(): return datetime.now(timezone.utc).isoformat()

def db_record_user(uid: int):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO users VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_active=?", (uid, _now(), _now(), _now()))
    except: pass

def db_record_download(uid: int, platform: str, url: str = ""):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO downloads (user_id, platform, url, created_at) VALUES (?,?,?,?)", (uid, platform, url, _now()))
    except: pass

def db_get_stats():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]

def db_add_channel(chat_id, title, link):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO force_sub_channels VALUES (?,?,?,?)", (chat_id, title, link, _now()))

def db_remove_channel(chat_id):
    with get_db() as conn:
        conn.execute("DELETE FROM force_sub_channels WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM channel_verifications WHERE chat_id=?", (chat_id,))

def db_list_channels():
    with get_db() as conn:
        return conn.execute("SELECT chat_id, title, invite_link FROM force_sub_channels").fetchall()

def db_record_verification(uid, chat_id):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO channel_verifications VALUES (?,?,?)", (uid, chat_id, _now()))

def db_verified_count(chat_id):
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM channel_verifications WHERE chat_id=?", (chat_id,)).fetchone()[0]


# ============================== تشخیص پلتفرم ==============================
PLATFORM_PATTERNS = {
    "instagram": re.compile(r"(instagram\.com|instagr\.am)", re.I),
    "pinterest": re.compile(r"(pinterest\.[a-z.]+|pin\.it)", re.I),
    "tiktok": re.compile(r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)", re.I),
    "twitter": re.compile(r"(twitter\.com|x\.com)", re.I),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)", re.I),
    "reddit": re.compile(r"(reddit\.com|redd\.it)", re.I),
}

def extract_url(text): return (re.search(r"https?://\S+", text) or [None])[0]
def detect_platform(url):
    for p, pat in PLATFORM_PATTERNS.items():
        if pat.search(url): return p
    return None

def build_caption(text):
    if not text or not text.strip(): return "✅ دانلود شد"
    text = text.strip()[:950] + ("…" if len(text.strip()) > 950 else "")
    return html_escape(text, quote=False)[:CAPTION_LIMIT]


# ============================== دانلودر ==============================
class DownloadError(Exception): pass

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
MediaItem = tuple[str, str]  # (path, type)

class Downloader:
    def __init__(self, download_dir: str, max_file_size_mb: int):
        self.download_dir = download_dir
        self.max_size = max_file_size_mb * 1024 * 1024
        self.hikerapi_key = os.getenv("HIKERAPI_KEY", "").strip() or None
        os.makedirs(download_dir, exist_ok=True)

    def _find_file(self, fid: str) -> Optional[str]:
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
        return "❌ نتونستم دانلود کنم، لینک رو چک کن"

    def _save(self, resp, path: str) -> bool:
        total = 0
        try:
            with open(path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    total += len(chunk)
                    if total > self.max_size:
                        os.remove(path)
                        return False
                    f.write(chunk)
            return total > 0
        except:
            if os.path.exists(path): os.remove(path)
            return False

    # ========== HikerAPI برای اینستاگرام ==========
    def _try_hikerapi(self, url: str, fid: str):
        if not self.hikerapi_key: return [], None
        try:
            resp = requests.get("https://api.hikerapi.com/v2/media/info/by/url", params={"url": url}, headers={"x-access-key": self.hikerapi_key}, timeout=20)
            if resp.status_code != 200: return [], None
            data = resp.json()
        except: return [], None

        caption = None
        if isinstance(data.get("caption"), dict):
            caption = data["caption"].get("text")
        
        media = data.get("carousel_media") or [data]
        results = []
        for i, item in enumerate(media[:10]):
            if item.get("video_versions"):
                url_media = item["video_versions"][0].get("url")
                mtype, ext = "video", ".mp4"
            else:
                candidates = (item.get("image_versions2") or {}).get("candidates") or []
                url_media = candidates[0].get("url") if candidates else None
                mtype, ext = "photo", ".jpg"
            
            if not url_media: continue
            path = os.path.join(self.download_dir, f"{fid}_{i}{ext}")
            try:
                r = requests.get(url_media, headers=_HEADERS, timeout=30, stream=True)
                if self._save(r, path):
                    results.append((path, mtype))
            except: pass
        return results, caption

    # ========== yt-dlp (هسته اصلی) ==========
    def _ytdlp_opts(self, output_path: str, is_youtube: bool = False):
        opts = {
            "outtmpl": output_path,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": self.max_size,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "restrictfilenames": True,
            "merge_output_format": "mp4",
        }
        
        if is_youtube:
            # تنظیمات مخصوص یوتیوب
            opts.update({
                "format": "best[height<=1080][ext=mp4]/best[height<=1080]/best[ext=mp4]/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "ios", "web"],
                        "skip": ["hls", "dash"],
                    }
                },
            })
        else:
            opts["format"] = "bestvideo*+bestaudio/best"
        
        return opts

    def _try_ytdlp(self, url: str, platform: str, fid: str):
        is_youtube = platform == "youtube"
        output_template = os.path.join(self.download_dir, f"{fid}.%(ext)s")
        ydl_opts = self._ytdlp_opts(output_template, is_youtube)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)

                # پیدا کردن فایل نهایی
                for ext in [".mp4", ".webm", ".mkv", ".mp3"]:
                    base = os.path.splitext(filepath)[0]
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
                    raise DownloadError(f"حجم فایل {size//1024//1024}MB از حد مجاز بیشتره")

                # تشخیص نوع
                ext = os.path.splitext(filepath)[1].lower()
                mtype = "video" if ext in (".mp4", ".mov", ".webm", ".mkv") else "photo"

                # کپشن
                title = info.get("title", "").strip()
                desc = info.get("description", "").strip()
                caption = "\n\n".join(p for p in [title, desc] if p) or None

                logger.info(f"✅ yt-dlp دانلود کرد: {size} بایت, نوع: {mtype}")
                return [(filepath, mtype)], caption

        except yt_dlp.utils.DownloadError as e:
            raise DownloadError(self._friendly_error(str(e)))
        except DownloadError:
            raise
        except Exception as e:
            raise DownloadError(self._friendly_error(str(e)))

    # ========== gallery-dl ==========
    def _try_gallerydl(self, url: str, platform: str, fid: str):
        cmd = ["gallery-dl", "-g", "--no-download"]
        if platform == "reddit":
            cid = os.getenv("REDDIT_CLIENT_ID")
            if cid:
                cmd += ["-o", f"extractor.reddit.client-id={cid}", "-o", f"extractor.reddit.client-secret={os.getenv('REDDIT_CLIENT_SECRET', '')}"]
        cmd.append(url)
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            urls = [l.strip() for l in result.stdout.splitlines() if l.strip().startswith("http")][:10]
        except: return [], None

        results = []
        for i, u in enumerate(urls):
            ext = ".mp4" if any(u.lower().endswith(e) for e in (".mp4", ".mov", ".webm")) else ".jpg"
            path = os.path.join(self.download_dir, f"{fid}_{i}{ext}")
            try:
                r = requests.get(u, headers=_HEADERS, timeout=30, stream=True)
                if self._save(r, path):
                    mtype = "video" if ext == ".mp4" else "photo"
                    results.append((path, mtype))
            except: pass
        return results, None

    # ========== OG Fallback ==========
    def _try_og(self, url: str, fid: str):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15, allow_redirects=True)
            img = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', r.text, re.I)
            if not img: return [], None
            
            img_url = img.group(1).replace("&amp;", "&")
            if img_url.startswith("//"): img_url = "https:" + img_url
            
            resp = requests.get(img_url, headers=_HEADERS, timeout=20, stream=True)
            ct = resp.headers.get("Content-Type", "")
            ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
            path = os.path.join(self.download_dir, f"{fid}{ext}")
            
            if self._save(resp, path):
                return [(path, "photo")], None
        except: pass
        return [], None

    # ========== دانلود اصلی ==========
    def _download(self, url: str, platform: str):
        fid = str(uuid.uuid4())[:8]
        logger.info(f"📥 دانلود: {platform} | {url[:80]}")

        # اینستاگرام: اول HikerAPI
        if platform == "instagram" and self.hikerapi_key:
            items, cap = self._try_hikerapi(url, fid)
            if items: return items, cap

        # yt-dlp برای همه
        try:
            return self._try_ytdlp(url, platform, fid)
        except DownloadError as e:
            if "private" in str(e) or "not available" in str(e):
                raise
        
        # gallery-dl
        items, cap = self._try_gallerydl(url, platform, fid)
        if items: return items, cap

        # OG
        items, cap = self._try_og(url, fid)
        if items: return items, cap

        raise DownloadError("❌ هیچ روشی جواب نداد")

    async def download(self, url: str, platform: str):
        return await asyncio.get_running_loop().run_in_executor(None, self._download, url, platform)

    # ========== یوتیوب: کیفیت‌ها ==========
    def list_qualities(self, url: str):
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        formats = info.get("formats", [])
        heights = sorted({f["height"] for f in formats if f.get("height") and f["height"] <= 1080}, reverse=True)
        
        result = []
        for h in heights[:8]:
            size = max((f.get("filesize") or f.get("filesize_approx") or 0 for f in formats if f.get("height") == h), default=0)
            result.append({"height": h, "approx_size_mb": round(size/1024/1024) if size else None})
        
        return result, info.get("title")

    def _download_yt_quality(self, url: str, height: int):
        fid = str(uuid.uuid4())[:8]
        output = os.path.join(self.download_dir, f"{fid}.%(ext)s")
        
        opts = {
            "outtmpl": output,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": self.max_size,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "restrictfilenames": True,
            "merge_output_format": "mp4",
            "format": f"best[height<={height}][ext=mp4]/best[height<={height}]/best[ext=mp4]/best",
            "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"], "skip": ["hls", "dash"]}},
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)

            # پیدا کردن فایل
            for ext in [".mp4", ".webm", ".mkv"]:
                base = os.path.splitext(filepath)[0]
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
                raise DownloadError(f"حجم {size//1024//1024}MB از حد مجاز بیشتره. کیفیت پایین‌تر رو انتخاب کن")

            title = info.get("title", "").strip()
            caption = f"{title}\n\n{info.get('description', '').strip()}" if title else None
            
            logger.info(f"✅ یوتیوب {height}p دانلود شد: {size//1024//1024}MB")
            return [(filepath, "video")], caption[:CAPTION_LIMIT] if caption else None

    async def download_yt_quality(self, url: str, height: int):
        return await asyncio.get_running_loop().run_in_executor(None, self._download_yt_quality, url, height)

    @staticmethod
    def cleanup(items):
        for path, _ in items:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except: pass


# ============================== محدودیت و میدلور ==============================
class DownloadLimiter:
    def __init__(self, n): self._sem = asyncio.Semaphore(n)
    async def __aenter__(self): await self._sem.acquire(); return self
    async def __aexit__(self, *a): self._sem.release()

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, sec): self.sec = sec; self._last = {}
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
        except: pass
    return unjoined

def membership_kb(unjoined):
    rows = []
    for cid, title, link in unjoined:
        url = link or (f"https://t.me/{cid.lstrip('@')}" if cid.startswith("@") else None)
        if url: rows.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
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
            try: await msg.answer_video(FSInputFile(path), caption=cap)
            except: await msg.answer_document(FSInputFile(path), caption=cap)
    else:
        media = [InputMediaPhoto(media=FSInputFile(p), caption=cap if i==0 else None) if t=="photo" 
                 else InputMediaVideo(media=FSInputFile(p), caption=cap if i==0 else None) 
                 for i, (p, t) in enumerate(items)]
        try: await msg.answer_media_group(media)
        except:
            for p, t in items:
                if t == "photo": await msg.answer_photo(FSInputFile(p))
                else: await msg.answer_video(FSInputFile(p))

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
    _, sid, h = cb.data.split(":")
    height = int(h)
    entry = pending_youtube.get(sid)
    if not entry:
        await cb.answer("⏰ منقضی شده", show_alert=True)
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
            await status.edit_text("❌ نتونستم اطلاعات ویدیو رو بگیرم")
            return

        if not qualities:
            await status.edit_text("❌ کیفیتی پیدا نشد")
            return

        sid = secrets.token_hex(4)
        pending_youtube[sid] = {"url": url, "title": title, "created_at": time.time()}
        
        rows = []
        for q in qualities:
            size = f" (~{q['approx_size_mb']}MB)" if q["approx_size_mb"] else ""
            rows.append([InlineKeyboardButton(text=f"🎬 {q['height']}p{size}", callback_data=f"ytq:{sid}:{q['height']}")])
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
        if not chs: text = "⚠️ کانالی نیست"
        else: text = "📋 کانال‌ها:\n" + "\n".join(f"• {t} (`{c}`)" for c, t, _ in chs)
        await cb.message.edit_text(text, reply_markup=back_kb())

    elif act == "add":
        admin_states[cb.from_user.id] = "awaiting_add_channel"
        await cb.message.edit_text("📝 آیدی کانال رو بفرست\n(عددی یا @username)", reply_markup=back_kb())

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
    
    downloader = Downloader(config.download_dir, config.max_file_size_mb)
    limiter = DownloadLimiter(config.max_concurrent_downloads)
    
    dp["downloader"] = downloader
    dp["limiter"] = limiter
    dp["config"] = config
    dp.message.middleware(ThrottlingMiddleware(config.rate_limit_seconds))
    dp.include_router(router)
    
    await bot.delete_webhook(drop_pending_updates=True)
    
    me = await bot.get_me()
    logger.info(f"🤖 @{me.username} | ادمین‌ها: {ADMIN_IDS}")
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
