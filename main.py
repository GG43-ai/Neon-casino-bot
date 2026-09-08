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

# ---------------- CONFIG ----------------
TOKEN = os.getenv("BOT_TOKEN")
ADMIN = "Legendjau2"

# ---------------- DB ----------------
conn = sqlite3.connect("casino.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance INTEGER DEFAULT 100,
    last_daily INTEGER DEFAULT 0,
    last_bonus INTEGER DEFAULT 0
)
""")
conn.commit()

# ---------------- STATE ----------------
user_custom = {}
basket_pending = {}
coin_pending = {}
mines_games = {}
pvp_requests = {}
next_pvp_id = 1


# ---------------- USERS ----------------
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

    return cur.fetchone()


def set_balance(uid, balance):
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, uid))
    conn.commit()


def get_by_username(name):
    cur.execute("SELECT * FROM users WHERE username=?", (name,))
    return cur.fetchone()


def user_label(uid, username=""):
    return f"@{username}" if username else str(uid)


# ---------------- BONUS ----------------
def claim_bonus(uid):
    now = int(time.time())
    cur.execute("SELECT last_bonus FROM users WHERE user_id=?", (uid,))
    row = cur.fetchone()

    last = row[0] if row else 0
    cooldown = 24 * 60 * 60

    if now - last < cooldown:
        return False, cooldown - (now - last)

    cur.execute(
        "UPDATE users SET balance = balance + 50, last_bonus=? WHERE user_id=?",
        (now, uid),
    )
    conn.commit()

    return True, 0


def format_time(sec):
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}ч {m}м" if h else f"{m}м {s}с"


# ---------------- TOP ----------------
def top10():
    cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    rows = cur.fetchall()

    text = "🏆 TOP 10\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. @{r[0] or 'no_name'} — {r[1]}\n"
    return text


# ---------------- KEYBOARDS ----------------
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        [InlineKeyboardButton("⚔ PvP", callback_data="pvp")],
        [InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("💰 Баланс", callback_data="bal")],
        [InlineKeyboardButton("🎁 Бонус", callback_data="bonus")],
    ])


def games_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 Баскет", callback_data="game_basket")],
        [InlineKeyboardButton("⚽ Футбол", callback_data="game_football")],
        [InlineKeyboardButton("🎯 Дартс", callback_data="game_darts")],
        [InlineKeyboardButton("🪙 Монетка", callback_data="game_flip")],
        [InlineKeyboardButton("🎰 Слоты", callback_data="game_slots")],
        [InlineKeyboardButton("💣 Мины", callback_data="game_mines")],
        [InlineKeyboardButton("⬅ Назад", callback_data="menu")],
    ])


def bets(game):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("10", callback_data=f"bet_{game}_10"),
            InlineKeyboardButton("50", callback_data=f"bet_{game}_50"),
        ],
        [
            InlineKeyboardButton("100", callback_data=f"bet_{game}_100"),
            InlineKeyboardButton("✏ Своя", callback_data=f"custom_{game}"),
        ],
        [InlineKeyboardButton("⬅", callback_data="games")],
    ])


def coin_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🦅 Орёл", callback_data="coin_heads"),
            InlineKeyboardButton("🪙 Решка", callback_data="coin_tails"),
        ]
    ])


# ---------------- MINES ----------------
def start_mines(uid, bet):
    grid = ["💣"] * 10 + ["💎"] * 15
    random.shuffle(grid)

    mines_games[uid] = {
        "grid": grid,
        "opened": [False] * 25,
        "bet": bet,
        "mult": 1.0,
    }


def mines_kb(uid):
    g = mines_games[uid]
    kb = []

    for i in range(0, 25, 5):
        row = []
        for j in range(5):
            idx = i + j
            if g["opened"][idx]:
                row.append(InlineKeyboardButton(g["grid"][idx], callback_data="x"))
            else:
                row.append(InlineKeyboardButton("❓", callback_data=f"mine_{idx}"))
        kb.append(row)

    kb.append([
        InlineKeyboardButton(f"💰 Забрать x{g['mult']:.2f}", callback_data="mine_cash")
    ])

    return InlineKeyboardMarkup(kb)


# ---------------- GAME LOGIC ----------------
async def play(message, user, game, amount, extra=None):
    u = get_user(user.id, user.username or "")

    if u[2] < amount:
        await message.reply_text("❌ нет денег")
        return

    balance = u[2] - amount

    # BASKET
    if game == "basket":
        dice = await message.reply_dice("🏀")
        await asyncio.sleep(2)

        win = dice.dice.value >= 4
        if extra == "hit" and win or extra == "miss" and not win:
            balance += amount * 2
            txt = "WIN"
        else:
            txt = "LOSE"

    # FLIP
    elif game == "flip":
        actual = random.choice(["heads", "tails"])
        if actual == extra:
            balance += amount * 2
            txt = "WIN"
        else:
            txt = "LOSE"

    # DEFAULT SIMPLE
    else:
        dice = await message.reply_dice("🎲")
        await asyncio.sleep(2)
        if dice.dice.value >= 4:
            balance += amount * 2
            txt = "WIN"
        else:
            txt = "LOSE"

    set_balance(user.id, balance)
    await message.reply_text(f"{game.upper()} {txt}", reply_markup=menu())


# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id, update.effective_user.username or "")
    await update.message.reply_text("🎰 CASINO", reply_markup=menu())


# ---------------- CALLBACK ----------------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = q.from_user
    data = q.data
    await q.answer()

    user = get_user(u.id, u.username or "")

    # MENU
    if data == "menu":
        await q.edit_message_text("🎰 CASINO", reply_markup=menu())

    elif data == "games":
        await q.edit_message_text("🎮 Игры", reply_markup=games_menu())

    elif data == "bal":
        await q.edit_message_text(f"💰 {user[2]}", reply_markup=menu())

    elif data == "top":
        await q.edit_message_text(top10(), reply_markup=menu())

    # BET SELECTION
    elif data.startswith("bet_"):
        _, game, amount = data.split("_")
        await play(q.message, u, game, int(amount))

    # CUSTOM BET
    elif data.startswith("custom_"):
        user_custom[u.id] = data.split("_")[1]
        await q.message.reply_text("Введи сумму")

    # GAMES MENU
    elif data.startswith("game_"):
        g = data.split("_")[1]
        await q.edit_message_text("💸 ставка", reply_markup=bets(g))

    # FLIP
    elif data in ["coin_heads", "coin_tails"]:
        await play(q.message, u, "flip", 10, "heads" if data == "coin_heads" else "tails")

    # MINES
    elif data.startswith("mine_"):
        idx = int(data.split("_")[1])
        g = mines_games.get(u.id)
        if not g:
            return

        g["opened"][idx] = True

        if g["grid"][idx] == "💣":
            mines_games.pop(u.id)
            await q.edit_message_text("💥 BOMB")
        else:
            g["mult"] += 0.3
            await q.edit_message_reply_markup(mines_kb(u.id))

    elif data == "mine_cash":
        g = mines_games.pop(u.id, None)
        if not g:
            return

        udata = get_user(u.id)
        set_balance(u.id, udata[2] + int(g["bet"] * g["mult"]))

        await q.edit_message_text("💰 CASHOUT", reply_markup=menu())


# ---------------- RUN ----------------
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(cb))

app.run_polling()
