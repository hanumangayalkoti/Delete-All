# DelAll Bot

Telegram bot jo kisi bhi channel ya group ke messages delete karta hai aur
kaam khatam hote hi khud us chat se leave kar jata hai.

## Features

- `/delall` command — channel aur group dono me chalti hai
- Inline confirm button (✅ Confirm Delete / ❌ Cancel) — galti se delete nahi hoga
- Live progress update
- Delete ke baad bot khud `leaveChat` karta hai (admin status apne aap hat jata hai)
- Sirf chat ke admin (ya `OWNER_IDS`) hi command chala sakte hain

## Zaroori baat (honest limitations)

Telegram bots chat ki history read nahi kar sakte. Isliye bot latest message ID
se lekar ID 1 tak delete try karta hai. Jo IDs delete ho sakte hain wo delete ho
jate hain, baaki chup-chaap skip ho jate hain.

Iska matlab:

- Bot **exact deleted count nahi bata sakta** — wo sirf "kitne IDs process kiye" batata hai
- Kuch messages Telegram ki taraf se delete nahi ho paate (service messages waghera)
- Bade channels me time lagta hai (~100 IDs har 0.35 second)

## Setup

### 1. Bot banao

1. Telegram pe [@BotFather](https://t.me/BotFather) kholo
2. `/newbot` bhejo, naam aur username do
3. Jo token mile use copy kar lo

### 2. GitHub repo

1. GitHub pe naya repo banao (e.g. `delall-bot`)
2. Is folder ki saari files upload kar do

### 3. Railway pe deploy

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Apna `delall-bot` repo select karo
3. **Variables** tab me jao aur add karo:
   - `BOT_TOKEN` = BotFather wala token
   - `OWNER_IDS` (optional) = tumhara Telegram user ID, e.g. `123456789`
4. Deploy hone do — logs me `DelAll Bot chalu ho gaya` dikhega

> Railway agar service ko "web" samajh kar port maange, to Settings me
> service type **Worker** kar do (ya `Procfile` apne aap handle kar lega).

## Use kaise kare

1. Bot ko channel/group me **admin** banao
2. **Delete Messages** permission ON rakho
3. Chat me `/delall` bhejo
4. **✅ Confirm Delete** dabao
5. Bot delete karega, phir khud nikal jayega

## Environment variables

| Variable | Zaroori? | Kaam |
|---|---|---|
| `BOT_TOKEN` | Haan | BotFather ka token |
| `OWNER_IDS` | Nahi | Comma-separated user IDs. Set kiya to sirf yahi log command chala sakte hain. Khali chhoda to koi bhi chat admin chala sakta hai. |
