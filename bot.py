"""
DelAll Bot
----------
Public Telegram bot jo kisi bhi channel/group ke messages delete karta hai
aur kaam khatam hote hi khud us chat se leave kar jata hai.

Use kaise kare:
  1. Bot ko apne channel/group me ADMIN banao (Delete Messages permission ON)
  2. Wahan /delall bhejo
  3. Inline button se Confirm karo
  4. Delete ke baad bot khud chat chhod dega

ADMIN_IDS ka kaam:
  Ye bot ke OWNER (tum) ke liye hai - use rokta nahi hai. Har delete job
  ke baad owner ko DM me report jati hai, aur owner /stats se overall
  numbers dekh sakta hai.

Honest note:
  Telegram bots chat ki history read nahi kar sakte. Isliye bot latest
  message ID se ID 1 tak delete try karta hai. Jo IDs delete ho sakte hain
  wo ho jate hain, baaki chup-chaap skip. Isi wajah se bot "exactly kitne
  delete hue" ka pakka number nahi de sakta - sirf kitne IDs process kiye.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Bot ke owner(s) - inhe report milti hai. Use kisi ke liye block nahi hota.
_admin_raw = os.environ.get("ADMIN_IDS", "").strip()
ADMIN_IDS = [int(x.strip()) for x in _admin_raw.split(",") if x.strip().lstrip("-").isdigit()]

STATS_FILE = Path(os.environ.get("STATS_FILE", "stats.json"))

BATCH_SIZE = 100        # Telegram bulk delete limit
BATCH_DELAY = 0.35      # har batch ke beech gap (seconds)
PROGRESS_EVERY = 10     # kitne batch baad progress update

RUNNING: set[int] = set()

CMD_FILTER = filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POSTS


# --------------------------------------------------------------------------
# Stats (owner reporting ke liye)
# --------------------------------------------------------------------------


def load_stats() -> dict:
    try:
        return json.loads(STATS_FILE.read_text())
    except Exception:
        return {"jobs": 0, "ids_processed": 0, "chats": [], "recent": []}


def save_stats(data: dict) -> None:
    try:
        STATS_FILE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as exc:
        logger.warning("Stats save fail: %s", exc)


STATS = load_stats()


def record_job(chat_id: int, title: str, chat_type: str, who: str, ids: int, status: str) -> None:
    STATS["jobs"] = STATS.get("jobs", 0) + 1
    STATS["ids_processed"] = STATS.get("ids_processed", 0) + ids

    chats = STATS.setdefault("chats", [])
    if chat_id not in chats:
        chats.append(chat_id)

    entry = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "chat_id": chat_id,
        "title": title,
        "type": chat_type,
        "who": who,
        "ids": ids,
        "status": status,
    }
    recent = STATS.setdefault("recent", [])
    recent.insert(0, entry)
    del recent[20:]

    save_stats(STATS)


async def report_to_owner(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode="Markdown")
        except TelegramError as exc:
            logger.warning("Owner %s ko report nahi bhej paya: %s", admin_id, exc)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def bot_can_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> tuple[bool, str]:
    """Bot admin hai aur delete permission hai ya nahi."""
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError as exc:
        return False, f"Mera status check nahi ho paya: {exc}"

    if me.status != "administrator":
        return False, "Main is chat me admin nahi hoon. Pehle mujhe admin banao."

    if not getattr(me, "can_delete_messages", False):
        return False, "Mere paas 'Delete Messages' permission nahi hai. Wo ON karo."

    return True, ""


async def is_chat_admin(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int | None
) -> bool:
    """Sirf us chat ka apna admin hi delete chala sake (safety)."""
    if user_id is None:
        # Channel post / anonymous admin - channel me sirf admin hi post kar sakta hai
        return True

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False

    return member.status in ("creator", "administrator")


def who_label(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "channel admin (anonymous)"
    name = f"@{user.username}" if user.username else (user.full_name or "unknown")
    return f"{name} (`{user.id}`)"


async def delete_batch(context: ContextTypes.DEFAULT_TYPE, chat_id: int, ids: list[int]) -> None:
    """Ek batch ke message IDs delete karne ki koshish karo."""
    if not ids:
        return

    for attempt in range(3):
        try:
            await context.bot.delete_messages(chat_id=chat_id, message_ids=ids)
            return
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
        except Forbidden:
            raise  # bot nikal diya gaya / permission gayi
        except BadRequest:
            for mid in ids:  # bulk fail - ek ek karke
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                except RetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after) + 1)
                except Forbidden:
                    raise
                except TelegramError:
                    pass
            return
        except TelegramError as exc:
            logger.warning("Batch fail (chat %s, try %s): %s", chat_id, attempt, exc)
            await asyncio.sleep(2)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 *DelAll Bot*\n\n"
        "Main kisi bhi channel ya group ke messages saaf kar sakta hoon.\n\n"
        "*Kaise use kare:*\n"
        "1️⃣ Mujhe apne channel/group me *admin* banao\n"
        "2️⃣ *Delete Messages* permission ON rakho\n"
        "3️⃣ Wahan `/delall` bhejo\n"
        "4️⃣ Confirm button dabao\n\n"
        "Kaam khatam hote hi main khud chat se nikal jaunga ✅\n\n"
        "⚠️ *Honest note:* Telegram bots chat history read nahi kar sakte. "
        "Main latest message ID se 1 tak delete try karta hoon. Jo delete ho "
        "sakta hai wo ho jata hai, baaki skip. Isliye exact deleted count "
        "main nahi bata sakta."
    )
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sirf bot owner ke liye - usage report."""
    user = update.effective_user
    message = update.effective_message
    if message is None:
        return

    if not ADMIN_IDS or user is None or user.id not in ADMIN_IDS:
        await message.reply_text("Ye command sirf bot owner ke liye hai.")
        return

    lines = [
        "📊 *DelAll Bot Stats*",
        "",
        f"Total jobs: `{STATS.get('jobs', 0)}`",
        f"Total IDs processed: `{STATS.get('ids_processed', 0)}`",
        f"Unique chats: `{len(STATS.get('chats', []))}`",
    ]

    recent = STATS.get("recent", [])
    if recent:
        lines += ["", "*Recent jobs:*"]
        for e in recent[:10]:
            lines.append(
                f"• {e['time']} — {e['title']} ({e['type']}) — "
                f"{e['ids']} IDs — {e['status']}"
            )

    lines += ["", "_Note: Railway redeploy pe stats reset ho jate hain (volume nahi hai)._"]

    await message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_delall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if chat is None or message is None:
        return

    if chat.type == ChatType.PRIVATE:
        await message.reply_text(
            "Ye command private chat me kaam nahi karti.\n"
            "Mujhe apne channel ya group me admin banao, phir wahan /delall bhejo."
        )
        return

    user_id = update.effective_user.id if update.effective_user else None

    if not await is_chat_admin(context, chat.id, user_id):
        await message.reply_text(
            "❌ Sirf is chat ke admin hi ye command chala sakte hain."
        )
        return

    ok, reason = await bot_can_delete(context, chat.id)
    if not ok:
        await message.reply_text(f"❌ {reason}")
        return

    if chat.id in RUNNING:
        await message.reply_text("⏳ Is chat me delete already chal raha hai.")
        return

    # Owner report ke liye kisne chalaya wo yaad rakho
    context.chat_data["delall_by"] = who_label(update)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm Delete", callback_data="delall:yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="delall:no"),
            ]
        ]
    )

    await message.reply_text(
        "⚠️ *Confirm karo*\n\n"
        "Main is chat ke saare messages delete karne ki koshish karunga.\n"
        "Ye kaam *undo nahi* ho sakta.\n\n"
        "Delete ke baad main khud chat se leave kar jaunga.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# --------------------------------------------------------------------------
# Button handler
# --------------------------------------------------------------------------


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return

    chat = query.message.chat
    user_id = query.from_user.id if query.from_user else None

    if not await is_chat_admin(context, chat.id, user_id):
        await query.answer("Sirf is chat ke admin hi ye kar sakte hain.", show_alert=True)
        return

    if query.data == "delall:no":
        await query.answer("Cancel")
        try:
            await query.edit_message_text("❌ Cancel kar diya. Kuch delete nahi hua.")
        except TelegramError:
            pass
        return

    if query.data != "delall:yes":
        await query.answer()
        return

    if chat.id in RUNNING:
        await query.answer("Already chal raha hai.", show_alert=True)
        return

    ok, reason = await bot_can_delete(context, chat.id)
    if not ok:
        await query.answer()
        try:
            await query.edit_message_text(f"❌ {reason}")
        except TelegramError:
            pass
        return

    await query.answer("Shuru kar raha hoon...")
    RUNNING.add(chat.id)

    status_id = query.message.message_id
    chat_title = chat.title or str(chat.id)
    chat_type = chat.type
    started_by = context.chat_data.get("delall_by") or who_label(update)

    processed = 0
    batch_count = 0
    job_status = "completed"

    try:
        await query.edit_message_text("🧹 Delete shuru ho gaya...")
    except TelegramError:
        pass

    try:
        current = status_id
        while current > 0:
            ids: list[int] = []
            while current > 0 and len(ids) < BATCH_SIZE:
                if current != status_id:  # apna status message abhi rehne do
                    ids.append(current)
                current -= 1

            try:
                await delete_batch(context, chat.id, ids)
            except Forbidden:
                job_status = "aborted (access gaya)"
                logger.warning("Chat %s se access chala gaya", chat.id)
                break

            processed += len(ids)
            batch_count += 1

            if batch_count % PROGRESS_EVERY == 0:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat.id,
                        message_id=status_id,
                        text=(
                            "🧹 *Delete chal raha hai...*\n\n"
                            f"Process kiye: `{processed}` message IDs\n"
                            f"Baaki: `{max(current, 0)}`"
                        ),
                        parse_mode="Markdown",
                    )
                except TelegramError:
                    pass

            await asyncio.sleep(BATCH_DELAY)

        if job_status == "completed":
            try:
                await context.bot.edit_message_text(
                    chat_id=chat.id,
                    message_id=status_id,
                    text=(
                        "✅ *Ho gaya*\n\n"
                        f"`{processed}` message IDs process kiye.\n"
                        "Jo Telegram delete karne deta tha, sab hat gaye.\n\n"
                        "Ab main chat se leave kar raha hoon 👋"
                    ),
                    parse_mode="Markdown",
                )
            except TelegramError:
                pass

            await asyncio.sleep(5)

            try:
                await context.bot.delete_message(chat.id, status_id)
            except TelegramError:
                pass

            try:
                await context.bot.leave_chat(chat.id)
                logger.info("Chat %s chhod diya (%s IDs)", chat.id, processed)
            except TelegramError as exc:
                logger.warning("Leave chat fail (%s): %s", chat.id, exc)

    except Exception:
        job_status = "crashed"
        logger.exception("Delete job crash (chat %s)", chat.id)
        try:
            await context.bot.edit_message_text(
                chat_id=chat.id,
                message_id=status_id,
                text=f"⚠️ Kuch gadbad ho gayi. {processed} IDs tak process hua tha.",
            )
        except TelegramError:
            pass
    finally:
        RUNNING.discard(chat.id)
        context.chat_data.pop("delall_by", None)

        record_job(chat.id, chat_title, chat_type, started_by, processed, job_status)
        await report_to_owner(
            context,
            (
                "🔔 *DelAll job*\n\n"
                f"Chat: {chat_title}\n"
                f"Chat ID: `{chat.id}`\n"
                f"Type: {chat_type}\n"
                f"Chalaya: {started_by}\n"
                f"IDs process kiye: `{processed}`\n"
                f"Status: {job_status}"
            ),
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update error: %s", context.error)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN set nahi hai. Railway Variables me daalo.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start, filters=CMD_FILTER))
    app.add_handler(CommandHandler("help", cmd_start, filters=CMD_FILTER))
    app.add_handler(CommandHandler("stats", cmd_stats, filters=CMD_FILTER))
    app.add_handler(CommandHandler("delall", cmd_delall, filters=CMD_FILTER))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^delall:"))
    app.add_error_handler(on_error)

    logger.info("DelAll Bot chalu ho gaya | owners: %s", ADMIN_IDS or "koi nahi")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
