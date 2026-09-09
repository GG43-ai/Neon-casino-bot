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
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USERNAME = "Legendjau2"

# ---------- DB SETUP ----------
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

# Перевірка наявності колонки last_bonus (якщо БД вже існувала)
try:
    cur.execute("ALTER TABLE users ADD COLUMN last_bonus INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass

conn.commit()

# ---------- STATE ----------
mines_games = {}
pending_bets = {}      # Тимчасові ставки (basket, flip)
awaiting_custom = {}  # Очікування введення власної ставки: {user_id: game_name}
awaiting_admin = {}   # Очікування дій адміна: {user_id: action_type}


# ---------- DB HELPERS ----------
def get_user(uid, username=""):
    cur.execute("SELECT user_id, username, balance, last_bonus FROM users WHERE user_id=?", (uid,))
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
        user = (user[0], username, user[2], user[3])

    return user


def set_balance(uid, balance):
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, uid))
    conn.commit()


def update_bonus_time(uid):
    now = int(time.time())
    cur.execute("UPDATE users SET last_bonus=? WHERE user_id=?", (now, uid))
    conn.commit()


def get_by_identifier(identifier):
    identifier = identifier.strip().lstrip("@")
    if identifier.isdigit():
        cur.execute("SELECT * FROM users WHERE user_id=?", (int(identifier),))
    else:
        cur.execute("SELECT * FROM users WHERE username=?", (identifier,))
    return cur.fetchone()


def top10():
    cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    rows = cur.fetchall()

    text = "🏆 TOP 10 ИГРОКОВ\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. @{r[0] or 'без_ника'} — {r[1]} 💰\n"
    return text


def get_stats():
    cur.execute("SELECT COUNT(*), SUM(balance) FROM users")
    count, total_bal = cur.fetchone()
    return f"📊 СТАТИСТИКА БОТА\n\n👥 Всего пользователей: {count}\n💰 Всего монет в системе: {total_bal or 0}"


# ---------- MENUS ----------
def menu(is_admin=False):
    kb = [
        [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        [InlineKeyboardButton("🏆 Топ", callback_data="top"), InlineKeyboardButton("💰 Баланс", callback_data="bal")],
        [InlineKeyboardButton("🎁 Бонус +50", callback_data="bonus")],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton("👑 Админ Панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)


def games():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 Баскет", callback_data="basket"), InlineKeyboardButton("⚽ Футбол", callback_data="football")],
        [InlineKeyboardButton("🎯 Дартс", callback_data="darts"), InlineKeyboardButton("🪙 Монетка", callback_data="flip")],
        [InlineKeyboardButton("🎰 Слоты", callback_data="slots"), InlineKeyboardButton("💣 Мины", callback_data="mines")],
        [InlineKeyboardButton("🎳 Кегли", callback_data="bowling")],
        [InlineKeyboardButton("⬅ Назад", callback_data="menu")],
    ])


def bets(game):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("10", callback_data=f"bet_{game}_10"),
            InlineKeyboardButton("50", callback_data=f"bet_{game}_50"),
            InlineKeyboardButton("100", callback_data=f"bet_{game}_100"),
        ],
        [
            InlineKeyboardButton("✏ Своя", callback_data=f"custom_{game}"),
            InlineKeyboardButton("🔥 Все", callback_data=f"bet_{game}_all"),
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


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Выдать баланс", callback_data="admin_add")],
        [InlineKeyboardButton("➖ Забрать баланс", callback_data="admin_sub")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("⬅ В главное меню", callback_data="menu")],
    ])


# ---------- MINES SYSTEM ----------
def generate_mines(uid, bet):
    grid = ["💣"] * 5 + ["💎"] * 20
    random.shuffle(grid)
    mines_games[uid] = {
        "grid": grid,
        "opened": [False] * 25,
        "bet": bet,
        "mult": 1.0,
    }


def mines_keyboard(uid):
    game = mines_games[uid]
    keyboard = []
    for row_start in range(0, 25, 5):
        row = []
        for col in range(5):
            index = row_start + col
            if game["opened"][index]:
                row.append(InlineKeyboardButton(game["grid"][index], callback_data="noop"))
            else:
                row.append(InlineKeyboardButton("❓", callback_data=f"mine_{index}"))
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(
            f"💰 Забрать x{round(game['mult'], 2)}",
            callback_data="mine_cashout",
        )
    ])
    return InlineKeyboardMarkup(keyboard)


# ---------- COMMAND HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, user.username or "")
    is_admin = (user.username == ADMIN_USERNAME)
    await update.message.reply_text("🎰 NEON CASINO", reply_markup=menu(is_admin))


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username != ADMIN_USERNAME:
        return
    await update.message.reply_text("👑 Админ Панель Управления:", reply_markup=admin_menu())


# ---------- TEXT MESSAGES HANDLER (CUSTOM BETS & ADMIN INPUT) ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    db_user = get_user(user.id, user.username or "")
    is_admin = (user.username == ADMIN_USERNAME)

    # Обслуживание ввода админа
    if is_admin and user.id in awaiting_admin:
        action = awaiting_admin.pop(user.id)
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ Формат: `@username_или_id сумма`", reply_markup=admin_menu())
            return

        target = get_by_identifier(parts[0])
        if not target:
            await update.message.reply_text("❌ Пользователь не найден!", reply_markup=admin_menu())
            return

        try:
            amount = int(parts[1])
        except ValueError:
            await update.message.reply_text("❌ Сумма должна быть числом!", reply_markup=admin_menu())
            return

        current_bal = target[2]
        new_bal = current_bal + amount if action == "add" else max(0, current_bal - amount)
        set_balance(target[0], new_bal)

        sign = "+" if action == "add" else "-"
        await update.message.reply_text(
            f"✅ Успешно! Пользователю @{target[1] or target[0]} изменено: {sign}{amount}\n"
            f"Новый баланс: {new_bal}",
            reply_markup=admin_menu()
        )
        return

    # Обслуживание своей ставки
    if user.id in awaiting_custom:
        game = awaiting_custom.pop(user.id)
        if not text.isdigit():
            await update.message.reply_text("❌ Введите корректное число!", reply_markup=menu(is_admin))
            return

        amount = int(text)
        if amount <= 0:
            await update.message.reply_text("❌ Ставка должна быть больше 0!", reply_markup=menu(is_admin))
            return

        if db_user[2] < amount:
            await update.message.reply_text("❌ Недостаточно средств!", reply_markup=menu(is_admin))
            return

        await start_bet_process(update.message, context, user, game, amount)


# ---------- CALLBACK QUERY HANDLER ----------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    await query.answer()

    db_user = get_user(user.id, user.username or "")
    is_admin = (user.username == ADMIN_USERNAME)

    # Navigation
    if data == "menu":
        await query.edit_message_text("🎰 NEON CASINO", reply_markup=menu(is_admin))

    elif data == "games":
        await query.edit_message_text("🎮 Выберите игру:", reply_markup=games())

    elif data == "bal":
        await query.edit_message_text(f"💰 Ваш баланс: {db_user[2]} монет", reply_markup=menu(is_admin))

    elif data == "top":
        await query.edit_message_text(top10(), reply_markup=menu(is_admin))

    # Bonus (+50 coins every 24h)
    elif data == "bonus":
        now = int(time.time())
        last_bonus = db_user[3]
        cooldown = 86400  # 24 hours in seconds

        if now - last_bonus >= cooldown:
            set_balance(user.id, db_user[2] + 50)
            update_bonus_time(user.id)
            await query.edit_message_text("🎁 Вы получили бонус +50 монет!", reply_markup=menu(is_admin))
        else:
            left_seconds = cooldown - (now - last_bonus)
            hours = left_seconds // 3600
            minutes = (left_seconds % 3600) // 60
            await query.edit_message_text(
                f"⏳ Бонус пока недоступен!\nПриходите через: {hours} ч. {minutes} мин.",
                reply_markup=menu(is_admin)
            )

    # ADMIN PANEL (Only Legendjau2)
    elif data == "admin_panel" and is_admin:
        await query.edit_message_text("👑 Панель Администратора", reply_markup=admin_menu())

    elif data == "admin_add" and is_admin:
        awaiting_admin[user.id] = "add"
        await query.edit_message_text("✏ Введите `@username` (или ID) и сумму через пробел:\nПример: `@steve 500`")

    elif data == "admin_sub" and is_admin:
        awaiting_admin[user.id] = "sub"
        await query.edit_message_text("✏ Введите `@username` (или ID) и сумму для снятия:\nПример: `@steve 200`")

    elif data == "admin_stats" and is_admin:
        await query.edit_message_text(get_stats(), reply_markup=admin_menu())

    # GAMES SELECTION & BETS
    elif data in {"basket", "football", "darts", "flip", "slots", "bowling", "mines"}:
        await query.edit_message_text("💸 Выберите или введите ставку:", reply_markup=bets(data))

    elif data.startswith("custom_"):
        game = data.split("_")[1]
        awaiting_custom[user.id] = game
        await query.edit_message_text("✏ Напишите сумму ставки сообщением в чат:")

    elif data.startswith("bet_"):
        _, game, amount_str = data.split("_")
        amount = db_user[2] if amount_str == "all" else int(amount_str)

        if amount <= 0:
            await query.edit_message_text("❌ У вас 0 монет!", reply_markup=menu(is_admin))
            return

        if db_user[2] < amount:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=menu(is_admin))
            return

        await start_bet_process(query, context, user, game, amount)

    # BASKET / FLIP CHOICE HANDLERS
    elif data.startswith("bchoice_"):
        choice = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)
        if not bet_info:
            await query.edit_message_text("❌ Сессия истекла.", reply_markup=menu(is_admin))
            return
        await query.delete_message()
        await play_basket(query.message.chat_id, context, user, bet_info["amount"], choice)

    elif data.startswith("fchoice_"):
        choice = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)
        if not bet_info:
            await query.edit_message_text("❌ Сессия истекла.", reply_markup=menu(is_admin))
            return
        await query.delete_message()
        await play_flip(query.message.chat_id, context, user, bet_info["amount"], choice)

    # MINES GAMEPLAY
    elif data.startswith("mine_") and data != "mine_cashout":
        index = int(data.split("_")[1])
        game = mines_games.get(user.id)
        if not game or game["opened"][index]:
            return

        game["opened"][index] = True
        if game["grid"][index] == "💣":
            mines_games.pop(user.id)
            await query.edit_message_text(f"💥 БОМБА! Вы подорвались!\n❌ Потеряно: {game['bet']}", reply_markup=menu(is_admin))
        else:
            game["mult"] += 0.25
            await query.edit_message_reply_markup(mines_keyboard(user.id))

    elif data == "mine_cashout":
        game = mines_games.get(user.id)
        if not game:
            return
        reward = int(game["bet"] * game["mult"])
        set_balance(user.id, db_user[2] + reward)
        mines_games.pop(user.id)
        await query.edit_message_text(f"💰 Забрано x{round(game['mult'], 2)}!\n🎉 Выигрыш: +{reward}", reply_markup=menu(is_admin))


# ---------- GAME STARTER ROUTER ----------
async def start_bet_process(event_obj, context, user, game, amount):
    is_admin = (user.username == ADMIN_USERNAME)

    if game == "mines":
        set_balance(user.id, get_user(user.id)[2] - amount)
        generate_mines(user.id, amount)
        text = "💣 Поле заминировано! Открывайте ячейки:"
        reply_markup = mines_keyboard(user.id)

        if isinstance(event_obj, Update) or hasattr(event_obj, 'reply_text'):
            await event_obj.reply_text(text, reply_markup=reply_markup)
        else:
            await event_obj.edit_message_text(text, reply_markup=reply_markup)
        return

    if game == "basket":
        pending_bets[user.id] = {"amount": amount}
        text, reply_markup = "🏀 Куда попадет мяч?", basket_choice_menu()
    elif game == "flip":
        pending_bets[user.id] = {"amount": amount}
        text, reply_markup = "🪙 Выберите сторону:", flip_choice_menu()
    else:
        chat_id = event_obj.chat.id if hasattr(event_obj, 'chat') else event_obj.message.chat_id
        if hasattr(event_obj, 'delete_message'):
            await event_obj.delete_message()
        await play_game(chat_id, context, user, game, amount)
        return

    if hasattr(event_obj, 'edit_message_text'):
        await event_obj.edit_message_text(text, reply_markup=reply_markup)
    else:
        await event_obj.reply_text(text, reply_markup=reply_markup)


# ---------- LOGIC FOR GAMES ----------
async def play_basket(chat_id, context, user, amount, choice):
    is_admin = (user.username == ADMIN_USERNAME)
    set_balance(user.id, get_user(user.id)[2] - amount)

    dice_msg = await context.bot.send_dice(chat_id, emoji="🏀")
    await asyncio.sleep(3.5)

    value = dice_msg.dice.value
    is_in = value in [4, 5]

    win = (choice == "in" and is_in) or (choice == "miss" and not is_in)
    coef = 2.5 if choice == "in" else 1.8

    new_bal = get_user(user.id)[2]
    if win:
        reward = int(amount * coef)
        set_balance(user.id, new_bal + reward)
        text = f"🎉 ВЫИГРЫШ!\nРезультат: {'Залетело! 🗑️' if is_in else 'Мимо! ❌'}\n💰 +{reward}"
    else:
        text = f"❌ ПРОИГРЫШ!\nРезультат: {'Залетело! 🗑️' if is_in else 'Мимо! ❌'}\n💸 -{amount}"

    await context.bot.send_message(chat_id, text, reply_markup=menu(is_admin))


async def play_flip(chat_id, context, user, amount, choice):
    is_admin = (user.username == ADMIN_USERNAME)
    set_balance(user.id, get_user(user.id)[2] - amount)

    msg = await context.bot.send_message(chat_id, "🪙 Монетка подбрасывается...")
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

    await msg.edit_text(text, reply_markup=menu(is_admin))


async def play_game(chat_id, context, user, game, amount):
    is_admin = (user.username == ADMIN_USERNAME)
    set_balance(user.id, get_user(user.id)[2] - amount)

    emoji_map = {"football": "⚽", "darts": "🎯", "slots": "🎰", "bowling": "🎳"}
    dice_msg = await context.bot.send_dice(chat_id, emoji=emoji_map[game])
    await asyncio.sleep(3.5)

    val = dice_msg.dice.value
    coef = 0

    if game == "football" and val in [3, 4, 5]:
        coef = 2.0
    elif game == "darts":
        coef = 3.0 if val == 6 else (1.5 if val in [4, 5] else 0)
    elif game == "bowling":
        coef = 3.0 if val == 6 else (1.5 if val in [3, 4, 5] else 0)
    elif game == "slots":
        coef = 5.0 if val == 64 else (3.0 if val in [1, 22, 43] else 0)

    new_bal = get_user(user.id)[2]
    if coef > 0:
        reward = int(amount * coef)
        set_balance(user.id, new_bal + reward)
        text = f"🎉 ВЫИГРЫШ (x{coef})!\n💰 +{reward}"
    else:
        text = f"❌ ПРОИГРЫШ!\n💸 -{amount}"

    await context.bot.send_message(chat_id, text, reply_markup=menu(is_admin))


# ---------- LAUNCH ----------
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin_cmd))
app.add_handler(CallbackQueryHandler(cb))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

app.run_polling()
