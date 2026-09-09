import asyncio
import os
import random
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
mines_games = {}
pending_bets = {}  # Зберігає тимчасовий вибір гри та ставки


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


def top10():
    cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    rows = cur.fetchall()

    text = "🏆 TOP 10\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. @{r[0] or 'без_ніка'} — {r[1]}\n"
    return text


# ---------- MENUS ----------
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        [InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("💰 Баланс", callback_data="bal")],
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
            InlineKeyboardButton("500", callback_data=f"bet_{game}_500"),
        ],
        [InlineKeyboardButton("⬅ Назад", callback_data="games")],
    ])


def basket_choice_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 Залетит (x2.5)", callback_data="bchoice_in")],
        [InlineKeyboardButton("❌ Мимо (x1.8)", callback_data="bchoice_miss")],
    ])


def flip_choice_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Орел (x1.9)", callback_data="fchoice_heads")],
        [InlineKeyboardButton("🪙 Решка (x1.9)", callback_data="fchoice_tails")],
    ])


# ---------- MINES ----------
def generate_mines(uid, bet):
    grid = ["💣"] * 5 + ["💎"] * 20  # 5 бомб, 20 кристаллов
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
                        game["grid"][index],
                        callback_data="noop",
                    )
                )
            else:
                row.append(
                    InlineKeyboardButton("❓", callback_data=f"mine_{index}")
                )
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(
            f"💰 Забрать x{round(game['mult'], 2)}",
            callback_data="mine_cashout",
        )
    ])

    return InlineKeyboardMarkup(keyboard)


# ---------- START ----------
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
        await query.edit_message_text("🎮 Выберите игру:", reply_markup=games())

    elif data == "bal":
        await query.edit_message_text(
            f"💰 Ваш баланс: {current_user[2]}",
            reply_markup=menu(),
        )

    elif data == "top":
        await query.edit_message_text(top10(), reply_markup=menu())

    # ---------- GAMES SELECTION ----------
    elif data in {"basket", "football", "darts", "flip", "slots", "bowling", "mines"}:
        await query.edit_message_text("💸 Выберите ставку:", reply_markup=bets(data))

    elif data.startswith("bet_"):
        _, game, amount = data.split("_")
        amount = int(amount)

        if current_user[2] < amount:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=menu())
            return

        # Гра «Мини»
        if game == "mines":
            set_balance(user.id, current_user[2] - amount)
            generate_mines(user.id, amount)
            await query.edit_message_text(
                "💣 Поле заминировано! Открывайте ячейки:",
                reply_markup=mines_keyboard(user.id),
            )
            return

        # Баскет і Монетка вимагають додаткового вибору
        if game == "basket":
            pending_bets[user.id] = {"game": "basket", "amount": amount}
            await query.edit_message_text("🏀 Куда попадет мяч?", reply_markup=basket_choice_menu())
            return

        if game == "flip":
            pending_bets[user.id] = {"game": "flip", "amount": amount}
            await query.edit_message_text("🪙 Выберите сторону:", reply_markup=flip_choice_menu())
            return

        # Для решти ігор запускаємо одразу
        await query.delete_message()
        await play_game(query.message.chat_id, context, user, game, amount)

    # ---------- BASKET CHOICE ----------
    elif data.startswith("bchoice_"):
        choice = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)

        if not bet_info:
            await query.edit_message_text("❌ Сессия истекла.", reply_markup=menu())
            return

        await query.delete_message()
        await play_basket(query.message.chat_id, context, user, bet_info["amount"], choice)

    # ---------- FLIP CHOICE ----------
    elif data.startswith("fchoice_"):
        choice = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)

        if not bet_info:
            await query.edit_message_text("❌ Сессия истекла.", reply_markup=menu())
            return

        await query.delete_message()
        await play_flip(query.message.chat_id, context, user, bet_info["amount"], choice)

    # ---------- MINES GAME LOGIC ----------
    elif data.startswith("mine_") and data != "mine_cashout":
        index = int(data.split("_")[1])
        game = mines_games.get(user.id)

        if not game or game["opened"][index]:
            return

        game["opened"][index] = True

        if game["grid"][index] == "💣":
            mines_games.pop(user.id)
            await query.edit_message_text(
                f"💥 БОМБА! Вы подорвались!\n❌ Потеряно: {game['bet']}",
                reply_markup=menu(),
            )
        else:
            game["mult"] += 0.25
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
            f"💰 Забрано x{round(game['mult'], 2)}!\n🎉 Выигрыш: +{reward}",
            reply_markup=menu(),
        )


# ---------- SPECIAL GAMES LOGIC ----------
async def play_basket(chat_id, context, user, amount, choice):
    current_user = get_user(user.id, user.username or "")
    if current_user[2] < amount:
        await context.bot.send_message(chat_id, "❌ Недостаточно денег!")
        return

    # Знімаємо ставку
    set_balance(user.id, current_user[2] - amount)

    dice_msg = await context.bot.send_dice(chat_id, emoji="🏀")
    await asyncio.sleep(3.5)

    value = dice_msg.dice.value
    # Значення Telegram: 4, 5 — забито в кошик; 1, 2, 3 — мимо
    is_in = value in [4, 5]

    win = False
    coef = 0

    if choice == "in" and is_in:
        win = True
        coef = 2.5
    elif choice == "miss" and not is_in:
        win = True
        coef = 1.8

    new_bal = get_user(user.id)[2]
    if win:
        reward = int(amount * coef)
        set_balance(user.id, new_bal + reward)
        text = f"🎉 ВЫИГРЫШ!\nРезультат: {'Залетело! 🗑️' if is_in else 'Мимо! ❌'}\n💰 +{reward}"
    else:
        text = f"❌ ПРОИГРЫШ!\nРезультат: {'Залетело! 🗑️' if is_in else 'Мимо! ❌'}\n💸 -{amount}"

    await context.bot.send_message(chat_id, text, reply_markup=menu())


async def play_flip(chat_id, context, user, amount, choice):
    current_user = get_user(user.id, user.username or "")
    if current_user[2] < amount:
        await context.bot.send_message(chat_id, "❌ Недостаточно денег!")
        return

    set_balance(user.id, current_user[2] - amount)

    msg = await context.bot.send_message(chat_id, "🪙 Монетка крутится...")
    await asyncio.sleep(1.5)

    result = random.choice(["heads", "tails"])
    res_text = "Орел 🪙" if result == "heads" else "Решка 🪙"

    new_bal = get_user(user.id)[2]
    if choice == result:
        reward = int(amount * 1.9)
        set_balance(user.id, new_bal + reward)
        text = f"🎉 ВЫИГРЫШ!\nВыпал: {res_text}\n💰 +{reward}"
    else:
        text = f"❌ ПРОИГРЫШ!\nВыпал: {res_text}\n💸 -{amount}"

    await msg.edit_text(text, reply_markup=menu())


# ---------- STANDARD GAMES LOGIC ----------
async def play_game(chat_id, context, user, game, amount):
    current_user = get_user(user.id, user.username or "")

    if current_user[2] < amount:
        await context.bot.send_message(chat_id, "❌ Недостаточно денег!")
        return

    set_balance(user.id, current_user[2] - amount)

    emoji_map = {
        "football": "⚽",
        "darts": "🎯",
        "slots": "🎰",
        "bowling": "🎳",
    }

    dice_msg = await context.bot.send_dice(chat_id, emoji=emoji_map[game])
    await asyncio.sleep(3.5)
    val = dice_msg.dice.value

    coef = 0

    # Оцінка результатів стандартних анімацій Telegram
    if game == "football":
        # 3, 4, 5 - гол
        if val in [3, 4, 5]:
            coef = 2.0

    elif game == "darts":
        if val == 6:  # Яблучко
            coef = 3.0
        elif val in [4, 5]:  # Влучання
            coef = 1.5

    elif game == "bowling":
        if val == 6:  # Страйк
            coef = 3.0
        elif val in [3, 4, 5]:  # Збито кілька
            coef = 1.5

    elif game == "slots":
        # 64 - Три 777 (Джекпот)
        # 1, 22, 43 - Три однакові картинки
        if val == 64:
            coef = 5.0
        elif val in [1, 22, 43]:
            coef = 3.0

    new_bal = get_user(user.id)[2]
    if coef > 0:
        reward = int(amount * coef)
        set_balance(user.id, new_bal + reward)
        text = f"🎉 ВЫИГРЫШ (x{coef})!\n💰 +{reward}"
    else:
        text = f"❌ ПРОИГРЫШ!\n💸 -{amount}"

    await context.bot.send_message(chat_id, text, reply_markup=menu())


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
        await update.message.reply_text("❌ Пользователь не найден")
        return

    set_balance(target[0], target[2] + amount)
    await update.message.reply_text(f"✅ Выдано +{amount} пользователю @{username}")


# ---------- RUN ----------
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("add", add))
app.add_handler(CallbackQueryHandler(cb))

app.run_polling()
