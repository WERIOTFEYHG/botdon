@router.message(Command("test"))
async def cmd_test(message: Message, config: Config) -> None:
    """تست تشخیص ادمین"""
    user_id = message.from_user.id
    
    await message.answer(
        f"🔍 اطلاعات تست:\n\n"
        f"• آیدی شما: `{user_id}`\n"
        f"• ADMIN_IDS گلوبال: `{ADMIN_IDS}`\n"
        f"• ADMIN_IDS از Config: `{config.admin_ids}`\n"
        f"• شما ادمین هستی؟ {'✅ بله' if user_id in ADMIN_IDS else '❌ خیر'}\n"
        f"• env ADMIN_IDS: `{os.getenv('ADMIN_IDS', 'تنظیم نشده')}`\n"
    )
