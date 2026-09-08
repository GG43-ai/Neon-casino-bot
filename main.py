import asyncio
import os
import random
import sqlite3
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
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
bet_state = {}
custom_bet_state = {}
pvp_requests = {}

# ---------- USERS ----------
def get_user(uid, username=""):
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    u = cur.fetchone()

    if not u:
        cur.execute("INSERT INTO users (user_id, username, balance, last_bonus) VALUES (?, ?, 100, 0)",
                    (uid, username))
        conn.commit()
        return get_user(uid, username)

    if username and u[1] != username:
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


# ---------- MENUS ----------
def menu(u):
    kb = [
        [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        [InlineKeyboardButton("⚔ PvP", callback_data="pvp")],
        [InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("💰 Баланс", callback_data="bal")],
        [InlineKeyboardButton("🎁 Бонус", callback_data="bonus")]
    ]

    if u == ADMIN:
        kb.append([InlineKeyboardButton("👑 Admin", callback_data="admin")])

    return InlineKeyboardMarkup(kb)


def games_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚽ Футбол", callback_data="game_football"),
         InlineKeyboardButton("🏀 Баскет", callback_data="game_basket")],

        [InlineKeyboardButton("🎯 Дартс", callback_data="game_darts"),
         InlineKeyboardButton("🎰 Слоты", callback_data="game_slots")],

        [InlineKeyboardButton("💣 Мины", callback_data="mines")],
        [InlineKeyboardButton("⬅ Назад", callback_data="menu")]
    ])


def bet_menu(game):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("10", callback_data=f"bet_{game}_10"),
         InlineKeyboardButton("50", callback_data=f"bet_{game}_50")],
        [InlineKeyboardButton("100", callback_data=f"bet_{game}_100"),
         InlineKeyboardButton("200", callback_data=f"bet_{game}_200")],
        [InlineKeyboardButton("500", callback_data=f"bet_{game}_500")],
        [InlineKeyboardButton("💸 ALL IN", callback_data=f"bet_{game}_all")],
        [InlineKeyboardButton("✍️ СВОЯ", callback_data=f"bet_{game}_custom")]
    ])


# ---------- TELEGRAM DICE ----------
async def roll(chat_id, emoji, context):
    msg = await context.bot.send_dice(chat_id=chat_id, emoji=emoji)
    return msg.dice.value


# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    get_user(u.id, u.username or "")
    await update.message.reply_text("🎰 CASINO PRO", reply_markup=menu(u.username))


# ---------- BAL ----------
async def bal(update, context):
    u = update.effective_user
    user = get_user(u.id, u.username or "")
    await update.message.reply_text(f"💰 {user[2]}")


# ---------- TOP ----------
async def top(update, context):
    cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    rows = cur.fetchall()

    text = "🏆 TOP:\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. @{r[0]} — {r[1]}\n"

    await update.message.reply_text(text)


# ---------- PvP (REAL DICE) ----------
async def pvp(update, context):
    u = update.effective_user

    if len(context.args) < 2:
        return await update.message.reply_text("/pvp @user bet")

    name = context.args[0].lstrip("@")
    bet = context.args[1]

    sender = get_user(u.id, u.username or "")
    target = get_by_username(name)

    if not target:
        return await update.message.reply_text("нет игрока")

    if bet == "all":
        bet = sender[2]
    else:
        bet = int(bet)

    if sender[2] < bet:
        return await update.message.reply_text("нет денег")

    set_balance(u.id, sender[2] - bet)

    d1 = await roll(u.id, "🎲", context)
    d2 = await roll(target[0], "🎲", context)

    text = f"⚔ PvP\n@{u.username} 🎲 {d1}\n@{name} 🎲 {d2}\n"

    if d1 > d2:
        set_balance(u.id, sender[2] + bet * 2)
        text += "🏆 ты победил"
    elif d2 > d1:
        set_balance(target[0], target[2] + bet * 2)
        text += "💀 ты проиграл"
    else:
        set_balance(u.id, sender[2] + bet)

    await context.bot.send_message(u.id, text)
    await context.bot.send_message(target[0], text)


# ---------- CALLBACK ----------
async def cb(update, context):
    q = update.callback_query
    u = q.from_user
    data = q.data

    await q.answer()
    user = get_user(u.id, u.username or "")

    if data == "menu":
        await q.edit_message_text("🎰 MENU", reply_markup=menu(u.username))

    elif data == "games":
        await q.edit_message_text("🎮", reply_markup=games_menu())

    elif data == "bal":
        await q.edit_message_text(f"💰 {user[2]}")

    # ---------- BET ----------
    elif data.startswith("bet_"):
        _, game, amount = data.split("_")

        if amount == "custom":
            custom_bet_state[u.id] = game
            return await q.edit_message_text("✍️ напиши сумму")

        if amount == "all":
            amount = user[2]
        else:
            amount = int(amount)

        bet_state[u.id] = (game, amount)
        await q.edit_message_text(f"🎮 {game} ставка {amount}", reply_markup=menu(u.username))

    # ---------- FOOTBALL ----------
    elif data == "game_football":
        await q.edit_message_text("⚽ ставка", reply_markup=bet_menu("football"))

    elif data.startswith("football_play"):
        val = await roll(u.id, "⚽", context)
        win = val >= 4

        game, bet = bet_state.get(u.id, (None, 0))
        if win:
            set_balance(u.id, user[2] + bet)
        else:
            set_balance(u.id, user[2] - bet)

        await q.edit_message_text("⚽ результат")

    # ---------- BASKET ----------
    elif data == "game_basket":
        await q.edit_message_text("🏀 залетит/мимо", reply_markup=bet_menu("basket"))

    elif data.startswith("basket_play"):
        val = await roll(u.id, "🏀", context)
        win = val >= 4

        game, bet = bet_state.get(u.id, (None, 0))

        if win:
            set_balance(u.id, user[2] + bet)
        else:
            set_balance(u.id, user[2] - bet)

        await q.edit_message_text("🏀 done")

    # ---------- DARTS ----------
    elif data == "game_darts":
        await q.edit_message_text("🎯", reply_markup=bet_menu("darts"))

    elif data.startswith("darts_play"):
        val = await roll(u.id, "🎯", context)

        game, bet = bet_state.get(u.id, (None, 0))

        mult = 0
        if val == 6:
            mult = 3
        elif val >= 4:
            mult = 2

        set_balance(u.id, user[2] + bet * mult)
        await q.edit_message_text("🎯 done")

    # ---------- SLOTS ----------
    elif data == "game_slots":
        await q.edit_message_text("🎰", reply_markup=bet_menu("slots"))

    elif data.startswith("slots_play"):
        val = await roll(u.id, "🎰", context)

        r1 = random.randint(1, 6)
        r2 = random.randint(1, 6)
        r3 = val

        game, bet = bet_state.get(u.id, (None, 0))

        if r1 == r2 == r3 == 7:
            mult = 4
        elif r1 == r2 == r3:
            mult = 2
        else:
            mult = 0

        set_balance(u.id, user[2] + bet * mult)
        await q.edit_message_text("🎰 slots done")

    # ---------- MINES ----------
    elif data == "mines":
        await q.edit_message_text("💣 mines (simplified)")


# ---------- RUN ----------
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("pvp", pvp))
app.add_handler(CommandHandler("balance", bal))
app.add_handler(CommandHandler("top", top))
app.add_handler(CallbackQueryHandler(cb))

app.run_polling()
