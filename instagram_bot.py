"""
Telegram Instagram Downloader Bot
----------------------------------
Requirements:
    pip install python-telegram-bot yt-dlp instaloader

Usage:
    1. Get your bot token from @BotFather on Telegram
    2. Set BOT_TOKEN below (or use environment variable)
    3. Run: python instagram_bot.py
"""

import os
import re
import asyncio
import tempfile
import shutil
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import yt_dlp

# ─── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8920584581:AAFNYeZK-5djio-hRN1wXTD_Sg5WqUcDt-8")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Helpers ─────────────────────────────────────────────────────────────────
INSTAGRAM_PATTERN = re.compile(
    r"(https?://)?(www\.)?instagram\.com/"
    r"(p|reel|tv|stories)/[A-Za-z0-9_\-]+/?(\?.*)?",
    re.IGNORECASE,
)


def extract_instagram_url(text: str) -> str | None:
    """Return the first Instagram URL found in text, or None."""
    match = INSTAGRAM_PATTERN.search(text)
    return match.group(0) if match else None


def download_instagram(url: str, output_dir: str) -> list[str]:
    """
    Download media from an Instagram URL using yt-dlp.
    Returns a list of downloaded file paths.
    """
    ydl_opts = {
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        # Download best quality (video + audio merged)
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        # For carousels / multi-image posts
        "noplaylist": False,
        # Write thumbnail as image if no video found
        "writethumbnail": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Collect files written to output_dir
    files = []
    for fname in os.listdir(output_dir):
        fpath = os.path.join(output_dir, fname)
        if os.path.isfile(fpath):
            files.append(fpath)

    return files


def is_video(path: str) -> bool:
    return path.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".avi"))


def is_image(path: str) -> bool:
    return path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


# ─── Handlers ────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 سلام!\n\n"
        "لینک پست، ریل یا استوری اینستاگرام رو برام بفرست تا دانلودش کنم 🎬📸\n\n"
        "مثال:\n"
        "https://www.instagram.com/p/XXXXXXXXXXX/"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 راهنما:\n\n"
        "• لینک پست عادی ➜ عکس یا ویدیو\n"
        "• لینک ریل ➜ ویدیو\n"
        "• لینک کاروسل (چند رسانه) ➜ همه فایل‌ها\n\n"
        "⚠️ محتوای private قابل دانلود نیست."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    url = extract_instagram_url(text)

    if not url:
        await update.message.reply_text(
            "❌ لینک اینستاگرام معتبری پیدا نکردم.\n"
            "لطفاً یه لینک پست، ریل یا استوری بفرست."
        )
        return

    status_msg = await update.message.reply_text("⏳ در حال دانلود...")

    tmp_dir = tempfile.mkdtemp()
    try:
        loop = asyncio.get_event_loop()
        files = await loop.run_in_executor(
            None, download_instagram, url, tmp_dir
        )

        if not files:
            await status_msg.edit_text(
                "⚠️ هیچ فایلی دانلود نشد.\n"
                "ممکنه محتوا private باشه یا لینک منقضی شده."
            )
            return

        await status_msg.edit_text(f"📤 در حال ارسال {len(files)} فایل...")

        for fpath in files:
            try:
                with open(fpath, "rb") as f:
                    if is_video(fpath):
                        await update.message.reply_video(
                            video=f,
                            caption="📥 دانلود شد توسط ربات",
                            supports_streaming=True,
                        )
                    elif is_image(fpath):
                        await update.message.reply_photo(
                            photo=f,
                            caption="📥 دانلود شد توسط ربات",
                        )
                    else:
                        # Unknown type → send as document
                        await update.message.reply_document(
                            document=f,
                            caption="📥 دانلود شد توسط ربات",
                        )
            except Exception as send_err:
                logger.warning("Could not send %s: %s", fpath, send_err)
                await update.message.reply_text(
                    f"⚠️ ارسال یکی از فایل‌ها با خطا مواجه شد:\n{send_err}"
                )

        await status_msg.delete()

    except yt_dlp.utils.DownloadError as dl_err:
        logger.error("Download error: %s", dl_err)
        await status_msg.edit_text(
            "❌ خطا در دانلود:\n"
            "• محتوا private است یا حذف شده\n"
            "• لینک اشتباه است\n\n"
            f"جزئیات: {dl_err}"
        )
    except Exception as e:
        logger.exception("Unexpected error")
        await status_msg.edit_text(f"❌ خطای غیرمنتظره:\n{e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError(
            "❗ لطفاً BOT_TOKEN رو تنظیم کن!\n"
            "یا متغیر محیطی BOT_TOKEN رو ست کن:\n"
            "  export BOT_TOKEN='توکنت'"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 ربات شروع به کار کرد...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
