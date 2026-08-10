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
"""
import os
import re
import json
import time
import uuid
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
from typing import Any, Awaitable, Callable, Dict

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # محدودیت تلگرام برای کپشن عکس/ویدیو

# ---- حالت‌های مشترک درون‌حافظه‌ای (برای یه پروسه‌ی تک‌نمونه‌ای کافیه) ----
ADMIN_IDS: set[int] = set(7714450221)
admin_states: Dict[int, str] = {7714450221}
pending_youtube: Dict[str, dict] = {7714450221}


# ============================== تنظیمات ==============================

@dataclass(frozen=True)
class Config:
    bot_token: str
    max_file_size_mb: int
    download_dir: str
    max_concurrent_downloads: int
    rate_limit_seconds: float
    cookies_b64: Dict[str, str] = field(default_factory=dict)
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str | None = None
    hikerapi_key: str | None = None
    admin_ids: set[int] = field(default_factory=set)
    db_path: str = "bot_data.db"


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _get_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            ids.add(int(part))
    return ids


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError(
            "متغیر محیطی BOT_TOKEN تنظیم نشده. "
            "توکن رو از @BotFather بگیر و توی Variables پروژه‌ی Railway ست کن."
        )

    cookies_b64 = {}
    for platform, env_name in (
        ("instagram", "INSTAGRAM_COOKIES_B64"),
        ("youtube", "YOUTUBE_COOKIES_B64"),
        ("twitter", "TWITTER_COOKIES_B64"),
    ):
        value = os.getenv(env_name)
        if value:
            cookies_b64[platform] = value

    return Config(
        bot_token=bot_token,
        max_file_size_mb=_get_int("MAX_FILE_SIZE_MB", 50),
        download_dir=os.getenv("DOWNLOAD_DIR", "downloads"),
        max_concurrent_downloads=_get_int("MAX_CONCURRENT_DOWNLOADS", 3),
        rate_limit_seconds=_get_float("RATE_LIMIT_SECONDS", 3.0),
        cookies_b64=cookies_b64,
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID"),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT"),
        hikerapi_key=os.getenv("HIKERAPI_KEY"),
        admin_ids=_get_admin_ids(),
        db_path=os.getenv("DB_PATH", "bot_data.db"),
    )


def prepare_cookie_files(cookies_b64: Dict[str, str]) -> Dict[str, str]:
    files: Dict[str, str] = {}
    for platform, b64_value in cookies_b64.items():
        try:
            raw = base64.b64decode(b64_value)
            path = f"{platform}_cookies.txt"
            with open(path, "wb") as f:
                f.write(raw)
            files[platform] = path
            logger.info(f"کوکی {platform} با موفقیت بارگذاری شد")
        except Exception:
            logger.exception(f"دیکد کردن کوکی {platform} ناموفق بود")
    return files


# ============================== دیتابیس ==============================
# SQLite ساده برای آمار و کانال‌های عضویت اجباری.
# نکته: روی Railway بدون Volume، این فایل با هر ردیپلوی پاک می‌شه.

DB_PATH = "bot_data.db"


def db_init() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_seen TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS downloads "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, platform TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS force_sub_channels "
        "(chat_id TEXT PRIMARY KEY, title TEXT, invite_link TEXT, added_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS channel_verifications "
        "(user_id INTEGER, chat_id TEXT, verified_at TEXT, PRIMARY KEY(user_id, chat_id))"
    )
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_record_user(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)", (user_id, _now()))
    conn.commit()
    conn.close()


def db_record_download(user_id: int, platform: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO downloads (user_id, platform, created_at) VALUES (?, ?, ?)",
        (user_id, platform, _now()),
    )
    conn.commit()
    conn.close()


def db_get_stats() -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    downloads = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    conn.close()
    return users, downloads


def db_add_channel(chat_id: str, title: str, invite_link: str | None) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO force_sub_channels (chat_id, title, invite_link, added_at) VALUES (?, ?, ?, ?)",
        (chat_id, title, invite_link, _now()),
    )
    conn.commit()
    conn.close()


def db_remove_channel(chat_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM force_sub_channels WHERE chat_id = ?", (chat_id,))
    conn.execute("DELETE FROM channel_verifications WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def db_list_channels() -> list[tuple[str, str, str | None]]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT chat_id, title, invite_link FROM force_sub_channels").fetchall()
    conn.close()
    return rows


def db_record_verification(user_id: int, chat_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO channel_verifications (user_id, chat_id, verified_at) VALUES (?, ?, ?)",
        (user_id, chat_id, _now()),
    )
    conn.commit()
    conn.close()


def db_channel_verified_count(chat_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM channel_verifications WHERE chat_id = ?", (chat_id,)
    ).fetchone()[0]
    conn.close()
    return count


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


def extract_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def detect_platform(url: str) -> str | None:
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return platform
    return None


def is_active(platform: str) -> bool:
    return platform in ACTIVE_PLATFORMS


def build_caption(text: str | None) -> str:
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
    )
}
_OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"', re.IGNORECASE)
_OG_DESC_RE = re.compile(r'<meta\s+property="og:description"\s+content="([^"]+)"', re.IGNORECASE)
_PWS_DATA_RE = re.compile(
    r'<script id="__PWS_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)

HIKERAPI_BASE = "https://api.hikerapi.com"

MediaItem = tuple[str, str]  # (filepath, media_type)
DownloadResult = tuple[list[MediaItem], str | None]  # (آیتم‌ها, کپشن)

_DESCRIPTION_KEYS = ("description", "content", "caption", "title", "alt_text")


class Downloader:
    def __init__(
        self,
        download_dir: str,
        max_file_size_mb: int,
        cookies_files: Dict[str, str] | None = None,
        hikerapi_key: str | None = None,
        reddit_client_id: str | None = None,
        reddit_client_secret: str | None = None,
        reddit_user_agent: str | None = None,
    ):
        self.download_dir = download_dir
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.cookies_files = cookies_files or {}
        self.hikerapi_key = hikerapi_key
        self.reddit_client_id = reddit_client_id
        self.reddit_client_secret = reddit_client_secret
        self.reddit_user_agent = reddit_user_agent
        os.makedirs(download_dir, exist_ok=True)

    # ---------- کمکی‌ها ----------

    def _find_downloaded_file(self, file_id: str) -> str | None:
        for fname in os.listdir(self.download_dir):
            if fname.startswith(file_id):
                return os.path.join(self.download_dir, fname)
        return None

    @staticmethod
    def _friendly_message(raw_error: str) -> str:
        lowered = raw_error.lower()
        if "private" in lowered:
            return "این پست خصوصیه و قابل دانلود نیست."
        if "not available" in lowered or "unavailable" in lowered:
            return "این محتوا در دسترس نیست یا حذف شده."
        if "sign in" in lowered or "login" in lowered:
            return "این پلتفرم برای این لینک نیاز به لاگین داره."
        return "لینک قابل پردازش نیست؛ لطفا لینک رو چک کن."

    @staticmethod
    def _guess_media_type(path_or_url: str) -> str:
        lowered = path_or_url.lower().split("?")[0]
        if lowered.endswith((".mp4", ".mov", ".webm", ".mkv")):
            return "video"
        return "photo"

    def _save_stream(self, resp: requests.Response, filepath: str) -> bool:
        total = 0
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                total += len(chunk)
                if total > self.max_file_size:
                    f.close()
                    os.remove(filepath)
                    return False
                f.write(chunk)
        if total == 0:
            os.remove(filepath)
            return False
        return True

    # ---------- لایه‌ی ویژه‌ی اینستاگرام: HikerAPI ----------

    def _try_hikerapi(self, url: str, file_id: str) -> DownloadResult:
        if not self.hikerapi_key:
            return [], None
        try:
            resp = requests.get(
                f"{HIKERAPI_BASE}/v2/media/info/by/url",
                params={"url": url},
                headers={"x-access-key": self.hikerapi_key},
                timeout=20,
            )
        except requests.RequestException:
            return [], None
        if resp.status_code != 200:
            return [], None
        try:
            data = resp.json()
        except ValueError:
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
            except requests.RequestException:
                continue
            if self._save_stream(media_resp, filepath):
                results.append((filepath, media_type))

        return results, caption

    # ---------- لایه‌ی ویژه‌ی پینترست: استخراج مستقیم JSON صفحه ----------

    def _try_pinterest_native(self, url: str, file_id: str) -> DownloadResult:
        try:
            resp = requests.get(url, headers=_HTTP_HEADERS, timeout=15, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException:
            return [], None

        match = _PWS_DATA_RE.search(resp.text)
        if not match:
            return [], None

        try:
            data = json.loads(match.group(1))
            pins = data["props"]["initialReduxState"]["pins"]
            pin_id = next(iter(pins))
            pin = pins[pin_id]
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError):
            return [], None

        media_type = "photo"
        media_url = (pin.get("videos") or {}).get("video_list", {}).get("V_720P", {}).get("url")
        if media_url:
            media_type = "video"
        else:
            media_url = (pin.get("images") or {}).get("orig", {}).get("url")

        if not media_url:
            return [], None

        caption = pin.get("description") or pin.get("grid_title") or None

        ext = ".mp4" if media_type == "video" else ".jpg"
        filepath = os.path.join(self.download_dir, f"{file_id}{ext}")
        try:
            media_resp = requests.get(media_url, headers=_HTTP_HEADERS, timeout=30, stream=True)
            media_resp.raise_for_status()
        except requests.RequestException:
            return [], None

        if self._save_stream(media_resp, filepath):
            return [(filepath, media_type)], caption
        return [], None

    # ---------- لایه‌ی عمومی: yt-dlp (بالاترین کیفیت ممکن) ----------

    def _ytdlp_opts(self, platform: str, output_path: str) -> dict:
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
            # بدون سقف کیفیت مصنوعی - بهترین ویدیو + بهترین صدا، مرج با ffmpeg
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
        }
        if platform in self.cookies_files:
            opts["cookiefile"] = self.cookies_files[platform]
        return opts

    def _try_ytdlp(self, url: str, platform: str, file_id: str) -> DownloadResult:
        output_template = os.path.join(self.download_dir, f"{file_id}.%(ext)s")
        ydl_opts = self._ytdlp_opts(platform, output_template)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
                raise DownloadError(f"حجم فایل بیشتر از {self.max_file_size // (1024 * 1024)} مگابایته.")

            media_type = self._guess_media_type(filepath)

            title = (info.get("title") or "").strip()
            description = (info.get("description") or "").strip()
            caption_parts = [p for p in (title, description) if p]
            caption = "\n\n".join(caption_parts) if caption_parts else None

            return [(filepath, media_type)], caption

    # ---------- لایه‌ی gallery-dl ----------

    def _reddit_extra_opts(self) -> list[str]:
        if not (self.reddit_client_id and self.reddit_client_secret):
            return []
        return [
            "-o", f"extractor.reddit.client-id={self.reddit_client_id}",
            "-o", f"extractor.reddit.client-secret={self.reddit_client_secret}",
            "-o", f"extractor.reddit.user-agent={self.reddit_user_agent or 'megasaver-bot/1.0'}",
        ]

    def _run_gallery_dl_json(self, url: str, platform: str) -> list | None:
        cmd = ["gallery-dl", "-j", "--no-download"]
        if platform in self.cookies_files:
            cmd += ["--cookies", self.cookies_files[platform]]
        if platform == "reddit":
            cmd += self._reddit_extra_opts()
        cmd.append(url)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def _run_gallery_dl_urls_only(self, url: str, platform: str) -> list[str]:
        cmd = ["gallery-dl", "-g", "--no-download"]
        if platform in self.cookies_files:
            cmd += ["--cookies", self.cookies_files[platform]]
        if platform == "reddit":
            cmd += self._reddit_extra_opts()
        cmd.append(url)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("http")][:10]

    def _extract_gallery_dl_caption(self, entries: list) -> str | None:
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
        media_urls: list[str] = []
        caption: str | None = None

        entries = self._run_gallery_dl_json(url, platform)
        if entries:
            caption = self._extract_gallery_dl_caption(entries)
            for entry in entries:
                if isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[1], str) and entry[1].startswith("http"):
                    media_urls.append(entry[1])
            media_urls = media_urls[:10]

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
            except requests.RequestException:
                continue
            if self._save_stream(resp, filepath):
                results.append((filepath, self._guess_media_type(media_url)))

        return results, caption

    # ---------- لایه‌ی آخر: og:image / og:description ----------

    def _try_og_fallback(self, url: str, file_id: str) -> DownloadResult:
        try:
            page = requests.get(url, headers=_HTTP_HEADERS, timeout=15)
            page.raise_for_status()
        except requests.RequestException:
            return [], None

        img_match = _OG_IMAGE_RE.search(page.text)
        if not img_match:
            return [], None

        desc_match = _OG_DESC_RE.search(page.text)
        caption = desc_match.group(1).replace("&amp;", "&") if desc_match else None

        image_url = img_match.group(1).replace("&amp;", "&")
        try:
            resp = requests.get(image_url, headers=_HTTP_HEADERS, timeout=20, stream=True)
            resp.raise_for_status()
        except requests.RequestException:
            return [], None

        content_type = resp.headers.get("Content-Type", "")
        ext = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
        filepath = os.path.join(self.download_dir, f"{file_id}{ext}")

        if self._save_stream(resp, filepath):
            return [(filepath, "photo")], caption
        return [], None

    # ---------- نقطه‌ی ورود عمومی (همه پلتفرم‌ها به‌جز یوتیوب) ----------

    def _download_sync(self, url: str, platform: str) -> DownloadResult:
        file_id = str(uuid.uuid4())
        last_error = "لینک قابل پردازش نیست."

        if platform == "instagram" and self.hikerapi_key:
            try:
                items, caption = self._try_hikerapi(url, file_id)
                if items:
                    return items, caption
            except Exception:
                logger.exception("خطای HikerAPI")

        if platform == "pinterest":
            try:
                items, caption = self._try_pinterest_native(url, file_id)
                if items:
                    return items, caption
            except Exception:
                logger.exception("خطای استخراج مستقیم پینترست")

        try:
            return self._try_ytdlp(url, platform, file_id)
        except yt_dlp.utils.DownloadError as e:
            last_error = str(e)
        except DownloadError as e:
            last_error = str(e)
        except Exception:
            logger.exception("خطای غیرمنتظره در yt-dlp")
            last_error = "خطای داخلی"

        if platform in GALLERY_DL_PLATFORMS:
            try:
                items, caption = self._try_gallery_dl(url, platform, file_id)
                if items:
                    return items, caption
            except Exception:
                logger.exception("خطای غیرمنتظره در gallery-dl")

        if platform in GALLERY_DL_PLATFORMS:
            items, caption = self._try_og_fallback(url, file_id)
            if items:
                return items, caption

        raise DownloadError(self._friendly_message(last_error))

    async def download(self, url: str, platform: str) -> DownloadResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, url, platform)

    # ---------- مسیر ویژه‌ی یوتیوب: انتخاب کیفیت ----------

    def _youtube_base_opts(self) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
        }
        if "youtube" in self.cookies_files:
            opts["cookiefile"] = self.cookies_files["youtube"]
        return opts

    def list_youtube_qualities(self, url: str) -> tuple[list[dict], str | None]:
        opts = self._youtube_base_opts()
        opts["skip_download"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats", []) or []
        heights = sorted({f.get("height") for f in formats if f.get("height")}, reverse=True)

        results = []
        for h in heights:
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

        return results[:8], info.get("title")

    def _download_youtube_sync(self, url: str, height: int) -> DownloadResult:
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

                return [(filepath, "video")], caption
        except yt_dlp.utils.DownloadError as e:
            raise DownloadError(self._friendly_message(str(e)))

    async def download_youtube(self, url: str, height: int) -> DownloadResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_youtube_sync, url, height)

    @staticmethod
    def cleanup(items: list[MediaItem]) -> None:
        for filepath, _ in items:
            try:
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
            except OSError as e:
                logger.warning(f"پاک کردن فایل {filepath} ناموفق بود: {e}")


# ===================== محدودکننده‌ی دانلود همزمان =====================

class DownloadLimiter:
    def __init__(self, max_concurrent: int):
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def __aenter__(self):
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._semaphore.release()


# ========================= میدلور ضد اسپم =========================

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit_seconds: float):
        self.rate_limit_seconds = rate_limit_seconds
        self._last_request: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id if event.from_user else None
        if user_id is not None and user_id not in ADMIN_IDS:
            now = time.monotonic()
            last = self._last_request.get(user_id)
            if last is not None and (now - last) < self.rate_limit_seconds:
                await event.answer("لطفا کمی صبر کن و دوباره امتحان کن ⏳")
                return
            self._last_request[user_id] = now
        return await handler(event, data)


# ========================= عضویت اجباری =========================

async def get_unjoined_channels(bot: Bot, user_id: int) -> list[tuple[str, str, str | None]]:
    channels = db_list_channels()
    unjoined = []
    for chat_id, title, invite_link in channels:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in ("left", "kicked"):
                unjoined.append((chat_id, title, invite_link))
        except Exception:
            logger.warning(f"چک عضویت کانال {chat_id} ناموفق بود (احتمالا بات ادمین نیست)")
            continue
    return unjoined


def build_membership_keyboard(unjoined: list[tuple[str, str, str | None]]) -> InlineKeyboardMarkup:
    rows = []
    for chat_id, title, invite_link in unjoined:
        url = invite_link
        if not url and isinstance(chat_id, str) and chat_id.startswith("@"):
            url = f"https://t.me/{chat_id.lstrip('@')}"
        if url:
            rows.append([InlineKeyboardButton(text=f"عضویت در {title}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ عضو شدم", callback_data="checksub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================== هندلرهای عمومی ==============================

router = Router(name="main")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    db_record_user(message.from_user.id)
    await message.answer(
        "سلام! 👋\n\n"
        "لینک پست/ریلز اینستاگرام، توییتر(X)، یوتیوب، پینترست، تیک‌تاک یا ردیت رو بفرست "
        "تا برات دانلودش کنم — همراه با کپشن/توضیحاتش.\n\n"
        "پست‌های چندعکسی (کاروسل) هم پشتیبانی می‌شن."
    )


async def _send_media(message: Message, items: list[MediaItem], caption: str | None) -> None:
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
                file = FSInputFile(filepath)
                await message.answer_document(document=file, caption=final_caption)
        return

    media_group = []
    for idx, (filepath, media_type) in enumerate(items):
        file = FSInputFile(filepath)
        item_caption = final_caption if idx == 0 else None
        if media_type == "photo":
            media_group.append(InputMediaPhoto(media=file, caption=item_caption))
        else:
            media_group.append(InputMediaVideo(media=file, caption=item_caption))

    await message.answer_media_group(media=media_group)


async def handle_youtube_link(message: Message, downloader: Downloader, url: str) -> None:
    status_msg = await message.answer("⏳ در حال بررسی کیفیت‌های موجود...")
    try:
        loop = asyncio.get_running_loop()
        qualities, title = await loop.run_in_executor(None, downloader.list_youtube_qualities, url)
    except Exception:
        logger.exception("خطا در گرفتن کیفیت‌های یوتیوب")
        await status_msg.edit_text("❌ نتونستم اطلاعات این ویدیو رو بگیرم (احتمالا یوتیوب سرور رو بلاک کرده).")
        return

    if not qualities:
        await status_msg.edit_text("❌ کیفیتی برای این ویدیو پیدا نشد.")
        return

    short_id = secrets.token_hex(4)
    pending_youtube[short_id] = {"url": url, "title": title}

    rows = []
    for q in qualities:
        size_txt = f" (~{q['approx_size_mb']}MB)" if q["approx_size_mb"] else ""
        rows.append([InlineKeyboardButton(
            text=f"🎬 {q['height']}p{size_txt}",
            callback_data=f"ytq:{short_id}:{q['height']}",
        )])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    header = f"{title}\n\n" if title else ""
    await status_msg.edit_text(f"{header}کیفیت مورد نظر رو انتخاب کن:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("ytq:"))
async def cb_youtube_quality(callback: CallbackQuery, downloader: Downloader, limiter: DownloadLimiter) -> None:
    try:
        _, short_id, height_str = callback.data.split(":")
        height = int(height_str)
    except (ValueError, AttributeError):
        await callback.answer("درخواست نامعتبره.", show_alert=True)
        return

    entry = pending_youtube.get(short_id)
    if not entry:
        await callback.answer("این درخواست منقضی شده، لینک رو دوباره بفرست.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(f"⏳ در حال دانلود با کیفیت {height}p...")

    items: list[MediaItem] = []
    try:
        async with limiter:
            items, caption = await downloader.download_youtube(entry["url"], height)
        await _send_media(callback.message, items, caption or entry.get("title"))
        db_record_download(callback.from_user.id, "youtube")
        await callback.message.delete()
    except DownloadError as e:
        await callback.message.edit_text(f"❌ {e}")
    except Exception:
        logger.exception("خطای غیرمنتظره در دانلود یوتیوب")
        await callback.message.edit_text("❌ یه مشکلی پیش اومد.")
    finally:
        if items:
            Downloader.cleanup(items)
        pending_youtube.pop(short_id, None)


@router.callback_query(F.data == "checksub")
async def cb_checksub(callback: CallbackQuery) -> None:
    unjoined = await get_unjoined_channels(callback.bot, callback.from_user.id)
    if unjoined:
        await callback.answer("هنوز عضو همه‌ی کانال‌ها نشدی.", show_alert=True)
        return
    for chat_id, _title, _link in db_list_channels():
        db_record_verification(callback.from_user.id, chat_id)
    await callback.answer("عضویت تایید شد ✅", show_alert=True)
    await callback.message.edit_text("✅ عضویت تایید شد! حالا لینکت رو بفرست.")


@router.message(F.text)
async def handle_link(message: Message, downloader: Downloader, limiter: DownloadLimiter) -> None:
    user_id = message.from_user.id
    db_record_user(user_id)

    text = message.text or ""

    if user_id in ADMIN_IDS and admin_states.get(user_id) == "awaiting_add_channel":
        await _process_add_channel(message)
        return

    if user_id not in ADMIN_IDS:
        unjoined = await get_unjoined_channels(message.bot, user_id)
        if unjoined:
            await message.answer(
                "برای استفاده از ربات باید عضو کانال‌های زیر بشی 👇",
                reply_markup=build_membership_keyboard(unjoined),
            )
            return

    url = extract_url(text)
    if not url:
        await message.answer("لینک معتبری پیدا نکردم. لطفا یه لینک از پلتفرم‌های پشتیبانی‌شده بفرست.")
        return

    platform = detect_platform(url)
    if platform is None:
        await message.answer("این لینک رو نشناختم.")
        return

    if not is_active(platform):
        await message.answer(f"پشتیبانی از {platform} هنوز فعال نشده.")
        return

    if platform == "youtube":
        await handle_youtube_link(message, downloader, url)
        return

    status_msg = await message.answer("⏳ در حال دانلود...")

    items: list[MediaItem] = []
    try:
        async with limiter:
            items, caption = await downloader.download(url, platform)

        await _send_media(message, items, caption)
        db_record_download(user_id, platform)
        await status_msg.delete()

    except DownloadError as e:
        await status_msg.edit_text(f"❌ {e}")
    except Exception:
        logger.exception("خطای غیرمنتظره در پردازش لینک")
        await status_msg.edit_text("❌ یه مشکلی پیش اومد، دوباره امتحان کن.")
    finally:
        if items:
            Downloader.cleanup(items)


# ============================== پنل ادمین ==============================

def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار کلی", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📋 لیست کانال‌ها", callback_data="admin:list")],
        [InlineKeyboardButton(text="➕ افزودن کانال", callback_data="admin:add")],
        [InlineKeyboardButton(text="➖ حذف کانال", callback_data="admin:remove")],
    ])


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ بازگشت", callback_data="admin:back")]]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    admin_states.pop(message.from_user.id, None)
    await message.answer("🛠 پنل مدیریت ربات", reply_markup=admin_main_keyboard())


async def _process_add_channel(message: Message) -> None:
    admin_states.pop(message.from_user.id, None)
    raw = (message.text or "").strip()

    try:
        chat = await message.bot.get_chat(raw)
    except Exception:
        await message.answer(
            "❌ نتونستم این کانال رو پیدا کنم. مطمئن شو آیدی/یوزرنیم درسته "
            "و ربات از قبل به‌عنوان ادمین به کانال اضافه شده.",
            reply_markup=admin_back_keyboard(),
        )
        return

    try:
        me = await message.bot.me()
        member = await message.bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await message.answer(
                "⚠️ ربات توی این کانال ادمین نیست. اول ادمینش کن، بعد دوباره امتحان کن.",
                reply_markup=admin_back_keyboard(),
            )
            return
    except Exception:
        await message.answer(
            "⚠️ نتونستم وضعیت ادمین‌بودن ربات رو توی این کانال چک کنم. "
            "مطمئن شو ربات عضو/ادمین کانال هست.",
            reply_markup=admin_back_keyboard(),
        )
        return

    title = chat.title or raw
    invite_link = None
    if chat.username:
        invite_link = f"https://t.me/{chat.username}"
    else:
        try:
            invite_link = await message.bot.export_chat_invite_link(chat.id)
        except Exception:
            invite_link = None

    db_add_channel(str(chat.id), title, invite_link)
    await message.answer(f"✅ کانال «{title}» به لیست عضویت اجباری اضافه شد.", reply_markup=admin_back_keyboard())


@router.callback_query(F.data.startswith("admin:"))
async def cb_admin(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی نداری.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "stats":
        users, downloads = db_get_stats()
        lines = [f"👥 تعداد کاربران: {users}", f"⬇️ محتوای دانلودشده: {downloads}", "", "📢 آمار عضویت اجباری:"]
        channels = db_list_channels()
        if not channels:
            lines.append("کانالی تعریف نشده.")
        for chat_id, title, _link in channels:
            verified = db_channel_verified_count(chat_id)
            try:
                total = await callback.bot.get_chat_member_count(chat_id)
            except Exception:
                total = "؟"
            lines.append(f"• {title}: {verified} نفر از طریق ربات تایید شدن (کل اعضای کانال: {total})")
        await callback.message.edit_text("\n".join(lines), reply_markup=admin_back_keyboard())

    elif action == "list":
        channels = db_list_channels()
        if not channels:
            await callback.message.edit_text("هیچ کانالی ثبت نشده.", reply_markup=admin_back_keyboard())
        else:
            text = "📋 کانال‌های عضویت اجباری:\n" + "\n".join(f"• {t} ({c})" for c, t, _l in channels)
            await callback.message.edit_text(text, reply_markup=admin_back_keyboard())

    elif action == "add":
        admin_states[callback.from_user.id] = "awaiting_add_channel"
        await callback.message.edit_text(
            "آیدی عددی کانال (مثلا -1001234567890) یا یوزرنیم (@channel) رو به‌صورت پیام بفرست.\n\n"
            "⚠️ قبلش حتما ربات رو به‌عنوان ادمین به اون کانال اضافه کن.",
            reply_markup=admin_back_keyboard(),
        )

    elif action == "remove":
        channels = db_list_channels()
        if not channels:
            await callback.message.edit_text("کانالی برای حذف نیست.", reply_markup=admin_back_keyboard())
        else:
            rows = [
                [InlineKeyboardButton(text=title, callback_data=f"admin:rm:{chat_id}")]
                for chat_id, title, _link in channels
            ]
            rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="admin:back")])
            await callback.message.edit_text("کدوم کانال حذف بشه؟", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    elif action.startswith("rm:"):
        chat_id = action.split(":", 1)[1]
        db_remove_channel(chat_id)
        await callback.message.edit_text("✅ حذف شد.", reply_markup=admin_back_keyboard())

    elif action == "back":
        admin_states.pop(callback.from_user.id, None)
        await callback.message.edit_text("🛠 پنل مدیریت ربات", reply_markup=admin_main_keyboard())


# ============================== اجرا ==============================

async def main() -> None:
    global ADMIN_IDS, DB_PATH

    config = load_config()
    ADMIN_IDS = config.admin_ids
    DB_PATH = config.db_path

    shutil.rmtree(config.download_dir, ignore_errors=True)
    db_init()

    cookies_files = prepare_cookie_files(config.cookies_b64)

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
    dp["downloader"] = downloader
    dp["limiter"] = limiter

    dp.message.middleware(ThrottlingMiddleware(config.rate_limit_seconds))
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)

    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS تنظیم نشده - پنل مدیریت غیرفعاله.")

    logger.info("ربات در حال اجراست...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
