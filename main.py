"""
ربات دانلودر تلگرام - اینستاگرام / توییتر(X) / یوتیوب / پینترست / تیک‌تاک
همه‌ی کد عمداً تو یه فایله تا مدیریت و آپلودش روی گیت‌هاب ساده باشه.

استراتژی دانلود سه‌لایه (برای هر لینک):
  ۱. yt-dlp        -> ویدیو/ریلز + کپشن (description)
  ۲. gallery-dl -j  -> عکس/کاروسل + متادیتا (وقتی yt-dlp جواب نداد)
  ۳. og:image/og:description از صفحه -> آخرین راه‌حل
"""
import os
import re
import json
import time
import uuid
import shutil
import base64
import asyncio
import logging
import subprocess
from html import escape as html_escape
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict

import requests
import yt_dlp
from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaVideo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # محدودیت تلگرام برای کپشن عکس/ویدیو


# ============================== تنظیمات ==============================

@dataclass(frozen=True)
class Config:
    bot_token: str
    max_file_size_mb: int
    download_dir: str
    max_concurrent_downloads: int
    rate_limit_seconds: float
    cookies_b64: Dict[str, str] = field(default_factory=dict)


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


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
    )


def prepare_cookie_files(cookies_b64: Dict[str, str]) -> Dict[str, str]:
    """
    کوکی هر پلتفرم (base64 توی متغیر محیطی) رو دیکد و روی دیسک ذخیره می‌کنه
    تا yt-dlp و gallery-dl بتونن ازش استفاده کنن.
    """
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


# ========================== تشخیص پلتفرم ==========================

ACTIVE_PLATFORMS = {"instagram", "pinterest", "tiktok", "twitter", "youtube"}
GALLERY_DL_PLATFORMS = {"instagram", "pinterest", "twitter", "tiktok"}

PLATFORM_PATTERNS: dict[str, re.Pattern] = {
    "instagram": re.compile(r"(instagram\.com|instagr\.am)", re.IGNORECASE),
    "pinterest": re.compile(r"(pinterest\.[a-z.]+|pin\.it)", re.IGNORECASE),
    "tiktok": re.compile(r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)", re.IGNORECASE),
    "twitter": re.compile(r"(twitter\.com|x\.com)", re.IGNORECASE),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE),
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
    """کپشن رو برای تلگرام امن (escape شده) و کوتاه‌شده به ۱۰۲۴ کاراکتر می‌سازه"""
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

MediaItem = tuple[str, str]  # (filepath, media_type)
DownloadResult = tuple[list[MediaItem], str | None]  # (آیتم‌ها, کپشن)

_DESCRIPTION_KEYS = ("description", "content", "caption", "title", "alt_text")


class Downloader:
    def __init__(self, download_dir: str, max_file_size_mb: int, cookies_files: Dict[str, str] | None = None):
        self.download_dir = download_dir
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.cookies_files = cookies_files or {}
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

    # ---------- لایه‌ی ۱: yt-dlp ----------

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
            "format": "best[ext=mp4]/best",
        }
        if platform == "youtube":
            opts["extractor_args"] = {"youtube": {"player_client": ["android", "ios"]}}
        if platform in self.cookies_files:
            opts["cookiefile"] = self.cookies_files[platform]
        return opts

    def _try_ytdlp(self, url: str, platform: str, file_id: str) -> DownloadResult:
        output_template = os.path.join(self.download_dir, f"{file_id}.%(ext)s")
        ydl_opts = self._ytdlp_opts(platform, output_template)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)

            if not os.path.exists(filepath):
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

    # ---------- لایه‌ی ۲: gallery-dl ----------

    def _run_gallery_dl_json(self, url: str, platform: str) -> list | None:
        cmd = ["gallery-dl", "-j", "--no-download"]
        if platform in self.cookies_files:
            cmd += ["--cookies", self.cookies_files[platform]]
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

    # ---------- لایه‌ی ۳: og:image / og:description ----------

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

    # ---------- نقطه‌ی ورود ----------

    def _download_sync(self, url: str, platform: str) -> DownloadResult:
        file_id = str(uuid.uuid4())
        last_error = "لینک قابل پردازش نیست."

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
        if user_id is not None:
            now = time.monotonic()
            last = self._last_request.get(user_id)
            if last is not None and (now - last) < self.rate_limit_seconds:
                await event.answer("لطفا کمی صبر کن و دوباره امتحان کن ⏳")
                return
            self._last_request[user_id] = now
        return await handler(event, data)


# ============================== هندلرها ==============================

router = Router(name="main")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "سلام! 👋\n\n"
        "لینک پست/ریلز اینستاگرام، توییتر(X)، یوتیوب، پینترست یا تیک‌تاک رو بفرست "
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


@router.message(F.text)
async def handle_link(message: Message, downloader: Downloader, limiter: DownloadLimiter) -> None:
    text = message.text or ""
    url = extract_url(text)

    if not url:
        await message.answer("لینک معتبری پیدا نکردم. لطفا یه لینک از اینستاگرام، توییتر، یوتیوب، پینترست یا تیک‌تاک بفرست.")
        return

    platform = detect_platform(url)

    if platform is None:
        await message.answer("این لینک رو نشناختم.")
        return

    if not is_active(platform):
        await message.answer(f"پشتیبانی از {platform} هنوز فعال نشده.")
        return

    status_msg = await message.answer("⏳ در حال دانلود...")

    items: list[MediaItem] = []
    try:
        async with limiter:
            items, caption = await downloader.download(url, platform)

        await _send_media(message, items, caption)
        await status_msg.delete()

    except DownloadError as e:
        await status_msg.edit_text(f"❌ {e}")
    except Exception:
        logger.exception("خطای غیرمنتظره در پردازش لینک")
        await status_msg.edit_text("❌ یه مشکلی پیش اومد، دوباره امتحان کن.")
    finally:
        if items:
            Downloader.cleanup(items)


# ============================== اجرا ==============================

async def main() -> None:
    config = load_config()

    shutil.rmtree(config.download_dir, ignore_errors=True)

    cookies_files = prepare_cookie_files(config.cookies_b64)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    downloader = Downloader(config.download_dir, config.max_file_size_mb, cookies_files)
    limiter = DownloadLimiter(config.max_concurrent_downloads)
    dp["downloader"] = downloader
    dp["limiter"] = limiter

    dp.message.middleware(ThrottlingMiddleware(config.rate_limit_seconds))
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("ربات در حال اجراست...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
