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

TOKEN = os.getenv("BOT_TOKEN")
ADMIN = "Legendjau2"

# ---------- DB ----------

conn = sqlite3.connect("casino.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
user_id INTEGER PRIMARY KEY,
username TEXT,
balance INTEGER DEFAULT 100
)
""")

try:
    cur.execute("ALTER TABLE users ADD COLUMN last_daily INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass

try:
    cur.execute("ALTER TABLE users ADD COLUMN last_bonus INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass

conn.commit()


# ---------- STATE ----------

user_custom = {}
basket_pending = {}
coin_pending = {}
mines_games = {}
pvp_requests = {}
next_pvp_id = 1


# ---------- USERS ----------

def get_user(uid, username=""):
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    user = cur.fetchone()

    if not user:
        cur.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?)",
            (uid, username),
        )
        conn.commit()
        return get_user(uid, username)

    if username and user[1] != username:
        cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
        conn.commit()
        user = (user[0], username, *user[2:])

    return user


def set_balance(uid, balance):
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, uid))
    conn.commit()


def get_by_username(name):
    cur.execute("SELECT * FROM users WHERE username=?", (name,))
    return cur.fetchone()


def user_label(user_id, username=""):
    return f"@{username}" if username else str(user_id)


# ---------- TOP ----------

def top10():
    cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    rows = cur.fetchall()

    text = "🏆 TOP 10\n\n"
    for index, row in enumerate(rows, 1):
        username = row[0] or "без_ника"
        text += f"{index}. @{username} — {row[1]}\n"

    return text


# ---------- MENUS ----------
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        [InlineKeyboardButton("⚔ PvP", callback_data="pvp")],
        [InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("💰 Баланс", callback_data="bal")],
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


# ---------- PVP FIXED CORE ----------
async def resolve_pvp(query, context, request_id):
    request = pvp_requests.get(request_id)

    if not request:
        await query.edit_message_text("❌ Этот вызов уже недействителен.")
        return

    if query.from_user.id != request["opponent_id"]:
        await query.answer("Это не твой вызов.", show_alert=True)
        return

    challenger = get_user(request["challenger_id"], request["challenger_username"])
    opponent = get_user(request["opponent_id"], request["opponent_username"])
    amount = request["amount"]

    if challenger[2] < amount or opponent[2] < amount:
        pvp_requests.pop(request_id, None)
        await query.edit_message_text("❌ Недостаточно средств.")
        return

    pvp_requests.pop(request_id, None)

    # списываем ставки
    set_balance(challenger[0], challenger[2] - amount)
    set_balance(opponent[0], opponent[2] - amount)

    challenger_label = user_label(challenger[0], challenger[1] or "")
    opponent_label = user_label(opponent[0], opponent[1] or "")

    await query.edit_message_text("⚔️ Дуэль начинается...")

    try:
        # 🎲 ОДИНАКОВЫЕ КУБИКИ В ОБА ЧАТА
        await context.bot.send_message(challenger[0], "🎲 Бросает кубик...")
        await context.bot.send_message(opponent[0], "🎲 Бросает кубик...")

        dice1 = await context.bot.send_dice(chat_id=challenger[0])
        dice2 = await context.bot.send_dice(chat_id=opponent[0])

        await asyncio.sleep(2)

        val1 = dice1.dice.value
        val2 = dice2.dice.value

    except TelegramError:
        set_balance(challenger[0], challenger[2] + amount)
        set_balance(opponent[0], opponent[2] + amount)
        await query.edit_message_text("❌ Ошибка PvP, ставки возвращены.")
        return

    # результат
    if val1 > val2:
        winner = challenger
        loser = opponent
    elif val2 > val1:
        winner = opponent
        loser = challenger
    else:
        set_balance(challenger[0], challenger[2] + amount)
        set_balance(opponent[0], opponent[2] + amount)

        text = f"🤝 Ничья!\n🎲 {val1} vs {val2}"
        await query.edit_message_text(text)
        await context.bot.send_message(challenger[0], text)
        await context.bot.send_message(opponent[0], text)
        return

    set_balance(winner[0], winner[2] + amount * 2)

    text = (
        f"🎲 {val1} vs {val2}\n"
        f"🏆 Победил: {user_label(winner[0], winner[1] or '')}\n"
        f"💰 +{amount * 2}"
    )

    await query.edit_message_text(text)
    await context.bot.send_message(challenger[0], text)
    await context.bot.send_message(opponent[0], text)


# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id, update.effective_user.username or "")
    await update.message.reply_text("🎰 NEON CASINO", reply_markup=menu())


# ---------- CALLBACK ----------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    await query.answer()
    current = get_user(user.id, user.username or "")

    if data == "menu":
        await query.edit_message_text("🎰 NEON CASINO", reply_markup=menu())

    elif data == "games":
        await query.edit_message_text("🎮 Игры", reply_markup=games())

    elif data == "bal":
        await query.edit_message_text(f"💰 Баланс: {current[2]}", reply_markup=menu())

    elif data == "top":
        await query.edit_message_text(top10(), reply_markup=menu())

    elif data.startswith("pvp_accept_"):
        request_id = int(data.split("_")[-1])
        await resolve_pvp(query, context, request_id)

    elif data.startswith("pvp_decline_"):
        await query.edit_message_text("❌ Отклонено")


# ---------- RUN ----------
if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(cb))

app.run_polling()
