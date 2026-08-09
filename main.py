"""
ربات دانلودر تلگرام - اینستاگرام / پینترست / تیک‌تاک
همه‌ی کد عمداً تو یه فایله تا مدیریت و آپلودش روی گیت‌هاب ساده باشه.
"""
import os
import re
import time
import uuid
import shutil
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

import yt_dlp
from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ============================== تنظیمات ==============================

@dataclass(frozen=True)
class Config:
    bot_token: str
    max_file_size_mb: int
    download_dir: str
    max_concurrent_downloads: int
    rate_limit_seconds: float


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
    return Config(
        bot_token=bot_token,
        max_file_size_mb=_get_int("MAX_FILE_SIZE_MB", 50),
        download_dir=os.getenv("DOWNLOAD_DIR", "downloads"),
        max_concurrent_downloads=_get_int("MAX_CONCURRENT_DOWNLOADS", 3),
        rate_limit_seconds=_get_float("RATE_LIMIT_SECONDS", 3.0),
    )


# ========================== تشخیص پلتفرم ==========================

ACTIVE_PLATFORMS = {"instagram", "pinterest", "tiktok"}

PLATFORM_PATTERNS: dict[str, re.Pattern] = {
    "instagram": re.compile(r"(instagram\.com|instagr\.am)", re.IGNORECASE),
    "pinterest": re.compile(r"(pinterest\.[a-z.]+|pin\.it)", re.IGNORECASE),
    "tiktok": re.compile(r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)", re.IGNORECASE),
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


# ============================== دانلودر ==============================

class DownloadError(Exception):
    """خطای قابل‌فهم برای نمایش به کاربر"""
    pass


class Downloader:
    def __init__(self, download_dir: str, max_file_size_mb: int):
        self.download_dir = download_dir
        self.max_file_size = max_file_size_mb * 1024 * 1024
        os.makedirs(download_dir, exist_ok=True)

    def _base_opts(self, output_path: str) -> dict:
        return {
            "outtmpl": output_path,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": self.max_file_size,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "restrictfilenames": True,
        }

    def _platform_opts(self, platform: str, output_path: str) -> dict:
        opts = self._base_opts(output_path)
        if platform == "youtube":
            opts["format"] = "best[ext=mp4]/best"
            opts["extractor_args"] = {"youtube": {"player_client": ["android", "ios"]}}
        else:
            opts["format"] = "best[ext=mp4]/best"
        return opts

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

    def _download_sync(self, url: str, platform: str) -> tuple[str, str]:
        file_id = str(uuid.uuid4())
        output_template = os.path.join(self.download_dir, f"{file_id}.%(ext)s")
        ydl_opts = self._platform_opts(platform, output_template)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)

                if not os.path.exists(filepath):
                    filepath = self._find_downloaded_file(file_id)

                if not filepath or not os.path.exists(filepath):
                    raise DownloadError("فایلی دانلود نشد؛ ممکنه لینک خصوصی یا نامعتبر باشه.")

                size = os.path.getsize(filepath)
                if size == 0:
                    os.remove(filepath)
                    raise DownloadError("فایل دانلودشده خالی بود.")

                if size > self.max_file_size:
                    os.remove(filepath)
                    raise DownloadError(
                        f"حجم فایل بیشتر از {self.max_file_size // (1024 * 1024)} مگابایته."
                    )

                media_type = "photo" if filepath.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else "video"
                return filepath, media_type

        except yt_dlp.utils.DownloadError as e:
            raise DownloadError(self._friendly_message(str(e)))
        except DownloadError:
            raise
        except Exception:
            logger.exception("خطای غیرمنتظره در yt-dlp")
            raise DownloadError("مشکلی توی دانلود پیش اومد.")

    async def download(self, url: str, platform: str) -> tuple[str, str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, url, platform)

    @staticmethod
    def cleanup(filepath: str) -> None:
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
        "لینک پست/ریلز اینستاگرام، پین پینترست یا ویدیوی تیک‌تاک رو بفرست "
        "تا برات دانلودش کنم.\n\n"
        "فقط کافیه لینک رو کپی و همینجا پیست کنی."
    )


@router.message(F.text)
async def handle_link(message: Message, downloader: Downloader, limiter: DownloadLimiter) -> None:
    text = message.text or ""
    url = extract_url(text)

    if not url:
        await message.answer("لینک معتبری پیدا نکردم. لطفا یه لینک از اینستاگرام، پینترست یا تیک‌تاک بفرست.")
        return

    platform = detect_platform(url)

    if platform is None:
        await message.answer("این لینک رو نشناختم. اینستاگرام، پینترست و تیک‌تاک پشتیبانی می‌شن.")
        return

    if not is_active(platform):
        await message.answer(f"پشتیبانی از {platform} هنوز فعال نشده، به‌زودی اضافه می‌شه.")
        return

    status_msg = await message.answer("⏳ در حال دانلود...")

    filepath = None
    try:
        async with limiter:
            filepath, media_type = await downloader.download(url, platform)

        file = FSInputFile(filepath)

        if media_type == "photo":
            await message.answer_photo(photo=file, caption="✅ دانلود شد")
        else:
            try:
                await message.answer_video(video=file, caption="✅ دانلود شد")
            except Exception:
                file = FSInputFile(filepath)
                await message.answer_document(document=file, caption="✅ دانلود شد")

        await status_msg.delete()

    except DownloadError as e:
        await status_msg.edit_text(f"❌ {e}")
    except Exception:
        logger.exception("خطای غیرمنتظره در پردازش لینک")
        await status_msg.edit_text("❌ یه مشکلی پیش اومد، دوباره امتحان کن.")
    finally:
        if filepath:
            Downloader.cleanup(filepath)


# ============================== اجرا ==============================

async def main() -> None:
    config = load_config()

    shutil.rmtree(config.download_dir, ignore_errors=True)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    downloader = Downloader(config.download_dir, config.max_file_size_mb)
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
