import asyncio
import os
import random
import sqlite3
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------- CONFIG ----------
TOKEN = os.getenv("BOT_TOKEN")
ADMIN = "Legendjau2"

# ---------- DB ----------
conn = sqlite3.connect("casino.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance INTEGER DEFAULT 100,
    last_bonus INTEGER DEFAULT 0
)
""")
conn.commit()

# ---------- STATE ----------
mines_games = {}
pvp_requests = {}
user_custom = {}
basket_pending = {}
coin_pending = {}
next_pvp_id = 1


# ---------- USERS ----------
def get_user(uid, username=""):
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    user = cur.fetchone()

    if not user:
        cur.execute(
            "INSERT INTO users (user_id, username, balance, last_bonus) VALUES (?, ?, 100, 0)",
            (uid, username),
        )
        conn.commit()
        return get_user(uid, username)

    if username and user[1] != username:
        cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
        conn.commit()

    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    return cur.fetchone()


def set_balance(uid, bal):
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (bal, uid))
    conn.commit()


def get_by_username(name):
    cur.execute("SELECT * FROM users WHERE username=?", (name,))
    return cur.fetchone()


def user_label(uid, username):
    return f"@{username}" if username else str(uid)


# ---------- BONUS ----------
def claim_bonus(uid):
    now = int(time.time())
    cur.execute("SELECT last_bonus FROM users WHERE user_id=?", (uid,))
    last = cur.fetchone()[0]

    if now - last < 86400:
        return False, 86400 - (now - last)

    cur.execute(
        "UPDATE users SET balance = balance + 50, last_bonus=? WHERE user_id=?",
        (now, uid),
    )
    conn.commit()
    return True, 0


# ---------- MENUS ----------
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        [InlineKeyboardButton("⚔ PvP", callback_data="pvp")],
        [InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("💰 Баланс", callback_data="bal")],
        [InlineKeyboardButton("🎁 Бонус +50", callback_data="bonus")],
        [InlineKeyboardButton("💸 Перевод", callback_data="send")],
        [InlineKeyboardButton("👑 Админ", callback_data="admin")],
    ])


def games():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 Баскет", callback_data="basket")],
        [InlineKeyboardButton("⚽ Футбол", callback_data="football")],
        [InlineKeyboardButton("🎯 Дартс", callback_data="darts")],
        [InlineKeyboardButton("🪙 Монетка", callback_data="flip")],
        [InlineKeyboardButton("🎰 Слоты", callback_data="slots")],
        [InlineKeyboardButton("💣 Мины", callback_data="mines")],
        [InlineKeyboardButton("🎳 Кегли", callback_data="bowling")],
        [InlineKeyboardButton("⬅ Назад", callback_data="menu")],
    ])


def mines_keyboard(uid):
    game = mines_games[uid]
    kb = []

    for i in range(0, 25, 5):
        row = []
        for j in range(5):
            idx = i + j
            if game["opened"][idx]:
                row.append(InlineKeyboardButton(game["grid"][idx], callback_data="noop"))
            else:
                row.append(InlineKeyboardButton("❓", callback_data=f"mine_{idx}"))
        kb.append(row)

    kb.append([
        InlineKeyboardButton(f"💰 Забрать x{round(game['mult'],2)}", callback_data="mine_cashout")
    ])

    return InlineKeyboardMarkup(kb)


def generate_mines(uid, bet):
    grid = ["💣"] * 10 + ["💎"] * 15
    random.shuffle(grid)

    mines_games[uid] = {
        "grid": grid,
        "opened": [False] * 25,
        "bet": bet,
        "mult": 1.0,
    }


# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, user.username or "")
    await update.message.reply_text("🎰 CASINO BOT", reply_markup=menu())


# ---------- SEND ----------
async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text("/send @user сумма")
        return

    name = context.args[0].lstrip("@")

    try:
        amount = int(context.args[1])
    except:
        await update.message.reply_text("❌ число")
        return

    sender = get_user(user.id, user.username or "")
    target = get_by_username(name)

    if not target:
        await update.message.reply_text("❌ нет юзера")
        return

    if sender[2] < amount:
        await update.message.reply_text("❌ мало денег")
        return

    set_balance(user.id, sender[2] - amount)
    set_balance(target[0], target[2] + amount)

    await update.message.reply_text(f"💸 отправлено {amount} @{name}")

    try:
        await context.bot.send_message(
            chat_id=target[0],
            text=f"💰 вам пришло {amount} от @{user.username}",
        )
    except:
        pass


# ---------- PvP (ОДИНАКОВЫЕ КУБЫ) ----------
async def pvp(update, context):
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text("/pvp @user ставка")
        return

    name = context.args[0].lstrip("@")
    bet = int(context.args[1])

    target = get_by_username(name)
    if not target:
        await update.message.reply_text("нет игрока")
        return

    dice = random.randint(1, 6)
    dice2 = random.randint(1, 6)

    msg = (
        f"⚔ PvP\n"
        f"{user.username}: {dice}\n"
        f"{name}: {dice2}\n"
    )

    if dice > dice2:
        msg += "🏆 победил ты"
    elif dice2 > dice:
        msg += "💀 проиграл ты"
    else:
        msg += "🤝 ничья"

    await update.message.reply_text(msg)


# ---------- CALLBACK ----------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    data = q.data

    await q.answer()
    cur_user = get_user(user.id, user.username or "")

    if data == "menu":
        await q.edit_message_text("🎰 CASINO", reply_markup=menu())

    elif data == "games":
        await q.edit_message_text("🎮", reply_markup=games())

    elif data == "bal":
        await q.edit_message_text(f"💰 {cur_user[2]}", reply_markup=menu())

    elif data == "send":
        await q.edit_message_text("/send @user сумма", reply_markup=menu())

    elif data == "bonus":
        ok, rem = claim_bonus(user.id)
        await q.edit_message_text("🎁 +50" if ok else f"жди {rem}s", reply_markup=menu())

    elif data == "mines":
        generate_mines(user.id, 10)
        await q.edit_message_text("💣 mines", reply_markup=mines_keyboard(user.id))

    elif data.startswith("mine_"):
        idx = int(data.split("_")[1])
        game = mines_games.get(user.id)

        if not game:
            return

        game["opened"][idx] = True

        if game["grid"][idx] == "💣":
            await q.edit_message_text("💥 проиграл", reply_markup=menu())
            mines_games.pop(user.id)
        else:
            game["mult"] += 0.3
            await q.edit_message_reply_markup(reply_markup=mines_keyboard(user.id))


# ---------- RUN ----------
TOKEN = os.getenv("BOT_TOKEN")
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("send", send_command))
app.add_handler(CommandHandler("pvp", pvp))
app.add_handler(CallbackQueryHandler(cb))

app.run_polling()
