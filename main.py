import os
import random
import sqlite3
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN = "legendjau2"

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
conn.commit()


# ---------- USERS ----------
def get_user(uid, username=""):
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    user = cur.fetchone()

    if not user:
        cur.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (uid, username))
        conn.commit()
        return get_user(uid, username)

    if username and user[1] != username:
        cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
        conn.commit()
        return get_user(uid, username)

    return user


def set_balance(uid, bal):
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (bal, uid))
    conn.commit()


def get_by_username(name):
    cur.execute("SELECT * FROM users WHERE username=?", (name,))
    return cur.fetchone()


# ---------- MENU ----------
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        [InlineKeyboardButton("💰 Баланс", callback_data="bal")],
        [InlineKeyboardButton("⚔ PvP", callback_data="pvp")],
    ])


def games_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 Баскет", callback_data="game_basket")],
        [InlineKeyboardButton("⚽ Футбол", callback_data="game_football")],
        [InlineKeyboardButton("🎯 Дартс", callback_data="game_darts")],
        [InlineKeyboardButton("🪙 Монета", callback_data="game_flip")],
        [InlineKeyboardButton("🎰 Слоты", callback_data="game_slots")],
        [InlineKeyboardButton("⬅ Назад", callback_data="menu")],
    ])


# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id, update.effective_user.username or "")
    await update.message.reply_text("🎰 CASINO BOT", reply_markup=menu())


# ---------- ADMIN ----------
async def give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.effective_user.username or "").lower() != ADMIN:
        return

    if len(context.args) < 2:
        await update.message.reply_text("/give user_id amount")
        return

    uid = int(context.args[0])
    amount = int(context.args[1])

    user = get_user(uid)
    set_balance(uid, user[2] + amount)

    await update.message.reply_text("✅ начислено")


# ---------- SEND MONEY ----------
async def send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text("/send @user amount")
        return

    name = context.args[0].lstrip("@")
    amount = int(context.args[1])

    sender = get_user(user.id, user.username or "")
    target = get_by_username(name)

    if not target:
        await update.message.reply_text("❌ не найден")
        return

    if sender[2] < amount:
        await update.message.reply_text("❌ нет денег")
        return

    set_balance(sender[0], sender[2] - amount)
    set_balance(target[0], target[2] + amount)

    await update.message.reply_text("✅ перевод выполнен")


# ---------- DM ----------
async def dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return

    name = context.args[0].lstrip("@")
    text = " ".join(context.args[1:])

    target = get_by_username(name)
    if not target:
        return

    try:
        await context.bot.send_message(target[0], f"💌 {text}")
        await update.message.reply_text("✅ отправлено")
    except:
        await update.message.reply_text("❌ ошибка")


# ---------- CALLBACK ----------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    data = q.data
    current = get_user(user.id, user.username or "")

    # MENU
    if data == "menu":
        await q.edit_message_text("🎰 CASINO BOT", reply_markup=menu())

    elif data == "games":
        await q.edit_message_text("🎮 Игры", reply_markup=games_menu())

    elif data == "bal":
        await q.edit_message_text(f"💰 Баланс: {current[2]}", reply_markup=menu())

    # ---------- FIXED GAMES ----------
    elif data.startswith("game_"):
        game = data.split("_")[1]
        await play_game(q.message, user, game)


# ---------- GAME LOGIC ----------
async def play_game(message, user, game):
    user_db = get_user(user.id, user.username or "")
    bet = 10

    if user_db[2] < bet:
        await message.reply_text("❌ нет денег")
        return

    balance = user_db[2] - bet

    if game == "basket":
        res = random.choice([True, False])
        if res:
            balance += bet * 2
            txt = "🏀 WIN"
        else:
            txt = "🏀 LOSE"

    elif game == "football":
        res = random.choice([True, False])
        if res:
            balance += bet * 2
            txt = "⚽ GOAL"
        else:
            txt = "⚽ MISS"

    elif game == "darts":
        res = random.randint(1, 6)
        if res == 6:
            balance += bet * 3
            txt = "🎯 CENTER"
        else:
            txt = "🎯 MISS"

    elif game == "flip":
        res = random.choice(["heads", "tails"])
        if random.choice(["heads", "tails"]) == res:
            balance += bet * 2
            txt = "🪙 WIN"
        else:
            txt = "🪙 LOSE"

    elif game == "slots":
        if random.randint(1, 10) == 10:
            balance += bet * 5
            txt = "🎰 JACKPOT"
        else:
            txt = "🎰 LOSE"

    else:
        txt = "❌ error"
        balance = user_db[2]

    set_balance(user.id, balance)
    await message.reply_text(txt, reply_markup=menu())


# ---------- RUN ----------
if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("give", give))
app.add_handler(CommandHandler("send", send))
app.add_handler(CommandHandler("dm", dm))
app.add_handler(CallbackQueryHandler(cb))

app.run_polling()
