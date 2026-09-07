# DelAll Bot

Public Telegram bot jo kisi bhi channel ya group ke messages delete karta hai
aur kaam khatam hote hi khud us chat se leave kar jata hai.

## Features

- `/delall` — channel aur group dono me chalti hai
- Inline confirm button (✅ Confirm Delete / ❌ Cancel) — galti se delete nahi hoga
- Live progress update
- Delete ke baad bot khud `leaveChat` karta hai (admin status apne aap hat jata hai)
- **Koi bhi user** apne channel/group me use kar sakta hai
- Bot owner ko har job ki DM report + `/stats` command

## Kaun use kar sakta hai

Koi bhi. Bas ek safety check hai: `/delall` sirf **us chat ka apna admin** hi
chala sakta hai. Warna kisi bhi group ka random member aake pura group uda deta.

`ADMIN_IDS` = bot ka owner (tum). Ye kisi ka use **nahi rokta** — sirf itna
karta hai:

- Har delete job ke baad tumhe DM me report aati hai (kaun sa chat, kisne chalaya, kitne IDs)
- Tum `/stats` chala ke overall usage dekh sakte ho

`ADMIN_IDS` khali chhod do to bot phir bhi normally chalega, bas reports nahi aayengi.

## Zaroori baat (honest limitations)

Telegram bots chat ki history read nahi kar sakte. Isliye bot latest message ID
se lekar ID 1 tak delete try karta hai. Jo IDs delete ho sakte hain wo ho jate
hain, baaki chup-chaap skip ho jate hain.

Iska matlab:

- Bot **exact deleted count nahi bata sakta** — sirf "kitne IDs process kiye" batata hai
- Kuch messages Telegram ki taraf se delete nahi ho paate (service messages waghera)
- Bade channels me time lagta hai (~100 IDs har 0.35 second)

## Setup

### 1. Bot banao

1. Telegram pe [@BotFather](https://t.me/BotFather) kholo
2. `/newbot` bhejo, naam aur username do
3. Token copy kar lo

### 2. Apni user ID nikalo

Telegram pe [@userinfobot](https://t.me/userinfobot) ko message karo — wo tumhari
numeric ID bata dega.

### 3. GitHub repo

1. GitHub pe naya repo banao (e.g. `delall-bot`)
2. Is folder ki saari files upload kar do

### 4. Railway pe deploy

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Apna repo select karo
3. **Variables** tab me add karo:
   - `BOT_TOKEN` = BotFather wala token
   - `ADMIN_IDS` = tumhari user ID
4. Logs me `DelAll Bot chalu ho gaya` dikhega

## Use kaise kare

1. Bot ko channel/group me **admin** banao
2. **Delete Messages** permission ON rakho
3. Chat me `/delall` bhejo
4. **✅ Confirm Delete** dabao
5. Bot delete karega, phir khud nikal jayega

## Commands

| Command | Kaun | Kaam |
|---|---|---|
| `/start`, `/help` | Sabhi | Instructions |
| `/delall` | Chat ka admin | Us chat ke messages delete karo |
| `/stats` | Bot owner | Usage report |

## Environment variables

| Variable | Zaroori? | Kaam |
|---|---|---|
| `BOT_TOKEN` | Haan | BotFather ka token |
| `ADMIN_IDS` | Nahi | Bot owner ki user ID(s). Reports + `/stats` ke liye. Use nahi rokta. |
| `STATS_FILE` | Nahi | Stats file ka path (default `stats.json`) |

> Railway pe volume nahi hai to redeploy pe stats reset ho jayenge. Job reports
> phir bhi DM me aati rahengi.
