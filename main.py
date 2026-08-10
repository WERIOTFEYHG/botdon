"""
ربات دانلودر تلگرام - اینستاگرام / توییتر(X) / یوتیوب / پینترست / تیک‌تاک / ردیت
همه‌ی کد عمداً تو یه فایله تا مدیریت و آپلودش روی گیت‌هاب ساده باشه.

ویژگی‌ها:
  - اینستاگرام: اول از HikerAPI (اگه کلید ست شده) بعد yt-dlp/gallery-dl/og
  - پینترست: اول استخراج مستقیم JSON صفحه (__PWS_DATA__) بعد yt-dlp/gallery-dl/og
  - بقیه پلتفرم‌ها: بالاترین کیفیت ممکن (bestvideo+bestaudio)
  - یوتیوب: کاربر اول کیفیت رو با دکمه انتخاب می‌کنه، بعد دانلود می‌شه
  - عضویت اجباری در کانال (قابل تنظیم از پنل ادمین)
  - پنل ادمین با دکمه‌ی شیشه‌ای: آمار کاربران/دانلودها، مدیریت کانال‌های عضویت اجباری
  - پشتیبانی از Railway با Volume پایدار برای دیتابیس
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

# ============================== تنظیمات لاگر ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # محدودیت تلگرام برای کپشن عکس/ویدیو

# ============================== حالت‌های درون‌حافظه‌ای ==============================
# این‌ها برای یه پروسه‌ی تک‌نمونه‌ای کافیه
# در صورت نیاز به اسکیل افقی باید Redis جایگزین بشه
ADMIN_IDS: set[int] = set()
admin_states: Dict[int, str] = {}
pending_youtube: Dict[str, dict] = {}
_last_cleanup_time: float = time.time()

# ============================== تنظیمات ==============================

@dataclass(frozen=True)
class Config:
    bot_token: str
    max_file_size_mb: int
    download_dir: str
    max_concurrent_downloads: int
    rate_limit_seconds: float
    cookies_b64: Dict[str, str] = field(default_factory=dict)
    reddit_client_id: Optional[str] = None
    reddit_client_secret: Optional[str] = None
    reddit_user_agent: Optional[str] = None
    hikerapi_key: Optional[str] = None
    admin_ids: set[int] = field(default_factory=set)
    db_path: str = "bot_data.db"
    youtube_request_ttl: int = 300  # 5 دقیقه زمان انقضا برای درخواست‌های یوتیوب


def _get_env_str(name: str, default: str = "") -> str:
    """دریافت متغیر محیطی با نوع str"""
    return os.getenv(name, default).strip()


def _get_env_int(name: str, default: int) -> int:
    """دریافت متغیر محیطی با نوع int"""
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        logger.warning(f"مقدار نامعتبر برای {name}، استفاده از مقدار پیش‌فرض: {default}")
        return default


def _get_env_float(name: str, default: float) -> float:
    """دریافت متغیر محیطی با نوع float"""
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        logger.warning(f"مقدار نامعتبر برای {name}، استفاده از مقدار پیش‌فرض: {default}")
        return default


def _parse_admin_ids(raw: str) -> set[int]:
    """پارس آیدی ادمین‌ها از رشته جدا شده با کاما"""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():  # پشتیبانی از اعداد منفی (سوپرگروه‌ها)
            ids.add(int(part))
    return ids


def load_config() -> Config:
    """بارگذاری تنظیمات از متغیرهای محیطی"""
    bot_token = _get_env_str("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError(
            "❌ متغیر محیطی BOT_TOKEN تنظیم نشده.\n"
            "توکن رو از @BotFather بگیر و توی Variables پروژه‌ی Railway ست کن."
        )

    # تنظیم مسیر دیتابیس روی Volume پایدار Railway
    db_path = _get_env_str("DB_PATH", "/data/bot_data.db")
    # اطمینان از وجود دایرکتوری دیتابیس
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # پارس کوکی‌های رمزنگاری شده
    cookies_b64 = {}
    for platform, env_name in (
        ("instagram", "INSTAGRAM_COOKIES_B64"),
        ("youtube", "YOUTUBE_COOKIES_B64"),
        ("twitter", "TWITTER_COOKIES_B64"),
    ):
        value = _get_env_str(env_name)
        if value:
            cookies_b64[platform] = value

    # پارس آیدی ادمین‌ها
    admin_ids = _parse_admin_ids(_get_env_str("ADMIN_IDS", ""))
    
    # اضافه کردن آیدی پیش‌فرض اگر هیچ ادمینی تنظیم نشده (برای اولین بار)
    if not admin_ids:
        logger.warning("⚠️ ADMIN_IDS تنظیم نشده. پنل مدیریت غیرفعال خواهد بود.")
        logger.info("برای تنظیم ادمین، متغیر محیطی ADMIN_IDS رو ست کن. مثال: ADMIN_IDS=123456789,987654321")

    return Config(
        bot_token=bot_token,
        max_file_size_mb=_get_env_int("MAX_FILE_SIZE_MB", 50),
        download_dir=_get_env_str("DOWNLOAD_DIR", "/tmp/downloads"),
        max_concurrent_downloads=_get_env_int("MAX_CONCURRENT_DOWNLOADS", 3),
        rate_limit_seconds=_get_env_float("RATE_LIMIT_SECONDS", 3.0),
        cookies_b64=cookies_b64,
        reddit_client_id=_get_env_str("REDDIT_CLIENT_ID") or None,
        reddit_client_secret=_get_env_str("REDDIT_CLIENT_SECRET") or None,
        reddit_user_agent=_get_env_str("REDDIT_USER_AGENT", "megasaver-bot/1.0") or None,
        hikerapi_key=_get_env_str("HIKERAPI_KEY") or None,
        admin_ids=admin_ids,
        db_path=db_path,
        youtube_request_ttl=_get_env_int("YOUTUBE_REQUEST_TTL", 300),
    )


def prepare_cookie_files(cookies_b64: Dict[str, str]) -> Dict[str, str]:
    """دیکد و ذخیره کوکی‌ها در فایل موقت"""
    files: Dict[str, str] = {}
    for platform, b64_value in cookies_b64.items():
        try:
            raw = base64.b64decode(b64_value)
            path = f"/tmp/{platform}_cookies.txt"
            with open(path, "wb") as f:
                f.write(raw)
            files[platform] = path
            logger.info(f"✅ کوکی {platform} با موفقیت بارگذاری شد")
        except Exception as e:
            logger.error(f"❌ دیکد کردن کوکی {platform} ناموفق بود: {e}")
    return files


# ============================== دیتابیس ==============================
# SQLite ساده برای آمار و کانال‌های عضویت اجباری
# مسیر دیتابیس روی Volume پایدار Railway تنظیم می‌شه

DB_PATH = "bot_data.db"  # مقدار پیش‌فرض که در main بازنویسی می‌شه


def get_db_connection() -> sqlite3.Connection:
    """ایجاد کانکشن به دیتابیس با timeout مناسب"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")  # بهبود performance برای concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init() -> None:
    """ایجاد جداول دیتابیس در صورت عدم وجود"""
    try:
        conn = get_db_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, 
                first_seen TEXT NOT NULL,
                last_active TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                user_id INTEGER NOT NULL, 
                platform TEXT NOT NULL, 
                url TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS force_sub_channels (
                chat_id TEXT PRIMARY KEY, 
                title TEXT NOT NULL, 
                invite_link TEXT, 
                added_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_verifications (
                user_id INTEGER, 
                chat_id TEXT, 
                verified_at TEXT NOT NULL, 
                PRIMARY KEY(user_id, chat_id)
            )
        """)
        conn.commit()
        logger.info("✅ دیتابیس با موفقیت آماده‌سازی شد")
    except Exception as e:
        logger.critical(f"❌ خطا در آماده‌سازی دیتابیس: {e}")
        raise
    finally:
        conn.close()


def _now() -> str:
    """زمان فعلی UTC به فرمت ISO"""
    return datetime.now(timezone.utc).isoformat()


def db_record_user(user_id: int) -> None:
    """ثبت یا بروزرسانی کاربر"""
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO users (user_id, first_seen, last_active) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_active = ?",
            (user_id, _now(), _now(), _now())
        )
        conn.commit()
    except Exception as e:
        logger.error(f"خطا در ثبت کاربر {user_id}: {e}")
    finally:
        conn.close()


def db_record_download(user_id: int, platform: str, url: str = "") -> None:
    """ثبت دانلود در دیتابیس"""
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO downloads (user_id, platform, url, created_at) VALUES (?, ?, ?, ?)",
            (user_id, platform, url, _now()),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"خطا در ثبت دانلود: {e}")
    finally:
        conn.close()


def db_get_stats() -> tuple[int, int]:
    """دریافت آمار کلی"""
    try:
        conn = get_db_connection()
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        downloads = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        return users, downloads
    except Exception as e:
        logger.error(f"خطا در دریافت آمار: {e}")
        return 0, 0
    finally:
        conn.close()


def db_add_channel(chat_id: str, title: str, invite_link: Optional[str]) -> None:
    """افزودن کانال به لیست عضویت اجباری"""
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO force_sub_channels (chat_id, title, invite_link, added_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, title, invite_link, _now()),
        )
        conn.commit()
        logger.info(f"کانال {title} ({chat_id}) به لیست عضویت اجباری اضافه شد")
    except Exception as e:
        logger.error(f"خطا در افزودن کانال: {e}")
        raise
    finally:
        conn.close()


def db_remove_channel(chat_id: str) -> None:
    """حذف کانال از لیست عضویت اجباری"""
    try:
        conn = get_db_connection()
        conn.execute("DELETE FROM force_sub_channels WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM channel_verifications WHERE chat_id = ?", (chat_id,))
        conn.commit()
        logger.info(f"کانال {chat_id} از لیست عضویت اجباری حذف شد")
    except Exception as e:
        logger.error(f"خطا در حذف کانال: {e}")
        raise
    finally:
        conn.close()


def db_list_channels() -> list[tuple[str, str, Optional[str]]]:
    """لیست کانال‌های عضویت اجباری"""
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT chat_id, title, invite_link FROM force_sub_channels"
        ).fetchall()
        return rows
    except Exception as e:
        logger.error(f"خطا در دریافت لیست کانال‌ها: {e}")
        return []
    finally:
        conn.close()


def db_record_verification(user_id: int, chat_id: str) -> None:
    """ثبت تایید عضویت کاربر"""
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT OR IGNORE INTO channel_verifications (user_id, chat_id, verified_at) "
            "VALUES (?, ?, ?)",
            (user_id, chat_id, _now()),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"خطا در ثبت تایید عضویت: {e}")
    finally:
        conn.close()


def db_channel_verified_count(chat_id: str) -> int:
    """تعداد کاربرانی که عضویت در کانال رو تایید کردن"""
    try:
        conn = get_db_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM channel_verifications WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        return count
    except Exception as e:
        logger.error(f"خطا در دریافت آمار تایید: {e}")
        return 0
    finally:
        conn.close()


# ========================== تشخیص پلتفرم ==========================

ACTIVE_PLATFORMS = {"instagram", "pinterest", "tiktok", "twitter", "youtube", "reddit"}
GALLERY_DL_PLATFORMS = {"instagram", "pinterest", "twitter", "tiktok", "reddit"}

PLATFORM_PATTERNS: dict[str, re.Pattern] = {
    "instagram": re.compile(r"(instagram\.com|instagr\.am)", re.IGNORECASE),
    "pinterest": re.compile(r"(pinterest\.[a-z.]+|pin\.it)", re.IGNORECASE),
    "tiktok": re.compile(r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)", re.IGNORECASE),
    "twitter": re.compile(r"(twitter\.com|x\.com)", re.IGNORECASE),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE),
    "reddit": re.compile(r"(reddit\.com|redd\.it)", re.IGNORECASE),
}

URL_PATTERN = re.compile(r"https?://\S+")


def extract_url(text: str) -> Optional[str]:
    """استخراج اولین URL از متن"""
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def detect_platform(url: str) -> Optional[str]:
    """تشخیص پلتفرم از روی URL"""
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return platform
    return None


def is_active(platform: str) -> bool:
    """بررسی فعال بودن پلتفرم"""
    return platform in ACTIVE_PLATFORMS


def build_caption(text: Optional[str]) -> str:
    """ساخت کپشن مناسب برای تلگرام"""
    if not text or not text.strip():
        return "✅ دانلود شد"
    cleaned = text.strip()
    if len(cleaned) > 950:
        cleaned = cleaned[:950].rstrip() + "…"
    escaped = html_escape(cleaned, quote=False)
    return escaped[:CAPTION_LIMIT]


# ============================== دانلودر ==============================

class DownloadError(Exception):
    """خطای قابل‌فهم برای نمایش به کاربر"""
    pass


_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
_OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"', re.IGNORECASE)
_OG_DESC_RE = re.compile(r'<meta\s+property="og:description"\s+content="([^"]+)"', re.IGNORECASE)
_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]+)"', re.IGNORECASE)
_PWS_DATA_RE = re.compile(
    r'<script id="__PWS_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)

HIKERAPI_BASE = "https://api.hikerapi.com"

MediaItem = tuple[str, str]  # (filepath, media_type)
DownloadResult = tuple[list[MediaItem], Optional[str]]  # (آیتم‌ها, کپشن)

_DESCRIPTION_KEYS = ("description", "content", "caption", "title", "alt_text")


class Downloader:
    """کلاس اصلی دانلود با استراتژی‌های چندلایه"""
    
    def __init__(
        self,
        download_dir: str,
        max_file_size_mb: int,
        cookies_files: Optional[Dict[str, str]] = None,
        hikerapi_key: Optional[str] = None,
        reddit_client_id: Optional[str] = None,
        reddit_client_secret: Optional[str] = None,
        reddit_user_agent: Optional[str] = None,
    ):
        self.download_dir = download_dir
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.cookies_files = cookies_files or {}
        self.hikerapi_key = hikerapi_key
        self.reddit_client_id = reddit_client_id
        self.reddit_client_secret = reddit_client_secret
        self.reddit_user_agent = reddit_user_agent
        os.makedirs(download_dir, exist_ok=True)
        logger.info(f"دانلودر آماده شد. مسیر دانلود: {download_dir}, حداکثر حجم: {max_file_size_mb}MB")

    # ---------- کمکی‌ها ----------

    def _find_downloaded_file(self, file_id: str) -> Optional[str]:
        """پیدا کردن فایل دانلود شده با file_id"""
        for fname in os.listdir(self.download_dir):
            if fname.startswith(file_id):
                return os.path.join(self.download_dir, fname)
        return None

    @staticmethod
    def _friendly_message(raw_error: str) -> str:
        """تبدیل خطاهای فنی به پیام‌های کاربرپسند فارسی"""
        lowered = raw_error.lower()
        if "private" in lowered:
            return "🔒 این پست خصوصیه و قابل دانلود نیست."
        if "not available" in lowered or "unavailable" in lowered:
            return "🚫 این محتوا در دسترس نیست یا حذف شده."
        if "sign in" in lowered or "login" in lowered:
            return "🔑 این پلتفرم برای این لینک نیاز به لاگین داره."
        if "too large" in lowered or "file size" in lowered:
            return "📦 حجم فایل بیشتر از حد مجازه."
        if "not found" in lowered or "404" in lowered:
            return "🔍 محتوای مورد نظر پیدا نشد."
        return "❌ لینک قابل پردازش نیست؛ لطفا لینک رو چک کن."

    @staticmethod
    def _guess_media_type(path_or_url: str) -> str:
        """تشخیص نوع مدیا (عکس/ویدیو) از روی مسیر یا URL"""
        lowered = path_or_url.lower().split("?")[0]
        if lowered.endswith((".mp4", ".mov", ".webm", ".mkv", ".avi")):
            return "video"
        if lowered.endswith((".gif",)):
            return "video"  # تلگرام گیف رو به عنوان ویدیو می‌فرسته
        return "photo"

    def _save_stream(self, resp: requests.Response, filepath: str) -> bool:
        """ذخیره محتوای stream با کنترل حجم"""
        total = 0
        try:
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    total += len(chunk)
                    if total > self.max_file_size:
                        f.close()
                        os.remove(filepath)
                        logger.warning(f"حجم فایل {total} بایت از حد مجاز {self.max_file_size} بیشتر شد")
                        return False
                    f.write(chunk)
            if total == 0:
                os.remove(filepath)
                logger.warning("فایل دانلود شده خالی بود")
                return False
            return True
        except Exception as e:
            logger.error(f"خطا در ذخیره فایل {filepath}: {e}")
            if os.path.exists(filepath):
                os.remove(filepath)
            return False

    # ---------- لایه‌ی ویژه‌ی اینستاگرام: HikerAPI ----------

    def _try_hikerapi(self, url: str, file_id: str) -> DownloadResult:
        """دانلود از اینستاگرام با HikerAPI"""
        if not self.hikerapi_key:
            return [], None
        try:
            resp = requests.get(
                f"{HIKERAPI_BASE}/v2/media/info/by/url",
                params={"url": url},
                headers={"x-access-key": self.hikerapi_key},
                timeout=20,
            )
            if resp.status_code != 200:
                logger.warning(f"HikerAPI پاسخ غیر 200 داد: {resp.status_code}")
                return [], None
            data = resp.json()
        except requests.RequestException as e:
            logger.warning(f"خطا در اتصال به HikerAPI: {e}")
            return [], None
        except ValueError as e:
            logger.warning(f"پاسخ HikerAPI JSON معتبر نبود: {e}")
            return [], None

        if not isinstance(data, dict):
            return [], None

        caption = None
        cap = data.get("caption")
        if isinstance(cap, dict):
            caption = cap.get("text")
        elif isinstance(cap, str):
            caption = cap

        media_entries = data.get("carousel_media") or [data]
        results: list[MediaItem] = []
        
        for idx, item in enumerate(media_entries[:10]):
            video_versions = item.get("video_versions")
            if video_versions:
                media_url = video_versions[0].get("url")
                media_type = "video"
                ext = ".mp4"
            else:
                candidates = (item.get("image_versions2") or {}).get("candidates") or []
                media_url = candidates[0].get("url") if candidates else None
                media_type = "photo"
                ext = ".jpg"
            
            if not media_url:
                continue
                
            filepath = os.path.join(self.download_dir, f"{file_id}_{idx}{ext}")
            try:
                media_resp = requests.get(media_url, headers=_HTTP_HEADERS, timeout=30, stream=True)
                media_resp.raise_for_status()
            except requests.RequestException as e:
                logger.warning(f"خطا در دانلود آیتم {idx} از HikerAPI: {e}")
                continue
                
            if self._save_stream(media_resp, filepath):
                results.append((filepath, media_type))

        if results:
            logger.info(f"HikerAPI: {len(results)} آیتم دانلود شد")
        return results, caption

    # ---------- لایه‌ی ویژه‌ی پینترست: استخراج مستقیم JSON صفحه ----------

    def _try_pinterest_native(self, url: str, file_id: str) -> DownloadResult:
        """استخراج مستقیم از JSON پینترست"""
        try:
            resp = requests.get(url, headers=_HTTP_HEADERS, timeout=15, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"خطا در دریافت صفحه پینترست: {e}")
            return [], None

        match = _PWS_DATA_RE.search(resp.text)
        if not match:
            logger.warning("پینترست: __PWS_DATA__ پیدا نشد")
            return [], None

        try:
            data = json.loads(match.group(1))
            pins = data["props"]["initialReduxState"]["pins"]
            pin_id = next(iter(pins))
            pin = pins[pin_id]
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError) as e:
            logger.warning(f"پینترست: خطا در پارس JSON: {e}")
            return [], None

        media_type = "photo"
        media_url = (pin.get("videos") or {}).get("video_list", {}).get("V_720P", {}).get("url")
        if media_url:
            media_type = "video"
        else:
            media_url = (pin.get("images") or {}).get("orig", {}).get("url")

        if not media_url:
            logger.warning("پینترست: URL مدیا پیدا نشد")
            return [], None

        caption = pin.get("description") or pin.get("grid_title") or None

        ext = ".mp4" if media_type == "video" else ".jpg"
        filepath = os.path.join(self.download_dir, f"{file_id}{ext}")
        try:
            media_resp = requests.get(media_url, headers=_HTTP_HEADERS, timeout=30, stream=True)
            media_resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"پینترست: خطا در دانلود مدیا: {e}")
            return [], None

        if self._save_stream(media_resp, filepath):
            logger.info("پینترست: استخراج مستقیم موفقیت‌آمیز بود")
            return [(filepath, media_type)], caption
        return [], None

    # ---------- لایه‌ی عمومی: yt-dlp (بالاترین کیفیت ممکن) ----------

    def _ytdlp_opts(self, platform: str, output_path: str) -> dict:
        """تنظیمات yt-dlp"""
        opts = {
            "outtmpl": output_path,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": self.max_file_size,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "restrictfilenames": True,
            "format": "bestvideo*+bestaudio/best",  # بالاترین کیفیت ممکن
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
        }
        if platform in self.cookies_files:
            opts["cookiefile"] = self.cookies_files[platform]
        return opts

    def _try_ytdlp(self, url: str, platform: str, file_id: str) -> DownloadResult:
        """دانلود با yt-dlp"""
        output_template = os.path.join(self.download_dir, f"{file_id}.%(ext)s")
        ydl_opts = self._ytdlp_opts(platform, output_template)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)

                # بررسی فایل merged
                base, _ext = os.path.splitext(filepath)
                merged_path = base + ".mp4"
                if os.path.exists(merged_path):
                    filepath = merged_path
                elif not os.path.exists(filepath):
                    filepath = self._find_downloaded_file(file_id)

                if not filepath or not os.path.exists(filepath):
                    raise DownloadError("فایلی دانلود نشد.")

                size = os.path.getsize(filepath)
                if size == 0:
                    os.remove(filepath)
                    raise DownloadError("فایل دانلودشده خالی بود.")
                if size > self.max_file_size:
                    os.remove(filepath)
                    raise DownloadError(
                        f"حجم فایل بیشتر از {self.max_file_size // (1024 * 1024)} مگابایته."
                    )

                media_type = self._guess_media_type(filepath)

                # استخراج کپشن
                title = (info.get("title") or "").strip()
                description = (info.get("description") or "").strip()
                caption_parts = [p for p in (title, description) if p]
                caption = "\n\n".join(caption_parts) if caption_parts else None

                logger.info(f"yt-dlp: دانلود موفق - {platform} - {len(filepath)} بایت")
                return [(filepath, media_type)], caption

        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"yt-dlp DownloadError: {e}")
            raise DownloadError(self._friendly_message(str(e)))
        except DownloadError:
            raise
        except Exception as e:
            logger.error(f"خطای غیرمنتظره در yt-dlp: {e}")
            raise DownloadError(self._friendly_message(str(e)))

    # ---------- لایه‌ی gallery-dl ----------

    def _reddit_extra_opts(self) -> list[str]:
        """تنظیمات اضافی برای ردیت"""
        if not (self.reddit_client_id and self.reddit_client_secret):
            return []
        return [
            "-o", f"extractor.reddit.client-id={self.reddit_client_id}",
            "-o", f"extractor.reddit.client-secret={self.reddit_client_secret}",
            "-o", f"extractor.reddit.user-agent={self.reddit_user_agent or 'megasaver-bot/1.0'}",
        ]

    def _run_gallery_dl_json(self, url: str, platform: str) -> Optional[list]:
        """اجرای gallery-dl برای دریافت JSON"""
        cmd = ["gallery-dl", "-j", "--no-download"]
        if platform in self.cookies_files:
            cmd += ["--cookies", self.cookies_files[platform]]
        if platform == "reddit":
            cmd += self._reddit_extra_opts()
        cmd.append(url)
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0 or not result.stdout.strip():
                logger.warning(f"gallery-dl JSON خروجی نداد: {result.stderr[:200]}")
                return None
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            logger.warning("gallery-dl JSON timeout شد")
            return None
        except FileNotFoundError:
            logger.error("gallery-dl نصب نیست")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"gallery-dl JSON پارس نشد: {e}")
            return None

    def _run_gallery_dl_urls_only(self, url: str, platform: str) -> list[str]:
        """اجرای gallery-dl برای دریافت URL های مستقیم"""
        cmd = ["gallery-dl", "-g", "--no-download"]
        if platform in self.cookies_files:
            cmd += ["--cookies", self.cookies_files[platform]]
        if platform == "reddit":
            cmd += self._reddit_extra_opts()
        cmd.append(url)
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                return []
            return [line.strip() for line in result.stdout.splitlines() 
                   if line.strip().startswith("http")][:10]
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning(f"gallery-dl URL extraction failed: {e}")
            return []

    def _extract_gallery_dl_caption(self, entries: list) -> Optional[str]:
        """استخراج کپشن از خروجی gallery-dl"""
        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            meta = entry[2]
            if not isinstance(meta, dict):
                continue
            for key in _DESCRIPTION_KEYS:
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _try_gallery_dl(self, url: str, platform: str, file_id: str) -> DownloadResult:
        """دانلود با gallery-dl"""
        media_urls: list[str] = []
        caption: Optional[str] = None

        # اول JSON رو امتحان کن
        entries = self._run_gallery_dl_json(url, platform)
        if entries:
            caption = self._extract_gallery_dl_caption(entries)
            for entry in entries:
                if (isinstance(entry, list) and len(entry) >= 2 and 
                    isinstance(entry[1], str) and entry[1].startswith("http")):
                    media_urls.append(entry[1])
            media_urls = media_urls[:10]

        # اگر JSON جواب نداد، URL های مستقیم رو بگیر
        if not media_urls:
            media_urls = self._run_gallery_dl_urls_only(url, platform)

        if not media_urls:
            return [], None

        results: list[MediaItem] = []
        for idx, media_url in enumerate(media_urls):
            ext = ".mp4" if self._guess_media_type(media_url) == "video" else ".jpg"
            filepath = os.path.join(self.download_dir, f"{file_id}_{idx}{ext}")
            try:
                resp = requests.get(media_url, headers=_HTTP_HEADERS, timeout=30, stream=True)
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.warning(f"gallery-dl: خطا در دانلود آیتم {idx}: {e}")
                continue
            if self._save_stream(resp, filepath):
                results.append((filepath, self._guess_media_type(media_url)))

        if results:
            logger.info(f"gallery-dl: {len(results)} آیتم دانلود شد")
        return results, caption

    # ---------- لایه‌ی آخر: og:image / og:description ----------

    def _try_og_fallback(self, url: str, file_id: str) -> DownloadResult:
        """آخرین شانس: استخراج از Open Graph tags"""
        try:
            page = requests.get(url, headers=_HTTP_HEADERS, timeout=15, allow_redirects=True)
            page.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"OG fallback: خطا در دریافت صفحه: {e}")
            return [], None

        img_match = _OG_IMAGE_RE.search(page.text)
        if not img_match:
            logger.warning("OG fallback: og:image پیدا نشد")
            return [], None

        # استخراج کپشن از og:description یا og:title
        desc_match = _OG_DESC_RE.search(page.text)
        title_match = _OG_TITLE_RE.search(page.text)
        caption = None
        if desc_match:
            caption = desc_match.group(1).replace("&amp;", "&")
        elif title_match:
            caption = title_match.group(1).replace("&amp;", "&")

        image_url = img_match.group(1).replace("&amp;", "&")
        
        # اصلاح URL های نسبی
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif image_url.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(url)
            image_url = f"{parsed.scheme}://{parsed.netloc}{image_url}"

        try:
            resp = requests.get(image_url, headers=_HTTP_HEADERS, timeout=20, stream=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"OG fallback: خطا در دانلود تصویر: {e}")
            return [], None

        content_type = resp.headers.get("Content-Type", "")
        ext = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
        filepath = os.path.join(self.download_dir, f"{file_id}{ext}")

        if self._save_stream(resp, filepath):
            logger.info("OG fallback: دانلود موفق")
            return [(filepath, "photo")], caption
        return [], None

    # ---------- نقطه‌ی ورود عمومی (همه پلتفرم‌ها به‌جز یوتیوب) ----------

    def _download_sync(self, url: str, platform: str) -> DownloadResult:
        """دانلود همگام (در thread pool اجرا می‌شه)"""
        file_id = str(uuid.uuid4())
        last_error = "لینک قابل پردازش نیست."
        logger.info(f"شروع دانلود - پلتفرم: {platform}, URL: {url[:50]}...")

        # استراتژی ویژه اینستاگرام
        if platform == "instagram" and self.hikerapi_key:
            try:
                items, caption = self._try_hikerapi(url, file_id)
                if items:
                    return items, caption
            except Exception as e:
                logger.exception(f"خطای HikerAPI: {e}")

        # استراتژی ویژه پینترست
        if platform == "pinterest":
            try:
                items, caption = self._try_pinterest_native(url, file_id)
                if items:
                    return items, caption
            except Exception as e:
                logger.exception(f"خطای استخراج مستقیم پینترست: {e}")

        # yt-dlp برای همه
        try:
            return self._try_ytdlp(url, platform, file_id)
        except yt_dlp.utils.DownloadError as e:
            last_error = str(e)
            logger.warning(f"yt-dlp شکست خورد: {e}")
        except DownloadError as e:
            last_error = str(e)
        except Exception as e:
            logger.exception(f"خطای غیرمنتظره در yt-dlp: {e}")
            last_error = str(e)

        # gallery-dl برای پلتفرم‌های پشتیبانی شده
        if platform in GALLERY_DL_PLATFORMS:
            try:
                items, caption = self._try_gallery_dl(url, platform, file_id)
                if items:
                    return items, caption
            except Exception as e:
                logger.exception(f"خطای غیرمنتظره در gallery-dl: {e}")

        # آخرین شانس: OG tags
        items, caption = self._try_og_fallback(url, file_id)
        if items:
            return items, caption

        raise DownloadError(self._friendly_message(last_error))

    async def download(self, url: str, platform: str) -> DownloadResult:
        """دانلود async با اجرا در thread pool"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, url, platform)

    # ---------- مسیر ویژه‌ی یوتیوب: انتخاب کیفیت ----------

    def _youtube_base_opts(self) -> dict:
        """تنظیمات پایه یوتیوب"""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
        }
        if "youtube" in self.cookies_files:
            opts["cookiefile"] = self.cookies_files["youtube"]
        return opts

    def list_youtube_qualities(self, url: str) -> tuple[list[dict], Optional[str]]:
        """دریافت لیست کیفیت‌های موجود برای ویدیوی یوتیوب"""
        opts = self._youtube_base_opts()
        opts["skip_download"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats", []) or []
        heights = sorted(
            {f.get("height") for f in formats if f.get("height") and f.get("height") > 0},
            reverse=True
        )

        results = []
        for h in heights[:8]:  # حداکثر 8 کیفیت
            candidates = [f for f in formats if f.get("height") == h]
            size = 0
            for f in candidates:
                s = f.get("filesize") or f.get("filesize_approx") or 0
                if s > size:
                    size = s
            results.append({
                "height": h,
                "approx_size_mb": round(size / (1024 * 1024)) if size else None,
            })

        logger.info(f"یوتیوب: {len(results)} کیفیت پیدا شد")
        return results, info.get("title")

    def _download_youtube_sync(self, url: str, height: int) -> DownloadResult:
        """دانلود ویدیوی یوتیوب با کیفیت انتخابی"""
        file_id = str(uuid.uuid4())
        output_template = os.path.join(self.download_dir, f"{file_id}.%(ext)s")

        opts = self._youtube_base_opts()
        opts.update({
            "outtmpl": output_template,
            "max_filesize": self.max_file_size,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "restrictfilenames": True,
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
            "merge_output_format": "mp4",
        })

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)

                base, _ext = os.path.splitext(filepath)
                merged_path = base + ".mp4"
                if os.path.exists(merged_path):
                    filepath = merged_path
                elif not os.path.exists(filepath):
                    filepath = self._find_downloaded_file(file_id)

                if not filepath or not os.path.exists(filepath):
                    raise DownloadError("فایلی دانلود نشد.")

                size = os.path.getsize(filepath)
                if size == 0:
                    os.remove(filepath)
                    raise DownloadError("فایل دانلودشده خالی بود.")
                if size > self.max_file_size:
                    os.remove(filepath)
                    raise DownloadError(
                        f"فایل با کیفیت {height}p بزرگتر از "
                        f"{self.max_file_size // (1024 * 1024)} مگابایته؛ کیفیت پایین‌تری رو امتحان کن."
                    )

                title = (info.get("title") or "").strip()
                description = (info.get("description") or "").strip()
                caption_parts = [p for p in (title, description) if p]
                caption = "\n\n".join(caption_parts) if caption_parts else None

                logger.info(f"یوتیوب: دانلود موفق - {height}p - {size} بایت")
                return [(filepath, "video")], caption

        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"یوتیوب DownloadError: {e}")
            raise DownloadError(self._friendly_message(str(e)))
        except DownloadError:
            raise
        except Exception as e:
            logger.error(f"خطای غیرمنتظره در دانلود یوتیوب: {e}")
            raise DownloadError(self._friendly_message(str(e)))

    async def download_youtube(self, url: str, height: int) -> DownloadResult:
        """دانلود async یوتیوب"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_youtube_sync, url, height)

    @staticmethod
    def cleanup(items: list[MediaItem]) -> None:
        """پاکسازی فایل‌های موقت"""
        for filepath, _ in items:
            try:
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
                    logger.debug(f"فایل موقت پاک شد: {filepath}")
            except OSError as e:
                logger.warning(f"پاک کردن فایل {filepath} ناموفق بود: {e}")


# ===================== محدودکننده‌ی دانلود همزمان =====================

class DownloadLimiter:
    """محدودکننده تعداد دانلودهای همزمان با Semaphore"""
    
    def __init__(self, max_concurrent: int):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        logger.info(f"محدودیت دانلود همزمان: {max_concurrent}")

    async def __aenter__(self):
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._semaphore.release()


# ========================= میدلور ضد اسپم =========================

class ThrottlingMiddleware(BaseMiddleware):
    """میدلور محدودیت نرخ درخواست"""
    
    def __init__(self, rate_limit_seconds: float):
        self.rate_limit_seconds = rate_limit_seconds
        self._last_request: Dict[int, float] = {}
        logger.info(f"محدودیت نرخ: {rate_limit_seconds} ثانیه")

    async def __call__(
        self,
        handler,
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id if event.from_user else None
        if user_id is not None and user_id not in ADMIN_IDS:
            now = time.monotonic()
            last = self._last_request.get(user_id)
            if last is not None and (now - last) < self.rate_limit_seconds:
                remaining = round(self.rate_limit_seconds - (now - last), 1)
                await event.answer(f"⏳ لطفا {remaining} ثانیه صبر کن و دوباره امتحان کن")
                return
            self._last_request[user_id] = now
        
        # پاکسازی حافظه هر 100 درخواست
        if len(self._last_request) > 10000:
            self._last_request.clear()
            
        return await handler(event, data)


# ========================= عضویت اجباری =========================

async def get_unjoined_channels(bot: Bot, user_id: int) -> list[tuple[str, str, Optional[str]]]:
    """دریافت کانال‌هایی که کاربر هنوز عضو نشده"""
    channels = db_list_channels()
    unjoined = []
    for chat_id, title, invite_link in channels:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in ("left", "kicked"):
                unjoined.append((chat_id, title, invite_link))
                logger.debug(f"کاربر {user_id} عضو {title} نیست")
        except Exception as e:
            logger.warning(f"چک عضویت کانال {chat_id} برای کاربر {user_id} ناموفق بود: {e}")
            # اگر ربات ادمین نباشه، از بررسی صرف‌نظر می‌کنیم
            continue
    return unjoined


def build_membership_keyboard(unjoined: list[tuple[str, str, Optional[str]]]) -> InlineKeyboardMarkup:
    """ساخت کیبورد عضویت در کانال‌ها"""
    rows = []
    for chat_id, title, invite_link in unjoined:
        url = invite_link
        if not url and isinstance(chat_id, str) and chat_id.startswith("@"):
            url = f"https://t.me/{chat_id.lstrip('@')}"
        if url:
            rows.append([InlineKeyboardButton(text=f"📢 عضویت در {title}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ عضو شدم", callback_data="checksub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================== هندلرهای عمومی ==============================

router = Router(name="main")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """دستور /start"""
    db_record_user(message.from_user.id)
    await message.answer(
        "👋 سلام!\n\n"
        "🔗 لینک پست/ریلز اینستاگرام، توییتر(X)، یوتیوب، پینترست، تیک‌تاک یا ردیت رو بفرست "
        "تا برات دانلودش کنم — همراه با کپشن/توضیحاتش.\n\n"
        "📸 پست‌های چندعکسی (کاروسل) هم پشتیبانی می‌شن.\n\n"
        "📊 دستور /admin برای پنل مدیریت (مخصوص ادمین‌ها)"
    )


async def _send_media(message: Message, items: list[MediaItem], caption: Optional[str]) -> None:
    """ارسال مدیا به کاربر"""
    final_caption = build_caption(caption)

    if len(items) == 1:
        filepath, media_type = items[0]
        file = FSInputFile(filepath)
        if media_type == "photo":
            await message.answer_photo(photo=file, caption=final_caption)
        else:
            try:
                await message.answer_video(video=file, caption=final_caption)
            except Exception:
                # اگر به عنوان ویدیو نشد، به عنوان سند بفرست
                file = FSInputFile(filepath)
                await message.answer_document(document=file, caption=final_caption)
        return

    # ارسال آلبوم برای چند فایل
    media_group = []
    for idx, (filepath, media_type) in enumerate(items):
        file = FSInputFile(filepath)
        item_caption = final_caption if idx == 0 else None
        if media_type == "photo":
            media_group.append(InputMediaPhoto(media=file, caption=item_caption))
        else:
            media_group.append(InputMediaVideo(media=file, caption=item_caption))

    try:
        await message.answer_media_group(media=media_group)
    except Exception as e:
        logger.error(f"خطا در ارسال MediaGroup: {e}")
        # اگر MediaGroup خطا داد، تک‌تک بفرست
        for filepath, media_type in items:
            file = FSInputFile(filepath)
            if media_type == "photo":
                await message.answer_photo(photo=file)
            else:
                await message.answer_video(video=file)


async def cleanup_expired_youtube_requests(config: Config) -> None:
    """پاکسازی درخواست‌های منقضی شده یوتیوب"""
    global _last_cleanup_time
    now = time.time()
    
    # هر 60 ثانیه یکبار پاکسازی کن
    if now - _last_cleanup_time < 60:
        return
    
    _last_cleanup_time = now
    expired = [
        k for k, v in pending_youtube.items()
        if now - v.get("created_at", 0) > config.youtube_request_ttl
    ]
    for k in expired:
        del pending_youtube[k]
    if expired:
        logger.info(f"{len(expired)} درخواست منقضی شده یوتیوب پاکسازی شد")


async def handle_youtube_link(message: Message, downloader: Downloader, url: str, config: Config) -> None:
    """مدیریت لینک‌های یوتیوب"""
    await cleanup_expired_youtube_requests(config)
    
    status_msg = await message.answer("⏳ در حال بررسی کیفیت‌های موجود...")
    try:
        loop = asyncio.get_running_loop()
        qualities, title = await loop.run_in_executor(None, downloader.list_youtube_qualities, url)
    except Exception as e:
        logger.exception("خطا در گرفتن کیفیت‌های یوتیوب")
        await status_msg.edit_text(
            "❌ نتونستم اطلاعات این ویدیو رو بگیرم.\n"
            "ممکنه یوتیوب درخواست رو بلاک کرده باشه یا ویدیو در دسترس نباشه."
        )
        return

    if not qualities:
        await status_msg.edit_text("❌ کیفیتی برای این ویدیو پیدا نشد.")
        return

    short_id = secrets.token_hex(4)
    pending_youtube[short_id] = {
        "url": url,
        "title": title,
        "created_at": time.time(),
    }

    rows = []
    for q in qualities:
        size_txt = f" (~{q['approx_size_mb']}MB)" if q["approx_size_mb"] else ""
        rows.append([InlineKeyboardButton(
            text=f"🎬 {q['height']}p{size_txt}",
            callback_data=f"ytq:{short_id}:{q['height']}",
        )])
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data=f"ytcancel:{short_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    header = f"🎥 {title}\n\n" if title else ""
    await status_msg.edit_text(
        f"{header}کیفیت مورد نظر رو انتخاب کن:",
        reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("ytcancel:"))
async def cb_youtube_cancel(callback: CallbackQuery) -> None:
    """لغو درخواست یوتیوب"""
    try:
        short_id = callback.data.split(":", 1)[1]
        pending_youtube.pop(short_id, None)
    except (ValueError, AttributeError):
        pass
    await callback.answer("درخواست لغو شد")
    await callback.message.edit_text("❌ دانلود لغو شد")


@router.callback_query(F.data.startswith("ytq:"))
async def cb_youtube_quality(callback: CallbackQuery, downloader: Downloader, limiter: DownloadLimiter) -> None:
    """پردازش انتخاب کیفیت یوتیوب"""
    try:
        _, short_id, height_str = callback.data.split(":")
        height = int(height_str)
    except (ValueError, AttributeError):
        await callback.answer("درخواست نامعتبره.", show_alert=True)
        return

    entry = pending_youtube.get(short_id)
    if not entry:
        await callback.answer("⏰ این درخواست منقضی شده، لینک رو دوباره بفرست.", show_alert=True)
        await callback.message.edit_text("⏰ درخواست منقضی شد. لطفا دوباره لینک رو بفرست.")
        return

    await callback.answer()
    await callback.message.edit_text(f"⏳ در حال دانلود با کیفیت {height}p...")

    items: list[MediaItem] = []
    try:
        async with limiter:
            items, caption = await downloader.download_youtube(entry["url"], height)
        await _send_media(callback.message, items, caption or entry.get("title"))
        db_record_download(callback.from_user.id, "youtube", entry["url"])
        await callback.message.delete()
    except DownloadError as e:
        await callback.message.edit_text(f"❌ {e}")
    except Exception:
        logger.exception("خطای غیرمنتظره در دانلود یوتیوب")
        await callback.message.edit_text("❌ یه مشکلی پیش اومد. لطفا دوباره امتحان کن.")
    finally:
        if items:
            Downloader.cleanup(items)
        pending_youtube.pop(short_id, None)


@router.callback_query(F.data == "checksub")
async def cb_checksub(callback: CallbackQuery) -> None:
    """بررسی عضویت کاربر در کانال‌ها"""
    unjoined = await get_unjoined_channels(callback.bot, callback.from_user.id)
    if unjoined:
        await callback.answer("⚠️ هنوز عضو همه‌ی کانال‌ها نشدی.", show_alert=True)
        return
    
    # ثبت تایید عضویت
    for chat_id, _title, _link in db_list_channels():
        db_record_verification(callback.from_user.id, chat_id)
    
    await callback.answer("✅ عضویت تایید شد!", show_alert=True)
    await callback.message.edit_text("✅ عضویت تایید شد! حالا لینکت رو بفرست.")


@router.message(F.text)
async def handle_link(message: Message, downloader: Downloader, limiter: DownloadLimiter, config: Config) -> None:
    """هندلر اصلی پردازش لینک‌ها"""
    user_id = message.from_user.id
    db_record_user(user_id)

    text = message.text or ""

    # اگر ادمین در حالت افزودن کانال باشه
    if user_id in ADMIN_IDS and admin_states.get(user_id) == "awaiting_add_channel":
        await _process_add_channel(message)
        return

    # بررسی عضویت اجباری (فقط برای غیر ادمین‌ها)
    if user_id not in ADMIN_IDS:
        unjoined = await get_unjoined_channels(message.bot, user_id)
        if unjoined:
            await message.answer(
                "🔒 برای استفاده از ربات باید عضو کانال‌های زیر بشی 👇",
                reply_markup=build_membership_keyboard(unjoined),
            )
            return

    # استخراج URL
    url = extract_url(text)
    if not url:
        await message.answer("❌ لینک معتبری پیدا نکردم. لطفا یه لینک از پلتفرم‌های پشتیبانی‌شده بفرست.")
        return

    # تشخیص پلتفرم
    platform = detect_platform(url)
    if platform is None:
        await message.answer(
            "❌ این لینک رو نشناختم.\n"
            "پلتفرم‌های پشتیبانی شده: اینستاگرام، توییتر/X، یوتیوب، پینترست، تیک‌تاک، ردیت"
        )
        return

    if not is_active(platform):
        await message.answer(f"⚠️ پشتیبانی از {platform} هنوز فعال نشده.")
        return

    # مسیر ویژه یوتیوب
    if platform == "youtube":
        await handle_youtube_link(message, downloader, url, config)
        return

    # دانلود برای بقیه پلتفرم‌ها
    status_msg = await message.answer("⏳ در حال دانلود...")

    items: list[MediaItem] = []
    try:
        async with limiter:
            items, caption = await downloader.download(url, platform)

        await _send_media(message, items, caption)
        db_record_download(user_id, platform, url)
        await status_msg.delete()

    except DownloadError as e:
        await status_msg.edit_text(f"❌ {e}")
    except Exception:
        logger.exception(f"خطای غیرمنتظره در پردازش لینک {url}")
        await status_msg.edit_text("❌ یه مشکلی پیش اومد، دوباره امتحان کن.")
    finally:
        if items:
            Downloader.cleanup(items)


# ============================== پنل ادمین ==============================

def admin_main_keyboard() -> InlineKeyboardMarkup:
    """کیبورد اصلی پنل ادمین"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار کلی", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📋 لیست کانال‌ها", callback_data="admin:list")],
        [InlineKeyboardButton(text="➕ افزودن کانال", callback_data="admin:add")],
        [InlineKeyboardButton(text="➖ حذف کانال", callback_data="admin:remove")],
        [InlineKeyboardButton(text="🔍 وضعیت ربات", callback_data="admin:status")],
    ])


def admin_back_keyboard() -> InlineKeyboardMarkup:
    """کیبورد بازگشت"""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ بازگشت به پنل", callback_data="admin:back")]]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """دستور /admin - پنل مدیریت"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔️ شما دسترسی به پنل مدیریت ندارید.")
        return
    
    admin_states.pop(message.from_user.id, None)
    await message.answer("🛠 پنل مدیریت ربات", reply_markup=admin_main_keyboard())


async def _process_add_channel(message: Message) -> None:
    """پردازش افزودن کانال جدید"""
    admin_states.pop(message.from_user.id, None)
    raw = (message.text or "").strip()

    # بررسی و دریافت اطلاعات کانال
    try:
        chat = await message.bot.get_chat(raw)
    except Exception as e:
        logger.warning(f"خطا در دریافت اطلاعات کانال {raw}: {e}")
        await message.answer(
            "❌ نتونستم این کانال رو پیدا کنم.\n"
            "مطمئن شو آیدی/یوزرنیم درسته و ربات از قبل به‌عنوان ادمین به کانال اضافه شده.",
            reply_markup=admin_back_keyboard(),
        )
        return

    # بررسی ادمین بودن ربات
    try:
        me = await message.bot.me()
        member = await message.bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await message.answer(
                "⚠️ ربات توی این کانال ادمین نیست.\n"
                "اول ربات رو ادمین کن، بعد دوباره امتحان کن.",
                reply_markup=admin_back_keyboard(),
            )
            return
    except Exception as e:
        logger.warning(f"خطا در بررسی وضعیت ادمین: {e}")
        await message.answer(
            "⚠️ نتونستم وضعیت ادمین بودن ربات رو چک کنم.\n"
            "مطمئن شو ربات عضو و ادمین کانال هست.",
            reply_markup=admin_back_keyboard(),
        )
        return

    # دریافت لینک دعوت
    title = chat.title or raw
    invite_link = None
    if chat.username:
        invite_link = f"https://t.me/{chat.username}"
    else:
        try:
            invite_link = await message.bot.export_chat_invite_link(chat.id)
        except Exception as e:
            logger.warning(f"نتونست لینک دعوت برای {chat.id} بسازه: {e}")
            invite_link = None

    # ذخیره در دیتابیس
    db_add_channel(str(chat.id), title, invite_link)
    await message.answer(
        f"✅ کانال «{title}» با موفقیت به لیست عضویت اجباری اضافه شد.",
        reply_markup=admin_back_keyboard(),
    )


@router.callback_query(F.data.startswith("admin:"))
async def cb_admin(callback: CallbackQuery) -> None:
    """پردازش دکمه‌های پنل ادمین"""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔️ دسترسی ندارید.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "stats":
        users, downloads = db_get_stats()
        channels = db_list_channels()
        
        lines = [
            "📊 **آمار کلی ربات**",
            "",
            f"👥 تعداد کاربران: {users}",
            f"⬇️ محتوای دانلود شده: {downloads}",
            "",
            "📢 **آمار عضویت اجباری:**",
        ]
        
        if not channels:
            lines.append("⚠️ هیچ کانالی تعریف نشده.")
        else:
            for chat_id, title, _link in channels:
                verified = db_channel_verified_count(chat_id)
                try:
                    total = await callback.bot.get_chat_member_count(chat_id)
                except Exception:
                    total = "؟"
                lines.append(f"• {title}")
                lines.append(f"  └ تایید شده: {verified} | کل اعضا: {total}")
        
        await callback.message.edit_text(
            "\n".join(lines), 
            reply_markup=admin_back_keyboard()
        )

    elif action == "list":
        channels = db_list_channels()
        if not channels:
            await callback.message.edit_text(
                "⚠️ هیچ کانالی ثبت نشده.", 
                reply_markup=admin_back_keyboard()
            )
        else:
            text = "📋 **کانال‌های عضویت اجباری:**\n\n"
            for chat_id, title, invite_link in channels:
                link_text = f" - [لینک]({invite_link})" if invite_link else ""
                text += f"• {title} (`{chat_id}`){link_text}\n"
            await callback.message.edit_text(text, reply_markup=admin_back_keyboard())

    elif action == "add":
        admin_states[callback.from_user.id] = "awaiting_add_channel"
        await callback.message.edit_text(
            "📝 **آیدی کانال رو بفرست:**\n\n"
            "می‌تونی یکی از این موارد رو بفرستی:\n"
            "• آیدی عددی (مثلاً `-1001234567890`)\n"
            "• یوزرنیم (مثلاً `@channel`)\n"
            "• لینک دعوت (مثلاً `https://t.me/channel`)\n\n"
            "⚠️ **قبلش حتماً ربات رو ادمین کانال کن!**",
            reply_markup=admin_back_keyboard(),
        )

    elif action == "remove":
        channels = db_list_channels()
        if not channels:
            await callback.message.edit_text(
                "⚠️ کانالی برای حذف وجود نداره.", 
                reply_markup=admin_back_keyboard()
            )
        else:
            rows = [
                [InlineKeyboardButton(
                    text=f"🗑 {title}", 
                    callback_data=f"admin:rm:{chat_id}"
                )]
                for chat_id, title, _link in channels
            ]
            rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="admin:back")])
            await callback.message.edit_text(
                "🗑 **کدوم کانال حذف بشه؟**",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )

    elif action.startswith("rm:"):
        chat_id = action.split(":", 1)[1]
        try:
            db_remove_channel(chat_id)
            await callback.message.edit_text(
                "✅ کانال با موفقیت حذف شد.", 
                reply_markup=admin_back_keyboard()
            )
        except Exception as e:
            logger.error(f"خطا در حذف کانال {chat_id}: {e}")
            await callback.message.edit_text(
                "❌ خطا در حذف کانال.", 
                reply_markup=admin_back_keyboard()
            )

    elif action == "status":
        # نمایش وضعیت فعلی ربات
        import platform as pf
        status_text = (
            "🔍 **وضعیت ربات**\n\n"
            f"• پایتون: {pf.python_version()}\n"
            f"• پلتفرم: {pf.system()} {pf.machine()}\n"
            f"• آیدی‌های ادمین: {len(ADMIN_IDS)} نفر\n"
            f"• کوکی اینستاگرام: {'✅' if 'instagram' in getattr(callback, '_cookies', {}) else '❌'}\n"
            f"• کلید HikerAPI: {'✅' if os.getenv('HIKERAPI_KEY') else '❌'}\n"
            f"• محدودیت حجم: {os.getenv('MAX_FILE_SIZE_MB', '50')}MB\n"
        )
        await callback.message.edit_text(status_text, reply_markup=admin_back_keyboard())

    elif action == "back":
        admin_states.pop(callback.from_user.id, None)
        await callback.message.edit_text(
            "🛠 پنل مدیریت ربات", 
            reply_markup=admin_main_keyboard()
        )


# ============================== اجرا ==============================

async def on_startup(bot: Bot, config: Config) -> None:
    """عملیات startup ربات"""
    logger.info("=" * 50)
    logger.info("🚀 ربات در حال راه‌اندازی...")
    logger.info(f"👥 ادمین‌ها: {config.admin_ids}")
    logger.info(f"📁 مسیر دیتابیس: {config.db_path}")
    logger.info(f"📦 مسیر دانلود: {config.download_dir}")
    logger.info(f"📏 حداکثر حجم فایل: {config.max_file_size_mb}MB")
    logger.info("=" * 50)

    # ست کردن webhook info (اختیاری)
    try:
        bot_info = await bot.get_me()
        logger.info(f"🤖 ربات @{bot_info.username} آماده به کاره")
    except Exception as e:
        logger.warning(f"نتونست اطلاعات ربات رو بگیره: {e}")


async def on_shutdown(bot: Bot, config: Config) -> None:
    """عملیات shutdown ربات"""
    logger.info("🛑 ربات در حال خاموش شدن...")
    
    # پاکسازی فایل‌های موقت
    try:
        shutil.rmtree(config.download_dir, ignore_errors=True)
        logger.info("🗑 فایل‌های موقت پاکسازی شدن")
    except Exception as e:
        logger.warning(f"خطا در پاکسازی فایل‌های موقت: {e}")
    
    # بستن session ربات
    await bot.session.close()
    logger.info("✅ ربات خاموش شد")


async def main() -> None:
    """تابع اصلی اجرای ربات"""
    global ADMIN_IDS, DB_PATH

    # بارگذاری تنظیمات
    config = load_config()
    ADMIN_IDS = config.admin_ids
    DB_PATH = config.db_path

    # آماده‌سازی محیط
    os.makedirs(config.download_dir, exist_ok=True)
    
    # پاکسازی فایل‌های قبلی
    try:
        shutil.rmtree(config.download_dir, ignore_errors=True)
        os.makedirs(config.download_dir, exist_ok=True)
    except Exception as e:
        logger.error(f"خطا در پاکسازی دایرکتوری دانلود: {e}")

    # آماده‌سازی دیتابیس
    db_init()

    # آماده‌سازی کوکی‌ها
    cookies_files = prepare_cookie_files(config.cookies_b64)

    # ایجاد نمونه‌های اصلی
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    downloader = Downloader(
        config.download_dir,
        config.max_file_size_mb,
        cookies_files,
        hikerapi_key=config.hikerapi_key,
        reddit_client_id=config.reddit_client_id,
        reddit_client_secret=config.reddit_client_secret,
        reddit_user_agent=config.reddit_user_agent,
    )
    limiter = DownloadLimiter(config.max_concurrent_downloads)

    # تزریق وابستگی‌ها به router
    dp["downloader"] = downloader
    dp["limiter"] = limiter
    dp["config"] = config

    # ثبت میدلورها
    dp.message.middleware(ThrottlingMiddleware(config.rate_limit_seconds))
    
    # ثبت router
    dp.include_router(router)

    # حذف webhook قبلی
    await bot.delete_webhook(drop_pending_updates=True)

    # اجرای startup
    await on_startup(bot, config)

    # مدیریت graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def signal_handler():
        logger.info("دریافت سیگنال توقف...")
        stop_event.set()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows از add_signal_handler پشتیبانی نمی‌کنه
            pass

    logger.info("🚀 ربات شروع به کار کرد...")
    
    try:
        # شروع polling
        polling_task = asyncio.create_task(dp.start_polling(bot))
        
        # منتظر سیگنال توقف باش
        await stop_event.wait()
        
        # لغو polling
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        
    except Exception as e:
        logger.critical(f"❌ خطای بحرانی: {e}")
    finally:
        await on_shutdown(bot, config)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("ربات با Ctrl+C متوقف شد")
    except Exception as e:
        logger.critical(f"خطای اجرا: {e}", exc_info=True)
