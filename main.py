@router.callback_query(F.data.startswith("ytq:"))
async def ytquality(cb: CallbackQuery, downloader: Downloader, limiter: DownloadLimiter):
    _, sid, h = cb.data.split(":")
    height = int(h)
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
        await cb.message.edit_text(f"❌ {str(e)}\n\nاگه حجمش زیاده، کیفیت پایین‌تر رو انتخاب کن")
    except Exception:
        logger.exception("خطای یوتیوب")
        await cb.message.edit_text("❌ خطا در دانلود")
    finally:
        Downloader.cleanup(items)
        pending_youtube.pop(sid, None)
