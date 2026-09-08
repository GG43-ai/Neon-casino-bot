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
        cur.execute(
            "UPDATE users SET username=? WHERE user_id=?",
            (username, uid),
        )
        conn.commit()
        user = (user[0], username, *user[2:])

    return user


def set_balance(uid, balance):
    cur.execute(
        "UPDATE users SET balance=? WHERE user_id=?",
        (balance, uid),
    )
    conn.commit()


def get_by_username(name):
    cur.execute("SELECT * FROM users WHERE username=?", (name,))
    return cur.fetchone()


def user_label(user_id, username=""):
    return f"@{username}" if username else str(user_id)


def claim_bonus(uid):
    now = int(time.time())

    cur.execute(
        "SELECT last_daily, last_bonus FROM users WHERE user_id=?",
        (uid,),
    )
    row = cur.fetchone()

    last_bonus = max((value or 0) for value in row) if row else 0

    cooldown = 24 * 60 * 60
    remaining = cooldown - (now - last_bonus)

    if remaining > 0:
        return False, remaining

    cur.execute(
        """
        UPDATE users
        SET balance = balance + 50,
            last_daily = ?,
            last_bonus = ?
        WHERE user_id = ?
        """,
        (now, now, uid),
    )
    conn.commit()

    return True, 0


def format_remaining(seconds):
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours} ч. {minutes} мин."
    if minutes:
        return f"{minutes} мин."
    return f"{seconds} сек."


def parse_amount(args):
    if len(args) < 2:
        return None

    try:
        amount = int(args[1])
    except (TypeError, ValueError):
        return None

    return amount if amount > 0 else None


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
        [InlineKeyboardButton("⬅ Назад", callback_data="games")],
    ])


def basket_predictions():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏀 ЗАЛЕТИТ", callback_data="basket_hit"),
            InlineKeyboardButton("❌ МИМО", callback_data="basket_miss"),
        ],
        [InlineKeyboardButton("⬅ Назад", callback_data="games")],
    ])


def coin_predictions():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🦅 ОРЁЛ", callback_data="flip_heads"),
            InlineKeyboardButton("🪙 РЕШКА", callback_data="flip_tails"),
        ],
        [InlineKeyboardButton("⬅ Назад", callback_data="games")],
    ])


# ---------- MINES ----------
def generate_mines(uid, bet):
    grid = ["💣"] * 15 + ["💎"] * 10
    random.shuffle(grid)

    mines_games[uid] = {
        "grid": grid,
        "opened": [False] * 25,
        "bet": bet,
        "mult": 1.0,
        "alive": True,
    }


def mines_keyboard(uid):
    game = mines_games[uid]
    keyboard = []

    for row_start in range(0, 25, 5):
        row = []

        for column in range(5):
            index = row_start + column

            if game["opened"][index]:
                row.append(InlineKeyboardButton(game["grid"][index] * 2, callback_data="noop"))
            else:
                row.append(InlineKeyboardButton("❓❓", callback_data=f"mine_{index}"))

        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(
            f"💰 Забрать x{round(game['mult'], 2)}",
            callback_data="mine_cashout",
        )
    ])

    return InlineKeyboardMarkup(keyboard)


# ---------- PVP ----------
def pvp_keyboard(request_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Принять", callback_data=f"pvp_accept_{request_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"pvp_decline_{request_id}"),
        ]
    ])
# ---------- MINES ----------
def generate_mines(uid, bet):
    grid = ["💣"] * 15 + ["💎"] * 10
    random.shuffle(grid)

    mines_games[uid] = {
        "grid": grid,
        "opened": [False] * 25,
        "bet": bet,
        "mult": 1.0,
        "alive": True,
    }


def mines_keyboard(uid):
    game = mines_games[uid]
    keyboard = []

    for row_start in range(0, 25, 5):
        row = []
        for col in range(5):
            index = row_start + col

            if game["opened"][index]:
                row.append(
                    InlineKeyboardButton(
                        game["grid"][index] * 2,
                        callback_data="noop",
                    )
                )
            else:
                row.append(
                    InlineKeyboardButton("❓❓", callback_data=f"mine_{index}")
                )
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(
            f"💰 Забрать x{round(game['mult'], 2)}",
            callback_data="mine_cashout",
        )
    ])

    return InlineKeyboardMarkup(keyboard)


# ---------- PVP ----------
def pvp_keyboard(request_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Принять", callback_data=f"pvp_accept_{request_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"pvp_decline_{request_id}"),
        ]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, user.username or "")
    await update.message.reply_text("🎰 NEON CASINO", reply_markup=menu())


# ---------- CALLBACK ----------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    await query.answer()

    current_user = get_user(user.id, user.username or "")

    # MENU
    if data == "menu":
        await query.edit_message_text("🎰 NEON CASINO", reply_markup=menu())

    elif data == "games":
        await query.edit_message_text("🎮 Игры", reply_markup=games())

    elif data == "bal":
        await query.edit_message_text(
            f"💰 Баланс: {current_user[2]}",
            reply_markup=menu(),
        )

    elif data == "top":
        await query.edit_message_text(top10(), reply_markup=menu())

    elif data == "bonus":
        await query.edit_message_text("🎁 Бонус пока не реализован", reply_markup=menu())

    # ---------- GAMES ----------
    elif data in {"basket", "football", "darts", "flip", "slots", "bowling", "mines"}:
        await query.edit_message_text("💸 ставка", reply_markup=bets(data))

    elif data.startswith("bet_"):
        _, game, amount = data.split("_")
        amount = int(amount)

        if current_user[2] < amount:
            await query.edit_message_text("❌ нет денег", reply_markup=menu())
            return

        if game == "mines":
            balance = current_user[2] - amount
            set_balance(user.id, balance)
            generate_mines(user.id, amount)
            await query.edit_message_text(
                "💣 Mines started",
                reply_markup=mines_keyboard(user.id),
            )
            return

        await play_game(query.message, user, game, amount)

    elif data.startswith("mine_") and data != "mine_cashout":
        index = int(data.split("_")[1])
        game = mines_games.get(user.id)

        if not game or game["opened"][index]:
            return

        game["opened"][index] = True

        if game["grid"][index] == "💣":
            mines_games.pop(user.id)
            await query.edit_message_text(
                f"💥 БОМБА!\n❌ -{game['bet']}",
                reply_markup=menu(),
            )
        else:
            game["mult"] += 0.4
            await query.edit_message_reply_markup(mines_keyboard(user.id))

    elif data == "mine_cashout":
        game = mines_games.get(user.id)
        if not game:
            return

        reward = int(game["bet"] * game["mult"])
        current_user = get_user(user.id, user.username or "")
        set_balance(user.id, current_user[2] + reward)

        mines_games.pop(user.id)

        await query.edit_message_text(
            f"💰 x{round(game['mult'], 2)} → +{reward}",
            reply_markup=menu(),
        )


# ---------- GAME LOGIC ----------
async def play_game(message, user, game, amount):
    current_user = get_user(user.id, user.username or "")

    if amount <= 0:
        await message.reply_text("❌ ставка должна быть больше 0")
        return

    if current_user[2] < amount:
        await message.reply_text("❌ нет денег")
        return

    balance = current_user[2] - amount

    # ---------- BASKET ----------
    if game == "basket":
        dice = await message.reply_dice("🏀")
        await asyncio.sleep(2)

        win = dice.dice.value >= 4

        if win:
            balance += amount * 2
            text = f"🏀 WIN +{amount * 2}"
        else:
            text = f"🏀 LOSE -{amount}"

        set_balance(user.id, balance)
        await message.reply_text(text, reply_markup=menu())

    # ---------- FLIP ----------
    elif game == "flip":
        result = random.choice(["heads", "tails"])
        text = "🪙 ОРЁЛ" if result == "heads" else "🪙 РЕШКА"

        balance += amount * 2

        set_balance(user.id, balance)
        await message.reply_text(f"{text} WIN", reply_markup=menu())

    # ---------- DEFAULT ----------
    else:
        await message.reply_text("❌ игра не реализована")


# ---------- ADMIN ----------
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ADMIN:
        return

    if len(context.args) < 2:
        await update.message.reply_text("Использование: /add @user сумма")
        return

    username = context.args[0].lstrip("@")
    amount = int(context.args[1])

    target = get_by_username(username)
    if not target:
        await update.message.reply_text("❌ пользователь не найден")
        return

    set_balance(target[0], target[2] + amount)

    await update.message.reply_text(f"✅ +{amount} @{username}")


# ---------- RUN ----------
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("add", add))
app.add_handler(CallbackQueryHandler(cb))

app.run_polling()
