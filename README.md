# DelAll Bot

A Telegram bot that deletes every message in a channel or group, then removes
itself automatically.

## For users

**One-tap setup (recommended)**

Send `/start` to the bot and tap **➕ Add Channel** or **➕ Add Group**.
Telegram shows your own list of channels and groups — pick one, and the bot is
added as an administrator automatically with the right permission. Then tap
**Delete All Messages** and it's done.

**Manual setup**

1. Open your channel or group
2. **Administrators** → **Add Admin** → select the bot
3. Turn on **Delete Messages**
4. Send `/delall` there
5. Tap **Confirm Delete**

Either way, the bot leaves the channel or group as soon as it finishes.

## For the owner

Set `ADMIN_IDS` to your own Telegram user ID and you'll receive a DM when:

- Someone starts the bot — name, username, user ID, language, time, start count
- The bot is added to a channel or group — title, username, link, member count, description, invite link, and who added it
- A delete job runs — full channel details, who started it, start/end time, deleted count, failed count, status

`/stats` gives you totals: users, jobs, messages deleted, unique chats, recent
jobs, newest users.

`ADMIN_IDS` does **not** restrict anyone from using the bot. `/delall` is limited
to administrators of that specific channel or group, so a random member can't
wipe someone else's chat.

## Setup

### 1. Create the bot
[@BotFather](https://t.me/BotFather) → `/newbot` → name and username → copy the token

### 2. Get your user ID
Message [@userinfobot](https://t.me/userinfobot)

### 3. Push to GitHub
Create a repo (e.g. `delall-bot`) and upload all the files

### 4. Deploy on Railway
1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your repo
3. Under **Variables**, add:
   - `BOT_TOKEN`
   - `ADMIN_IDS`
4. The logs should show `DelAll Bot started`

### 5. Register commands in BotFather (optional)
BotFather → `/setcommands` → select your bot → paste:

```
start - How to use the bot
help - Help and setup steps
delall - Delete all messages in a channel or group
```

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start`, `/help` | Everyone | Setup guide and the Add Channel / Add Group buttons |
| `/delall` | Chat admins | Delete all messages in that channel or group |
| `/stats` | Bot owner | Usage report |

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BOT_TOKEN` | Yes | — | BotFather token |
| `ADMIN_IDS` | No | empty | Owner user ID(s), comma separated. Notifications and `/stats`. |
| `NOTIFY_REPEAT_STARTS` | No | `true` | Set `false` to only be notified about first-time users |
| `TZ_OFFSET_HOURS` | No | `5.5` | Timezone offset used in notifications (IST = 5.5) |
| `TZ_NAME` | No | `IST` | Timezone label |
| `STATS_FILE` | No | `stats.json` | Where stats are stored |

## Notes

- The **Add Channel / Add Group** buttons appear above the keyboard rather than
  under the message. Telegram only supports its chat picker on keyboard buttons,
  not inline buttons.
- Stats reset when Railway redeploys unless you attach a volume. Notifications
  are unaffected.
