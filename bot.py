"""
Telegram bot that logs into Instagram (via instagrapi) and shows the
same categories as the "nofollow.app" screenshot:
  - Doesn't follow you back
  - You don't follow back
  - Mutual followers
  - Your followers / Who you follow
  - Recent unfollowed you

SECURITY NOTE:
  Your Instagram password is used only once, in memory, to establish a
  session (cookies/tokens) with instagrapi. After that, only the
  session file is kept on disk (data/<telegram_id>/session.json) —
  the password itself is never written to disk. Anyone with access to
  that session file could act as you on Instagram until you log out
  from Instagram's "Login activity" settings, so keep this bot's
  server private and never share the data/ folder.

Run:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC-your-telegram-bot-token"
    python bot.py
"""

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import instagram_helper as ig
from instagrapi.exceptions import BadPassword

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

USERNAME, PASSWORD, TWOFA = range(3)

MAIN_MENU = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("Doesn't follow you back", callback_data="cat:doesnt_follow_back"),
            InlineKeyboardButton("You don't follow back", callback_data="cat:you_dont_follow_back"),
        ],
        [
            InlineKeyboardButton("Mutual followers", callback_data="cat:mutual"),
            InlineKeyboardButton("Recent unfollowed you", callback_data="cat:recent_unfollowed"),
        ],
        [InlineKeyboardButton("🔄 Refresh list", callback_data="cat:refresh")],
        [InlineKeyboardButton("🚪 Logout", callback_data="cat:logout")],
    ]
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if ig.is_logged_in(tg_id):
        await update.message.reply_text(
            "قبلاً وارد شدی. از دکمه‌های زیر استفاده کن:", reply_markup=MAIN_MENU
        )
    else:
        await update.message.reply_text(
            "برای اتصال به اینستاگرامت، /login رو بزن."
        )


async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("یوزرنیم اینستاگرامت رو بفرست:")
    return USERNAME


async def login_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ig_username"] = update.message.text.strip()
    await update.message.reply_text(
        "حالا پسوردت رو بفرست.\n"
        "⚠️ پسورد فقط یک‌بار برای ورود استفاده و بعدش از حافظه پاک میشه؛ "
        "روی سرور ذخیره نمیشه."
    )
    return PASSWORD


async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    username = context.user_data.pop("ig_username")
    password = update.message.text

    # try to delete the message containing the password for basic hygiene
    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg = await update.effective_chat.send_message("در حال ورود...")

    try:
        ig.login(tg_id, username, password)
    except ig.TwoFactorNeeded:
        await status_msg.edit_text(
            "این اکانت تایید دو مرحله‌ای (2FA) داره. کد ارسال‌شده به پیامک/اپ رو بفرست:"
        )
        return TWOFA
    except ig.ChallengeNeeded:
        await status_msg.edit_text(
            "اینستاگرام درخواست تایید امنیتی (challenge) کرده. لطفاً یک‌بار از "
            "اپ رسمی اینستاگرام روی همین دستگاه/شبکه لاگین کن تا تایید بشه، "
            "بعد دوباره /login رو بزن."
        )
        return ConversationHandler.END
    except BadPassword:
        await status_msg.edit_text("یوزرنیم یا پسورد اشتباهه. دوباره /login رو بزن.")
        return ConversationHandler.END
    except Exception as e:
        log.exception("login failed")
        await status_msg.edit_text(f"ورود ناموفق بود: {e}")
        return ConversationHandler.END
    finally:
        password = None  # drop reference

    await status_msg.edit_text("✅ با موفقیت وارد شدی!", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def login_twofa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    code = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg = await update.effective_chat.send_message("در حال بررسی کد...")
    try:
        ig.login_2fa(tg_id, code)
    except Exception as e:
        await status_msg.edit_text(f"کد اشتباه بود یا منقضی شده: {e}\nدوباره /login رو بزن.")
        return ConversationHandler.END

    await status_msg.edit_text("✅ با موفقیت وارد شدی!", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END


def _format_list(names, empty_msg):
    if not names:
        return empty_msg
    return "\n".join(f"• @{n}" for n in names)


async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id
    action = query.data.split(":", 1)[1]

    if not ig.is_logged_in(tg_id):
        await query.edit_message_text("اول باید لاگین کنی. /login رو بزن.")
        return

    if action == "logout":
        ig.logout(tg_id)
        await query.edit_message_text("خارج شدی. برای اتصال دوباره /login رو بزن.")
        return

    cl = ig.get_client(tg_id)

    try:
        followers, following = ig.fetch_followers_following(cl)
    except Exception as e:
        await query.edit_message_text(f"خطا در گرفتن لیست‌ها: {e}")
        return

    data = ig.compute_categories(tg_id, followers, following)

    if action == "refresh":
        text = (
            f"لیست‌ها به‌روزرسانی شد ✅\n"
            f"فالوئرها: {data['followers_count']}\n"
            f"فالووینگ: {data['following_count']}"
        )
    elif action == "doesnt_follow_back":
        text = (
            f"❌ فالوبک نمی‌کنن ({len(data['doesnt_follow_back'])}):\n"
            + _format_list(data["doesnt_follow_back"], "همه فالوبک کردن 🎉")
        )
    elif action == "you_dont_follow_back":
        text = (
            f"👤 تو فالوبک نکردی ({len(data['you_dont_follow_back'])}):\n"
            + _format_list(data["you_dont_follow_back"], "همه رو فالو کردی 🎉")
        )
    elif action == "mutual":
        text = (
            f"🤝 فالوئرهای متقابل ({len(data['mutual'])}):\n"
            + _format_list(data["mutual"], "-")
        )
    elif action == "recent_unfollowed":
        text = (
            f"🕵️ اخیراً آنفالوت کردن ({len(data['recent_unfollowed'])}):\n"
            + _format_list(
                data["recent_unfollowed"],
                "کسی آنفالوت نکرده، بعداً دوباره چک کن 😄",
            )
        )
    else:
        text = "دستور نامشخص."

    # Telegram messages have a length limit; trim if needed
    if len(text) > 3500:
        text = text[:3500] + "\n...(لیست کوتاه شد)"

    await query.edit_message_text(text, reply_markup=MAIN_MENU)


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN environment variable is not set.")

    app = Application.builder().token(token).build()

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_username)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
            TWOFA: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_twofa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(login_conv)
    app.add_handler(CallbackQueryHandler(handle_category, pattern=r"^cat:"))

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
