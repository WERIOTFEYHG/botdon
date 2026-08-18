"""
ربات تلگرامی دانلودر یوتیوب (پایتون + yt-dlp)
با الهام از معماری فیچر یوتیوب پروژه‌ی Rostam (github.com/mmahdi-sz/rostam):
    - تشخیص خودکار لینک تک‌ویدیو / پلی‌لیست
    - نمایش اطلاعات ویدیو (عنوان، کانال، مدت‌زمان، تعداد بازدید، دیسکریپشن) قبل از دانلود
    - انتخاب کیفیت از یک پنل اینلاین (دکمه‌ای)، دقیقاً یک‌بار برای کل پلی‌لیست
    - خروجی صوتی MP3 جدا از ویدیو
    - دانلود آیتم‌های پلی‌لیست به‌صورت پشت‌سرهم با گزارش وضعیت روی یک پیام ثابت (pin نمی‌کنیم، ولی edit می‌کنیم)
    - مدیریت خطای هر آیتم بدون متوقف‌کردن کل صف (مثل رستم: خطای یک آیتم، بقیه رو متوقف نمی‌کنه)

نکته‌ی مهم درباره‌ی محدودیت حجم:
    API رسمی تلگرام حداکثر ۵۰ مگابایت آپلود از طرف بات رو قبول می‌کنه.
    برای فایل‌های بزرگ‌تر (کیفیت بالا) باید از یک Local Bot API Server خودت
    (دقیقاً مثل چیزی که رستم با tdlib در پورت ۸۰۸۱/۸۰۸۲ اجرا می‌کنه) استفاده کنی
    که سقف را تا ۲ گیگابایت می‌برد. این نسخه با API رسمی کار می‌کند؛ در انتهای
    فایل توضیح داده شده چطور به Local Bot API وصل شوید.

نصب:
    pip install -r requirements.txt
    (نیاز به ffmpeg نصب‌شده روی سیستم دارید تا صدا/ویدیو merge و mp3 extract بشه)

اجرا روی Railway:
    1) این فایل‌ها را در ریپازیتوری بگذارید.
    2) متغیر محیطی TELEGRAM_BOT_TOKEN را ست کنید.
    3) مطمئن شوید ffmpeg روی محیط اجرا نصب است (nixpacks.toml یا apt در Railway).
    4) Start Command: python bot.py
"""

import os
import re
import logging
import asyncio
import functools
from dataclasses import dataclass, field

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# ---------- تنظیمات ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # سقف API رسمی تلگرام؛ اگر Local Bot API داری بالاتر ببرش
DESCRIPTION_LIMIT = 700  # برای اینکه کپشن از سقف ۱۰۲۴ کاراکتری تلگرام رد نشه

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/\S+", re.IGNORECASE
)

# دکمه‌های کیفیت -> فرمت yt-dlp (دقیقاً مثل پنل کیفیت رستم: یک انتخاب، اعمال روی همه‌ی آیتم‌های صف)
QUALITY_OPTIONS = {
    "q_best":  ("🔝 بهترین کیفیت", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"),
    "q_1080":  ("1080p",            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]"),
    "q_720":   ("720p",             "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]"),
    "q_480":   ("480p",             "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]"),
    "q_360":   ("360p",             "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]"),
    "q_audio": ("🎵 فقط صدا (MP3)", "bestaudio/best"),
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------- وضعیت هر درخواست (شبیه‌ساده‌شده‌ی pending job رستم) ----------
@dataclass
class PendingJob:
    url: str
    is_playlist: bool
    entries: list = field(default_factory=list)  # لیست URL آیتم‌های پلی‌لیست (اگر پلی‌لیست باشه)
    title: str = ""


# chat_id -> PendingJob (منتظر انتخاب کیفیت کاربر)
pending_jobs: dict[int, PendingJob] = {}


# ---------- توابع کمکی yt-dlp (Sync -> در executor اجرا میشن تا event loop بلاک نشه) ----------
def _extract_info(url: str, flat: bool = False) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": flat,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download(url: str, format_selector: str, out_dir: str, is_audio: bool) -> str:
    outtmpl = os.path.join(out_dir, "%(title).80s.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_selector,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "merge_output_format": "mp4",
    }
    if is_audio:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if is_audio:
            filename = os.path.splitext(filename)[0] + ".mp3"
        return filename


async def run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))


def format_duration(seconds) -> str:
    if not seconds:
        return "نامشخص"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_views(views) -> str:
    if not views:
        return "نامشخص"
    return f"{views:,}"


def quality_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for key, (label, _) in QUALITY_OPTIONS.items():
        row.append(InlineKeyboardButton(label, callback_data=key))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ---------- دستورات ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n"
        "لینک ویدیو یا پلی‌لیست یوتیوب رو بفرست تا اطلاعاتش رو نشونت بدم و بعد "
        "کیفیت دلخواه رو انتخاب کنی.\n\n"
        "برای پلی‌لیست: کیفیت رو فقط یک‌بار انتخاب می‌کنی و همون برای همه‌ی "
        "آیتم‌ها اعمال میشه (دقیقاً مثل رستم)."
    )


# ---------- دریافت لینک ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    if not YOUTUBE_URL_RE.search(text):
        await update.message.reply_text("این یه لینک یوتیوب معتبر نیست. یه لینک ویدیو یا پلی‌لیست بفرست.")
        return

    status_msg = await update.message.reply_text("⏳ در حال دریافت اطلاعات...")

    try:
        # اول با extract_flat چک می‌کنیم پلی‌لیسته یا نه (سریع، بدون دانلود متادیتای کامل هر آیتم)
        info = await run_blocking(_extract_info, text, True)
    except Exception as e:
        logger.error(f"extract_info error: {e}")
        await status_msg.edit_text("❌ نتونستم اطلاعات این لینک رو بگیرم. لینک رو چک کن و دوباره امتحان کن.")
        return

    is_playlist = info.get("_type") == "playlist" or "entries" in info

    if is_playlist:
        entries = [e for e in info.get("entries", []) if e]
        urls = []
        for e in entries:
            u = e.get("url") or e.get("webpage_url") or e.get("id")
            if u and not u.startswith("http"):
                u = f"https://www.youtube.com/watch?v={u}"
            if u:
                urls.append(u)

        if not urls:
            await status_msg.edit_text("❌ پلی‌لیست خالی به نظر می‌رسه یا خصوصیه.")
            return

        pending_jobs[chat_id] = PendingJob(
            url=text, is_playlist=True, entries=urls, title=info.get("title", "پلی‌لیست")
        )

        await status_msg.edit_text(
            f"📃 پلی‌لیست شناسایی شد: <b>{info.get('title', '')}</b>\n"
            f"تعداد آیتم‌ها: {len(urls)}\n\n"
            "کیفیت دانلود رو انتخاب کن (روی همه‌ی آیتم‌ها اعمال میشه):",
            parse_mode=ParseMode.HTML,
            reply_markup=quality_keyboard(),
        )
        return

    # تک ویدیو: اطلاعات کامل‌تر بگیریم (برای دیسکریپشن دقیق)
    try:
        full_info = await run_blocking(_extract_info, text, False)
    except Exception as e:
        logger.error(f"extract_info (full) error: {e}")
        await status_msg.edit_text("❌ نتونستم اطلاعات کامل این ویدیو رو بگیرم.")
        return

    pending_jobs[chat_id] = PendingJob(url=text, is_playlist=False, title=full_info.get("title", ""))

    title = full_info.get("title", "بدون عنوان")
    uploader = full_info.get("uploader", "نامشخص")
    duration = format_duration(full_info.get("duration"))
    views = format_views(full_info.get("view_count"))
    description = (full_info.get("description") or "بدون توضیحات").strip()
    if len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT].rsplit(" ", 1)[0] + " …"

    caption = (
        f"🎬 <b>{title}</b>\n"
        f"👤 کانال: {uploader}\n"
        f"⏱ مدت‌زمان: {duration}\n"
        f"👁 بازدید: {views}\n\n"
        f"📝 <b>توضیحات:</b>\n{description}"
    )

    await status_msg.delete()
    thumbnail = full_info.get("thumbnail")
    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=quality_keyboard(),
            )
            return
        except Exception:
            pass  # اگه عکس نشد، متن ساده بفرست

    await update.message.reply_text(
        caption, parse_mode=ParseMode.HTML, reply_markup=quality_keyboard()
    )


# ---------- انتخاب کیفیت ----------
async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    job = pending_jobs.get(chat_id)
    if not job:
        await query.message.reply_text("این درخواست منقضی شده. لطفاً لینک رو دوباره بفرست.")
        return

    label, format_selector = QUALITY_OPTIONS[query.data]
    is_audio = query.data == "q_audio"

    urls = job.entries if job.is_playlist else [job.url]
    total = len(urls)

    progress_msg = await query.message.reply_text(f"⏳ در حال دانلود ({label})... 0/{total}")

    ok_count = 0
    fail_count = 0

    for idx, url in enumerate(urls, start=1):
        try:
            filepath = await run_blocking(_download, url, format_selector, DOWNLOAD_DIR, is_audio)
            size = os.path.getsize(filepath)

            if size > MAX_UPLOAD_BYTES:
                await query.message.reply_text(
                    f"⚠️ «{os.path.basename(filepath)}» حجمش {size // (1024*1024)} مگابایته و "
                    f"از سقف {MAX_UPLOAD_BYTES // (1024*1024)} مگابایت API رسمی تلگرام رد شده. "
                    "کیفیت پایین‌تر رو امتحان کن یا از Local Bot API استفاده کن."
                )
                fail_count += 1
            else:
                with open(filepath, "rb") as f:
                    if is_audio:
                        await query.message.reply_audio(audio=f)
                    else:
                        await query.message.reply_video(video=f, supports_streaming=True)
                ok_count += 1

            os.remove(filepath)

        except Exception as e:
            logger.error(f"download error for {url}: {e}")
            fail_count += 1
            # طبق منطق رستم: خطای یک آیتم صف رو متوقف نمی‌کنه، به بعدی می‌ریم
            continue

        if total > 1:
            try:
                await progress_msg.edit_text(f"⏳ در حال دانلود ({label})... {idx}/{total}")
            except Exception:
                pass

    summary = f"✅ تمام شد. موفق: {ok_count}"
    if fail_count:
        summary += f" | ❌ ناموفق: {fail_count}"
    await progress_msg.edit_text(summary)

    pending_jobs.pop(chat_id, None)


# ---------- اجرای ربات ----------
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_quality_choice))

    logger.info("ربات دانلودر یوتیوب در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
