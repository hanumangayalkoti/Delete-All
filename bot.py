"""
DelAll Bot
----------
A Telegram bot that deletes all messages in a channel or group, then removes
itself from that channel or group automatically.

Two ways to use it:
  1. Tap "Add Channel" / "Add Group" in the bot's private chat. Telegram shows
     your list, and the bot is added as an admin automatically.
  2. Add the bot as an admin manually and send /delall inside the channel/group.

Owner notifications (ADMIN_IDS):
  - Someone starts the bot        -> full user details
  - The bot is added to a chat    -> full channel/group details
  - A delete job runs             -> full job report
  - /stats                        -> overall usage
"""

import asyncio
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    ChatAdministratorRights,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
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

_admin_raw = os.environ.get("ADMIN_IDS", "").strip()
ADMIN_IDS = [
    int(x.strip()) for x in _admin_raw.split(",") if x.strip().lstrip("-").isdigit()
]

NOTIFY_REPEAT_STARTS = os.environ.get("NOTIFY_REPEAT_STARTS", "true").lower() in (
    "1",
    "true",
    "yes",
)

STATS_FILE = Path(os.environ.get("STATS_FILE", "stats.json"))

TZ_OFFSET_HOURS = float(os.environ.get("TZ_OFFSET_HOURS", "5.5"))
TZ_NAME = os.environ.get("TZ_NAME", "IST")

BATCH_SIZE = 100          # Telegram's bulk delete limit
BATCH_DELAY = 0.5         # pause between batches
PROGRESS_EVERY = 8        # update the progress message every N batches
RETRY_SWEEPS = 3          # extra passes over messages that failed the first time
MAX_FLOOD_RETRIES = 25    # how many times to wait out Telegram rate limits

# Request IDs for the chat picker buttons
REQ_CHANNEL = 1
REQ_GROUP = 2

RUNNING: set[int] = set()

CMD_FILTER = filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POSTS


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def now_str() -> str:
    local = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)
    return local.strftime(f"%d %b %Y, %I:%M %p {TZ_NAME}")


def esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


def admin_rights() -> ChatAdministratorRights:
    """The permissions the bot asks for when a user picks a chat."""
    return ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_post_messages=True,
    )


def picker_keyboard() -> ReplyKeyboardMarkup:
    """Buttons that open Telegram's own channel/group picker."""
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "➕ Add Channel",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=REQ_CHANNEL,
                        chat_is_channel=True,
                        bot_is_member=False,
                        bot_administrator_rights=admin_rights(),
                        user_administrator_rights=admin_rights(),
                        request_title=True,
                        request_username=True,
                    ),
                ),
                KeyboardButton(
                    "➕ Add Group",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=REQ_GROUP,
                        chat_is_channel=False,
                        bot_is_member=False,
                        bot_administrator_rights=admin_rights(),
                        user_administrator_rights=admin_rights(),
                        request_title=True,
                        request_username=True,
                    ),
                ),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


def load_stats() -> dict:
    try:
        data = json.loads(STATS_FILE.read_text())
    except Exception:
        data = {}
    data.setdefault("jobs", 0)
    data.setdefault("deleted", 0)
    data.setdefault("chats", [])
    data.setdefault("recent", [])
    data.setdefault("users", {})
    return data


def save_stats(data: dict) -> None:
    try:
        STATS_FILE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as exc:
        logger.warning("Could not save stats: %s", exc)


STATS = load_stats()


def record_user(user) -> bool:
    """Store the user. Returns True if this is a first-time user."""
    users = STATS.setdefault("users", {})
    key = str(user.id)
    is_new = key not in users

    entry = users.get(key, {})
    entry.update(
        {
            "id": user.id,
            "name": user.full_name,
            "username": user.username,
            "lang": user.language_code,
            "last_seen": now_str(),
            "starts": entry.get("starts", 0) + 1,
        }
    )
    if is_new:
        entry["first_seen"] = now_str()
    users[key] = entry
    save_stats(STATS)
    return is_new


def record_job(chat_id, title, chat_type, who, deleted, failed, status) -> None:
    STATS["jobs"] = STATS.get("jobs", 0) + 1
    STATS["deleted"] = STATS.get("deleted", 0) + deleted

    chats = STATS.setdefault("chats", [])
    if chat_id not in chats:
        chats.append(chat_id)

    recent = STATS.setdefault("recent", [])
    recent.insert(
        0,
        {
            "time": now_str(),
            "chat_id": chat_id,
            "title": title,
            "type": chat_type,
            "who": who,
            "deleted": deleted,
            "failed": failed,
            "status": status,
        },
    )
    del recent[20:]
    save_stats(STATS)


async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.warning("Could not notify owner %s: %s", admin_id, exc)


# --------------------------------------------------------------------------
# Info formatting
# --------------------------------------------------------------------------


def user_block(user) -> str:
    if user is None:
        return "• Anonymous channel admin"

    lines = [
        f"• Name: <b>{esc(user.full_name)}</b>",
        f"• Username: {'@' + esc(user.username) if user.username else '<i>none</i>'}",
        f"• User ID: <code>{user.id}</code>",
    ]
    if getattr(user, "language_code", None):
        lines.append(f"• Language: {esc(user.language_code)}")
    lines.append(f"• Profile: <a href='tg://user?id={user.id}'>open</a>")
    return "\n".join(lines)


def who_short(user) -> str:
    if user is None:
        return "anonymous channel admin"
    name = f"@{user.username}" if user.username else user.full_name
    return f"{name} ({user.id})"


async def chat_block(context: ContextTypes.DEFAULT_TYPE, chat_id: int, fallback_title=None) -> str:
    """Collect as much detail about a channel/group as the API allows."""
    lines = []
    try:
        chat = await context.bot.get_chat(chat_id)
    except TelegramError:
        return (
            f"• Title: <b>{esc(fallback_title or chat_id)}</b>\n"
            f"• Chat ID: <code>{chat_id}</code>"
        )

    lines.append(f"• Title: <b>{esc(chat.title or fallback_title or chat_id)}</b>")
    lines.append(f"• Type: {esc(chat.type)}")
    lines.append(f"• Chat ID: <code>{chat_id}</code>")

    if getattr(chat, "username", None):
        lines.append(f"• Link: @{esc(chat.username)} — https://t.me/{esc(chat.username)}")
    else:
        lines.append("• Link: <i>private — no public username</i>")

    try:
        count = await context.bot.get_chat_member_count(chat_id)
        lines.append(f"• Members: {count}")
    except TelegramError:
        pass

    if getattr(chat, "description", None):
        lines.append(f"• Description: {esc(chat.description[:150])}")
    if getattr(chat, "invite_link", None):
        lines.append(f"• Invite link: {esc(chat.invite_link)}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Permission checks
# --------------------------------------------------------------------------


async def bot_can_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> tuple[bool, str]:
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError:
        return False, (
            "I can't access that channel or group. Please add me as an "
            "administrator with the <b>Delete Messages</b> permission and try again."
        )

    if me.status != "administrator":
        return False, (
            "I'm not an administrator there yet.\n\n"
            "Open the channel or group → <b>Administrators</b> → <b>Add Admin</b> → "
            "select me, and turn on <b>Delete Messages</b>. Then send /delall again."
        )

    if not getattr(me, "can_delete_messages", False):
        return False, (
            "I'm an administrator, but the <b>Delete Messages</b> permission is off.\n\n"
            "Open the channel or group → <b>Administrators</b> → select me → "
            "turn on <b>Delete Messages</b>. Then send /delall again."
        )

    return True, ""


async def is_chat_admin(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int | None
) -> bool:
    if user_id is None:
        return True  # channel posts can only come from admins
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in ("creator", "administrator")


# --------------------------------------------------------------------------
# Deletion engine
# --------------------------------------------------------------------------


async def delete_ids(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, ids: list[int]
) -> tuple[int, list[int]]:
    """Delete a batch. Returns (deleted_count, ids_that_failed)."""
    if not ids:
        return 0, []

    for _ in range(MAX_FLOOD_RETRIES):
        try:
            await context.bot.delete_messages(chat_id=chat_id, message_ids=ids)
            return len(ids), []
        except RetryAfter as exc:
            # Rate limited — wait it out and try the same batch again.
            await asyncio.sleep(float(exc.retry_after) + 1)
        except Forbidden:
            raise
        except BadRequest:
            # The bulk call was rejected. Fall back to one message at a time so
            # a single undeletable message doesn't cost us the whole batch.
            deleted = 0
            failed: list[int] = []
            for mid in ids:
                for _ in range(MAX_FLOOD_RETRIES):
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                        deleted += 1
                        break
                    except RetryAfter as exc:
                        await asyncio.sleep(float(exc.retry_after) + 1)
                    except Forbidden:
                        raise
                    except TelegramError:
                        failed.append(mid)
                        break
            return deleted, failed
        except TelegramError as exc:
            logger.warning("Batch error in chat %s: %s", chat_id, exc)
            await asyncio.sleep(2)

    return 0, list(ids)


async def run_delete_job(
    context: ContextTypes.DEFAULT_TYPE,
    target_chat_id: int,
    status_chat_id: int,
    status_message_id: int,
    actor,
    chat_title: str,
    chat_type: str,
) -> None:
    """Delete everything in target_chat_id, then leave it."""
    RUNNING.add(target_chat_id)

    same_chat = status_chat_id == target_chat_id
    protected = status_message_id if same_chat else None

    started_at = now_str()
    deleted = 0
    failed: list[int] = []
    job_status = "completed"

    # Capture the details now — they're unavailable once the bot leaves.
    chat_info = await chat_block(context, target_chat_id, chat_title)

    async def set_status(text: str) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=status_chat_id,
                message_id=status_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    await set_status("🧹 <b>Deleting messages…</b>")

    try:
        # Find the newest message ID in the target chat.
        if same_chat:
            highest = status_message_id
        else:
            probe = await context.bot.send_message(target_chat_id, "🧹 Starting cleanup…")
            highest = probe.message_id

        total = highest
        current = highest
        batches = 0

        while current > 0:
            ids: list[int] = []
            while current > 0 and len(ids) < BATCH_SIZE:
                if current != protected:
                    ids.append(current)
                current -= 1

            try:
                done, bad = await delete_ids(context, target_chat_id, ids)
            except Forbidden:
                job_status = "stopped — bot lost access"
                logger.warning("Lost access to chat %s", target_chat_id)
                break

            deleted += done
            failed.extend(bad)
            batches += 1

            if batches % PROGRESS_EVERY == 0:
                pct = int(((total - current) / total) * 100) if total else 100
                await set_status(
                    f"🧹 <b>Deleting messages… {pct}%</b>\n\n"
                    f"Deleted so far: <b>{deleted}</b>"
                )

            await asyncio.sleep(BATCH_DELAY)

        # Extra passes over anything that didn't go the first time.
        if job_status == "completed" and failed:
            for sweep in range(RETRY_SWEEPS):
                if not failed:
                    break
                await set_status(
                    f"🧹 <b>Finishing up…</b>\n\n"
                    f"Deleted so far: <b>{deleted}</b>\n"
                    f"Retrying <b>{len(failed)}</b> remaining messages"
                )
                retry_list, failed = failed, []
                for i in range(0, len(retry_list), BATCH_SIZE):
                    chunk = retry_list[i : i + BATCH_SIZE]
                    try:
                        done, bad = await delete_ids(context, target_chat_id, chunk)
                    except Forbidden:
                        job_status = "stopped — bot lost access"
                        failed.extend(retry_list[i:])
                        break
                    deleted += done
                    failed.extend(bad)
                    await asyncio.sleep(BATCH_DELAY)
                if job_status != "completed":
                    break
                await asyncio.sleep(2)

        # Anything posted while we were working.
        if job_status == "completed":
            try:
                marker = await context.bot.send_message(target_chat_id, "🧹")
                if marker.message_id > highest + 1:
                    tail = list(range(highest + 1, marker.message_id + 1))
                    for i in range(0, len(tail), BATCH_SIZE):
                        done, bad = await delete_ids(
                            context, target_chat_id, tail[i : i + BATCH_SIZE]
                        )
                        deleted += done
                        failed.extend(bad)
                else:
                    await context.bot.delete_message(target_chat_id, marker.message_id)
            except TelegramError:
                pass

        if job_status == "completed":
            if failed:
                summary = (
                    "✅ <b>Done</b>\n\n"
                    f"Deleted <b>{deleted}</b> messages.\n"
                    f"<b>{len(failed)}</b> could not be deleted — Telegram doesn't "
                    "allow bots to remove those.\n\n"
                    f"Leaving the {chat_type_word(chat_type)} now."
                )
            else:
                summary = (
                    "✅ <b>Done</b>\n\n"
                    f"Deleted <b>{deleted}</b> messages.\n\n"
                    f"Leaving the {chat_type_word(chat_type)} now."
                )
            await set_status(summary)
            await asyncio.sleep(4)

            if same_chat:
                try:
                    await context.bot.delete_message(target_chat_id, status_message_id)
                except TelegramError:
                    pass

            try:
                await context.bot.leave_chat(target_chat_id)
                logger.info("Left chat %s after deleting %s messages", target_chat_id, deleted)
            except TelegramError as exc:
                logger.warning("Could not leave chat %s: %s", target_chat_id, exc)
        else:
            await set_status(
                f"⚠️ <b>Stopped early</b>\n\nDeleted <b>{deleted}</b> messages before "
                "I lost access to that channel or group."
            )

    except Exception:
        job_status = "error"
        logger.exception("Delete job failed for chat %s", target_chat_id)
        await set_status(
            f"⚠️ <b>Something went wrong</b>\n\nDeleted <b>{deleted}</b> messages "
            "before the error."
        )
    finally:
        RUNNING.discard(target_chat_id)

        record_job(
            target_chat_id, chat_title, chat_type, who_short(actor), deleted, len(failed), job_status
        )

        await notify_owner(
            context,
            "🧹 <b>Delete job</b>\n\n"
            f"<b>Channel / group:</b>\n{chat_info}\n\n"
            f"<b>Started by:</b>\n{user_block(actor)}\n\n"
            "<b>Result:</b>\n"
            f"• Started: {started_at}\n"
            f"• Finished: {now_str()}\n"
            f"• Deleted: <b>{deleted}</b>\n"
            f"• Failed: <b>{len(failed)}</b>\n"
            f"• Status: {esc(job_status)}\n\n"
            f"Total jobs so far: <b>{STATS.get('jobs', 0)}</b>",
        )


def chat_type_word(chat_type: str) -> str:
    return "channel" if chat_type == "channel" else "group"


# --------------------------------------------------------------------------
# /start
# --------------------------------------------------------------------------

WELCOME = """👋 <b>Welcome to DelAll Bot</b>

I delete every message in your channel or group, then remove myself automatically.

<b>Option 1 — One tap (recommended)</b>
Tap <b>➕ Add Channel</b> or <b>➕ Add Group</b> below. Telegram will show your list — pick one, and I'll be added as an admin automatically. Then just confirm.

<b>Option 2 — Manual</b>
1. Open your channel or group
2. <b>Administrators</b> → <b>Add Admin</b> → select me (@{username})
3. Turn on <b>Delete Messages</b>
4. Send <code>/delall</code> there
5. Tap <b>Confirm Delete</b>

Deletion cannot be undone, so I always ask for confirmation first.

Need this again? Send /help."""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None:
        return

    me = await context.bot.get_me()
    in_private = chat is not None and chat.type == ChatType.PRIVATE

    await message.reply_text(
        WELCOME.format(username=me.username),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=picker_keyboard() if in_private else None,
    )

    if not in_private or user is None:
        return

    is_new = record_user(user)
    if not is_new and not NOTIFY_REPEAT_STARTS:
        return

    entry = STATS["users"][str(user.id)]
    header = "🆕 <b>New user</b>" if is_new else "🔁 <b>Returning user</b>"

    await notify_owner(
        context,
        f"{header}\n\n"
        f"{user_block(user)}\n"
        f"• Time: {now_str()}\n"
        f"• Total starts: {entry['starts']}\n"
        f"• First seen: {esc(entry.get('first_seen', '—'))}\n\n"
        f"👥 Total users: <b>{len(STATS.get('users', {}))}</b>",
    )


# --------------------------------------------------------------------------
# Chat picked from Telegram's list
# --------------------------------------------------------------------------


async def on_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.chat_shared is None:
        return

    shared = message.chat_shared
    chat_id = shared.chat_id
    title = shared.title or str(chat_id)
    kind = "channel" if shared.request_id == REQ_CHANNEL else "group"

    ok, reason = await bot_can_delete(context, chat_id)
    if not ok:
        await message.reply_text(
            f"❌ {reason}",
            parse_mode=ParseMode.HTML,
            reply_markup=picker_keyboard(),
        )
        return

    if chat_id in RUNNING:
        await message.reply_text("⏳ A deletion is already running there.")
        return

    context.user_data["target"] = {"id": chat_id, "title": title, "type": kind}

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗑 Delete All Messages", callback_data="delall:go"),
                InlineKeyboardButton("❌ Cancel", callback_data="delall:no"),
            ]
        ]
    )

    await message.reply_text(
        f"✅ I've been added to <b>{esc(title)}</b> as an administrator.\n\n"
        f"Delete every message in this {kind}? This cannot be undone.\n"
        f"I'll leave the {kind} as soon as I'm finished.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# --------------------------------------------------------------------------
# /delall
# --------------------------------------------------------------------------

PRIVATE_DELALL_HELP = """ℹ️ <code>/delall</code> only works inside a channel or group.

<b>Easiest way:</b> tap <b>➕ Add Channel</b> or <b>➕ Add Group</b> below — Telegram will show your list and I'll be added as an admin automatically.

<b>Or do it manually:</b>
1. Open your channel or group
2. <b>Administrators</b> → <b>Add Admin</b> → select me (@{username})
3. Turn on <b>Delete Messages</b>
4. Send <code>/delall</code> there
5. Tap <b>Confirm Delete</b>"""


async def cmd_delall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if chat is None or message is None:
        return

    if chat.type == ChatType.PRIVATE:
        me = await context.bot.get_me()
        await message.reply_text(
            PRIVATE_DELALL_HELP.format(username=me.username),
            parse_mode=ParseMode.HTML,
            reply_markup=picker_keyboard(),
        )
        return

    user = update.effective_user
    user_id = user.id if user else None

    if not await is_chat_admin(context, chat.id, user_id):
        await message.reply_text(
            "❌ Only administrators of this channel or group can use this command.",
            parse_mode=ParseMode.HTML,
        )
        return

    ok, reason = await bot_can_delete(context, chat.id)
    if not ok:
        await message.reply_text(f"❌ {reason}", parse_mode=ParseMode.HTML)
        return

    if chat.id in RUNNING:
        await message.reply_text("⏳ A deletion is already running here.")
        return

    context.chat_data["delall_user"] = user

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm Delete", callback_data="delall:yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="delall:no"),
            ]
        ]
    )

    word = chat_type_word(chat.type)
    await message.reply_text(
        f"⚠️ <b>Delete every message in this {word}?</b>\n\n"
        f"This cannot be undone. I'll leave the {word} as soon as I'm finished.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return

    chat = query.message.chat
    data = query.data

    if data == "delall:no":
        await query.answer("Cancelled")
        context.user_data.pop("target", None)
        try:
            await query.edit_message_text("❌ Cancelled. Nothing was deleted.")
        except TelegramError:
            pass
        return

    # Started from the bot's private chat via the picker
    if data == "delall:go":
        target = context.user_data.get("target")
        if not target:
            await query.answer("That selection expired. Please pick the chat again.", show_alert=True)
            return

        ok, reason = await bot_can_delete(context, target["id"])
        if not ok:
            await query.answer()
            try:
                await query.edit_message_text(f"❌ {reason}", parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
            return

        if target["id"] in RUNNING:
            await query.answer("Already running.", show_alert=True)
            return

        await query.answer("Starting…")
        context.user_data.pop("target", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass

        await run_delete_job(
            context,
            target_chat_id=target["id"],
            status_chat_id=chat.id,
            status_message_id=query.message.message_id,
            actor=query.from_user,
            chat_title=target["title"],
            chat_type=target["type"],
        )
        return

    # Started with /delall inside the channel or group
    if data != "delall:yes":
        await query.answer()
        return

    user_id = query.from_user.id if query.from_user else None
    if not await is_chat_admin(context, chat.id, user_id):
        await query.answer(
            "Only administrators of this channel or group can do that.", show_alert=True
        )
        return

    if chat.id in RUNNING:
        await query.answer("Already running.", show_alert=True)
        return

    ok, reason = await bot_can_delete(context, chat.id)
    if not ok:
        await query.answer()
        try:
            await query.edit_message_text(f"❌ {reason}", parse_mode=ParseMode.HTML)
        except TelegramError:
            pass
        return

    await query.answer("Starting…")
    actor = context.chat_data.pop("delall_user", None) or query.from_user

    await run_delete_job(
        context,
        target_chat_id=chat.id,
        status_chat_id=chat.id,
        status_message_id=query.message.message_id,
        actor=actor,
        chat_title=chat.title or str(chat.id),
        chat_type=chat.type,
    )


# --------------------------------------------------------------------------
# /stats (owner only)
# --------------------------------------------------------------------------


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if message is None:
        return

    if not ADMIN_IDS or user is None or user.id not in ADMIN_IDS:
        await message.reply_text("This command is only available to the bot owner.")
        return

    users = STATS.get("users", {})
    lines = [
        "📊 <b>DelAll Bot — Stats</b>",
        "",
        f"👥 Total users: <b>{len(users)}</b>",
        f"🧹 Delete jobs: <b>{STATS.get('jobs', 0)}</b>",
        f"🗑 Messages deleted: <b>{STATS.get('deleted', 0)}</b>",
        f"💬 Unique chats: <b>{len(STATS.get('chats', []))}</b>",
    ]

    recent = STATS.get("recent", [])
    if recent:
        lines += ["", "<b>Recent jobs</b>"]
        for e in recent[:10]:
            lines.append(
                f"• {esc(e['time'])} — {esc(e['title'])} ({esc(e['type'])}) — "
                f"{e.get('deleted', 0)} deleted — {esc(e['status'])}"
            )

    latest = sorted(users.values(), key=lambda u: u.get("first_seen", ""), reverse=True)[:5]
    if latest:
        lines += ["", "<b>Newest users</b>"]
        for u in latest:
            tag = f"@{esc(u['username'])}" if u.get("username") else esc(u.get("name", "?"))
            lines.append(f"• {tag} — <code>{u['id']}</code> — {esc(u.get('first_seen', '—'))}")

    lines += ["", "<i>Stats reset when Railway redeploys unless a volume is attached.</i>"]

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------
# Membership changes
# --------------------------------------------------------------------------


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    upd = update.my_chat_member
    if upd is None or upd.chat.type == ChatType.PRIVATE:
        return

    old = upd.old_chat_member.status
    new = upd.new_chat_member.status
    if old == new:
        return

    if new == "administrator":
        can_del = getattr(upd.new_chat_member, "can_delete_messages", False)
        title = (
            "✅ <b>Bot promoted to admin</b>"
            if can_del
            else "⚠️ <b>Bot promoted, but Delete Messages is off</b>"
        )
    elif new == "member":
        title = "➕ <b>Bot added (not an admin yet)</b>"
    elif new in ("left", "kicked"):
        title = "➖ <b>Bot removed</b>"
    else:
        title = f"ℹ️ <b>Bot status changed to {esc(new)}</b>"

    info = await chat_block(context, upd.chat.id, upd.chat.title)

    await notify_owner(
        context,
        f"{title}\n\n"
        f"<b>Channel / group:</b>\n{info}\n\n"
        f"<b>By:</b>\n{user_block(upd.from_user)}\n\n"
        f"• Time: {now_str()}\n"
        f"• Change: {esc(old)} → {esc(new)}",
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update error: %s", context.error)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Add it to your Railway variables.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start, filters=CMD_FILTER))
    app.add_handler(CommandHandler("help", cmd_start, filters=CMD_FILTER))
    app.add_handler(CommandHandler("stats", cmd_stats, filters=CMD_FILTER))
    app.add_handler(CommandHandler("delall", cmd_delall, filters=CMD_FILTER))
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, on_chat_shared))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^delall:"))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)

    logger.info("DelAll Bot started | owners: %s", ADMIN_IDS or "none")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
