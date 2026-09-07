"""
DelAll Bot
----------
A Telegram bot that deletes every message in a channel or group, then leaves
it automatically.

How people use it:
  1. Tap "Add Channel" or "Add Group" in the bot's private chat. Telegram shows
     their own list, and the bot is promoted to admin automatically.
  2. Open that channel or group and send /delall there.
  3. Tap Confirm Delete.

Owner notifications (ADMIN_IDS):
  - Someone starts the bot     -> full user details
  - The bot is added to a chat -> full channel/group details
  - A delete job runs          -> full report
  - /stats                     -> overall usage
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
BATCH_DELAY = 0.4         # pause between batches
PROGRESS_EVERY = 5        # refresh the progress message every N batches
MAX_FLOOD_RETRIES = 25    # how many times to wait out a rate limit

REQ_CHANNEL = 1
REQ_GROUP = 2

SPINNER = ["⏳", "⌛"]
BAR_SLOTS = 12

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


def chat_type_word(chat_type: str) -> str:
    return "channel" if chat_type == "channel" else "group"


def progress_bar(pct: int, frame: int) -> str:
    pct = max(0, min(100, pct))
    filled = round(BAR_SLOTS * pct / 100)
    bar = "▰" * filled + "▱" * (BAR_SLOTS - filled)
    return f"{SPINNER[frame % len(SPINNER)]} <code>{bar}</code> {pct}%"


def admin_rights() -> ChatAdministratorRights:
    """Permissions requested when a user picks a chat from the list."""
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

    def button(label: str, req_id: int, is_channel: bool) -> KeyboardButton:
        return KeyboardButton(
            label,
            request_chat=KeyboardButtonRequestChat(
                request_id=req_id,
                chat_is_channel=is_channel,
                bot_is_member=False,
                bot_administrator_rights=admin_rights(),
                user_administrator_rights=admin_rights(),
                request_title=True,
                request_username=True,
            ),
        )

    return ReplyKeyboardMarkup(
        [[button("➕ Add Channel", REQ_CHANNEL, True), button("➕ Add Group", REQ_GROUP, False)]],
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
    """Store the user. Returns True for a first-time user."""
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


def record_job(chat_id, title, chat_type, who, deleted, status) -> None:
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


async def chat_block(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, fallback_title=None, fallback_type=None
) -> str:
    """Collect as much detail about a channel/group as the API allows."""
    try:
        chat = await context.bot.get_chat(chat_id)
    except TelegramError:
        word = chat_type_word(fallback_type or "channel").capitalize()
        return (
            f"• {word}: <b>{esc(fallback_title or chat_id)}</b>\n"
            f"• {word} ID: <code>{chat_id}</code>"
        )

    ctype = chat.type or fallback_type or "channel"
    word = chat_type_word(ctype).capitalize()

    lines = [
        f"• {word}: <b>{esc(chat.title or fallback_title or chat_id)}</b>",
        f"• {word} ID: <code>{chat_id}</code>",
        f"• Type: {esc(ctype)}",
    ]

    try:
        count = await context.bot.get_chat_member_count(chat_id)
        lines.append(f"• Members: {count}")
    except TelegramError:
        pass

    if getattr(chat, "description", None):
        lines.append(f"• Description: {esc(chat.description[:150])}")

    # Link last
    if getattr(chat, "username", None):
        lines.append(f"• Link: https://t.me/{esc(chat.username)} (@{esc(chat.username)})")
    elif getattr(chat, "invite_link", None):
        lines.append(f"• Link: {esc(chat.invite_link)}")
    else:
        lines.append("• Link: <i>private — no public link</i>")

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
            "select me, and turn on <b>Delete Messages</b>. Then send /delall there."
        )

    if not getattr(me, "can_delete_messages", False):
        return False, (
            "I'm an administrator, but the <b>Delete Messages</b> permission is off.\n\n"
            "Open the channel or group → <b>Administrators</b> → select me → "
            "turn on <b>Delete Messages</b>. Then send /delall there."
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


def is_missing(exc: TelegramError) -> bool:
    """
    True only when Telegram says the message isn't there at all.

    Anything else - including "message can't be deleted" - is a real refusal
    and gets recorded so we can see why.
    """
    text = str(exc).lower()
    return (
        "message to delete not found" in text
        or "message identifier is not specified" in text
    )


async def delete_ids(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    ids: list[int],
    report: dict,
) -> int:
    """
    Delete these message IDs and return how many were actually removed.

    Bulk delete is tried first. If Telegram rejects the batch, we split it in
    half and retry each half, so a few gaps or undeletable messages never cost
    us the whole batch. IDs that don't exist are skipped silently.
    """
    if not ids:
        return 0

    for _ in range(MAX_FLOOD_RETRIES):
        try:
            await context.bot.delete_messages(chat_id=chat_id, message_ids=ids)
            return len(ids)
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
            continue
        except Forbidden:
            raise
        except BadRequest:
            break
        except TelegramError as exc:
            logger.warning("Batch error in chat %s: %s", chat_id, exc)
            await asyncio.sleep(2)
            continue
    else:
        return 0

    # The batch was rejected — narrow it down.
    if len(ids) == 1:
        for _ in range(MAX_FLOOD_RETRIES):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=ids[0])
                return 1
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
            except Forbidden:
                raise
            except TelegramError as exc:
                if is_missing(exc):
                    report["missing"] = report.get("missing", 0) + 1
                else:
                    report["refused"] = report.get("refused", 0) + 1
                    reasons = report.setdefault("reasons", {})
                    key = str(exc)[:120]
                    reasons[key] = reasons.get(key, 0) + 1
                    logger.info("Refused to delete %s in %s: %s", ids[0], chat_id, exc)
                return 0
        return 0

    mid = len(ids) // 2
    left = await delete_ids(context, chat_id, ids[:mid], report)
    right = await delete_ids(context, chat_id, ids[mid:], report)
    return left + right


async def run_delete_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status_message_id: int,
    actor,
    chat_title: str,
    chat_type: str,
) -> None:
    """Delete everything in the chat, keep the closing message, then leave."""
    RUNNING.add(chat_id)

    word = chat_type_word(chat_type)
    started_at = now_str()
    deleted = 0
    job_status = "Completed"
    report: dict = {}

    # Capture details now — they're unavailable once the bot leaves.
    chat_info = await chat_block(context, chat_id, chat_title, chat_type)

    async def set_status(text: str) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    await set_status(f"{progress_bar(0, 0)}\n\n🧹 <b>Deleting messages…</b>")

    try:
        total = status_message_id
        current = status_message_id
        batches = 0

        while current > 0:
            ids: list[int] = []
            while current > 0 and len(ids) < BATCH_SIZE:
                if current != status_message_id:  # keep the closing message
                    ids.append(current)
                current -= 1

            try:
                deleted += await delete_ids(context, chat_id, ids, report)
            except Forbidden:
                job_status = "Stopped — bot lost access"
                logger.warning("Lost access to chat %s", chat_id)
                break

            batches += 1

            if batches % PROGRESS_EVERY == 0:
                pct = int(((total - current) / total) * 100) if total else 100
                await set_status(
                    f"{progress_bar(pct, batches)}\n\n🧹 <b>Deleting messages…</b>"
                )

            await asyncio.sleep(BATCH_DELAY)

        # Catch anything posted while we were working. The probe tells us the
        # newest message ID; it is removed afterwards and never counted.
        if job_status == "Completed":
            try:
                probe = await context.bot.send_message(chat_id, "🧹")
                tail = [
                    i
                    for i in range(status_message_id + 1, probe.message_id)
                    if i != status_message_id
                ]
                for i in range(0, len(tail), BATCH_SIZE):
                    deleted += await delete_ids(context, chat_id, tail[i : i + BATCH_SIZE], report)
                try:
                    await context.bot.delete_message(chat_id, probe.message_id)
                except TelegramError:
                    pass
            except TelegramError:
                pass

        if job_status == "Completed":
            if report.get("refused"):
                await set_status(
                    f"✅ <b>Cleanup finished</b>\n\n"
                    f"Telegram wouldn't let me remove <b>{report['refused']}</b> "
                    f"older messages, so those are still here.\n\n"
                    f"I'm leaving this {word} now. See you again 🙏"
                )
            else:
                await set_status(
                    f"✅ <b>All messages deleted</b>\n\n"
                    f"I'm leaving this {word} now. See you again 🙏"
                )
            await asyncio.sleep(3)

            try:
                await context.bot.leave_chat(chat_id)
                logger.info("Left chat %s after deleting %s messages", chat_id, deleted)
            except TelegramError as exc:
                logger.warning("Could not leave chat %s: %s", chat_id, exc)
        else:
            await set_status(
                f"⚠️ <b>Stopped early</b>\n\n"
                f"I lost access to this {word} before finishing."
            )

    except Exception:
        job_status = "Error"
        logger.exception("Delete job failed for chat %s", chat_id)
        await set_status("⚠️ <b>Something went wrong.</b> The cleanup did not finish.")
    finally:
        RUNNING.discard(chat_id)

        record_job(chat_id, chat_title, chat_type, who_short(actor), deleted, job_status)

        refused = report.get("refused", 0)
        if refused:
            reasons = sorted(
                report.get("reasons", {}).items(), key=lambda kv: kv[1], reverse=True
            )[:3]
            detail = "\n".join(f"   – {esc(msg)} (x{count})" for msg, count in reasons)
            diagnostics = (
                f"• Telegram refused: <b>{refused}</b> messages\n{detail}\n"
            )
        else:
            diagnostics = ""

        await notify_owner(
            context,
            "🧹 <b>Delete job</b>\n\n"
            f"{chat_info}\n\n"
            f"<b>Started by</b>\n{user_block(actor)}\n\n"
            f"<b>Result</b>\n"
            f"• Started: {started_at}\n"
            f"• Finished: {now_str()}\n"
            f"• Message IDs scanned: <b>{deleted:,}</b>\n"
            f"• Status: {esc(job_status)}\n"
            f"{diagnostics}\n"
            f"Total jobs so far: <b>{STATS.get('jobs', 0)}</b>",
        )


# --------------------------------------------------------------------------
# /start
# --------------------------------------------------------------------------

WELCOME = """👋 <b>Welcome to DelAll Bot</b>

I delete every message in your channel or group, then leave it automatically.

<b>Step 1 — Add me</b>
Tap <b>➕ Add Channel</b> or <b>➕ Add Group</b> below. Telegram will show your list — pick one and I'll be promoted to admin automatically.

<b>Step 2 — Send the command</b>
Open that channel or group and send <code>/delall</code> there.

<b>Step 3 — Confirm</b>
Tap <b>Confirm Delete</b> and I'll take care of the rest.

<b>Prefer to do it manually?</b>
Open your channel or group → <b>Administrators</b> → <b>Add Admin</b> → select me (@{username}) → turn on <b>Delete Messages</b> → send <code>/delall</code>.

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
# A chat was picked from Telegram's list
# --------------------------------------------------------------------------


async def on_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.chat_shared is None:
        return

    shared = message.chat_shared
    chat_id = shared.chat_id
    title = shared.title or str(chat_id)
    word = "channel" if shared.request_id == REQ_CHANNEL else "group"

    ok, reason = await bot_can_delete(context, chat_id)
    if not ok:
        await message.reply_text(f"❌ {reason}", parse_mode=ParseMode.HTML)
        return

    await message.reply_text(
        f"✅ I'm now an administrator in <b>{esc(title)}</b>.\n\n"
        f"<b>Next step:</b> open that {word} and send <code>/delall</code> there.\n\n"
        f"I'll ask you to confirm, then delete every message and leave the {word}.",
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------
# /delall
# --------------------------------------------------------------------------

PRIVATE_DELALL_HELP = """ℹ️ <code>/delall</code> only works inside a channel or group.

<b>Step 1</b> — Tap <b>➕ Add Channel</b> or <b>➕ Add Group</b> below and pick one from your list. I'll be promoted to admin automatically.

<b>Step 2</b> — Open that channel or group and send <code>/delall</code> there.

<b>Step 3</b> — Tap <b>Confirm Delete</b>."""


async def cmd_delall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if chat is None or message is None:
        return

    if chat.type == ChatType.PRIVATE:
        await message.reply_text(
            PRIVATE_DELALL_HELP,
            parse_mode=ParseMode.HTML,
            reply_markup=picker_keyboard(),
        )
        return

    user = update.effective_user
    user_id = user.id if user else None

    if not await is_chat_admin(context, chat.id, user_id):
        await message.reply_text(
            "❌ Only administrators of this channel or group can use this command."
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

    if query.data == "delall:no":
        await query.answer("Cancelled")
        try:
            await query.edit_message_text("❌ Cancelled. Nothing was deleted.")
        except TelegramError:
            pass
        return

    if query.data != "delall:yes":
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
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    actor = context.chat_data.pop("delall_user", None) or query.from_user

    await run_delete_job(
        context,
        chat_id=chat.id,
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
        f"🗑 Messages deleted: <b>{STATS.get('deleted', 0):,}</b>",
        f"💬 Channels / groups: <b>{len(STATS.get('chats', []))}</b>",
    ]

    recent = STATS.get("recent", [])
    if recent:
        lines += ["", "<b>Recent jobs</b>"]
        for e in recent[:10]:
            lines.append(
                f"• {esc(e['time'])} — {esc(e['title'])} ({esc(e['type'])}) — "
                f"{e.get('deleted', 0):,} deleted — {esc(e['status'])}"
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

    info = await chat_block(context, upd.chat.id, upd.chat.title, upd.chat.type)

    await notify_owner(
        context,
        f"{title}\n\n"
        f"{info}\n\n"
        f"<b>By</b>\n{user_block(upd.from_user)}\n\n"
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
