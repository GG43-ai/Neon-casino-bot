import asyncio
import os
import random
import sqlite3
import time
import math

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USERNAME = "Legendjau2"
CARD_NUMBER = "XXXX-XXXX-XXXX-XXXX"  # Вкажіть номер вашої картки

# ---------- КОНФІГУРАЦІЯ КУРСУ ----------
# 1 XTR (Star) = 0.8 грн => 1 грн = 1.25 Stars
# 100 монет = 1 грн => 1 Star = 80 монет
XTR_RATE_UAH = 0.8 

# ---------- DB SETUP ----------
DATA_DIR = "/app/data"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "casino.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance INTEGER DEFAULT 100,
    last_bonus INTEGER DEFAULT 0,
    referrer_id INTEGER DEFAULT 0,
    referrals_count INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    reward INTEGER,
    uses_left INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS promo_uses (
    user_id INTEGER,
    code TEXT,
    PRIMARY KEY (user_id, code)
)
""")

conn.commit()

# ---------- STATE ----------
mines_games = {}
crash_games = {}       
pending_bets = {}      
awaiting_custom = {}  
awaiting_admin = {}   
awaiting_deposit_amount = set()  
awaiting_receipt = set()         

# PvP
pvp_requests = {}
next_pvp_id = 1


# ---------- DB HELPERS ----------
def get_user(uid, username=""):
    cur.execute("SELECT user_id, username, balance, last_bonus, referrer_id, referrals_count FROM users WHERE user_id=?", (uid,))
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
        user = (user[0], username, user[2], user[3], user[4], user[5])

    return user


def set_balance(uid, balance):
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, uid))
    conn.commit()


def update_bonus_time(uid):
    now = int(time.time())
    cur.execute("UPDATE users SET last_bonus=? WHERE user_id=?", (now, uid))
    conn.commit()


def add_referral(new_user_id, referrer_id):
    cur.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (referrer_id, new_user_id))
    cur.execute("UPDATE users SET referrals_count = referrals_count + 1, balance = balance + 100 WHERE user_id=?", (referrer_id,))
    conn.commit()


def get_by_identifier(identifier):
    identifier = str(identifier).strip().lstrip("@")
    if identifier.isdigit():
        cur.execute("SELECT * FROM users WHERE user_id=?", (int(identifier),))
    else:
        cur.execute("SELECT * FROM users WHERE username=?", (identifier,))
    return cur.fetchone()


def top10():
    cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    rows = cur.fetchall()

    text = "🏆 TOP 10 ІГРОКІВ\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. @{r[0] or 'без_ніка'} — {r[1]} 💰\n"
    return text


def get_stats():
    cur.execute("SELECT COUNT(*), SUM(balance) FROM users")
    count, total_bal = cur.fetchone()
    return f"📊 СТАТИСТИКА БОТА\n\n👥 Всього користувачів: {count}\n💰 Всього монет у системі: {total_bal or 0}"


# ---------- MENUS ----------
def menu(is_admin=False):
    kb = [
        [InlineKeyboardButton("🎮 Ігри", callback_data="games"), InlineKeyboardButton("⚔️ PvP дуель", callback_data="pvp_info")],
        [InlineKeyboardButton("🏆 Топ", callback_data="top"), InlineKeyboardButton("💰 Баланс", callback_data="bal")],
        [InlineKeyboardButton("💳 Поповнити", callback_data="deposit"), InlineKeyboardButton("🎁 Бонус +50", callback_data="bonus")],
        [InlineKeyboardButton("💸 Переказ", callback_data="pay_info"), InlineKeyboardButton("👥 Реферали (+100 💰)", callback_data="ref_info")],
        [InlineKeyboardButton("🎟 Промокод", callback_data="promo_info")],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton("👑 Адмін Панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)


def deposit_card_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Перевірити (Надіслати чек)", callback_data="send_receipt")],
        [InlineKeyboardButton("⬅ Назад", callback_data="deposit")],
    ])


def admin_receipt_keyboard(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Підтвердити", callback_data=f"approve_dep_{user_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"decline_dep_{user_id}"),
        ]
    ])


def games():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Краш (Aviator)", callback_data="crash"), InlineKeyboardButton("💣 Міни", callback_data="mines")],
        [InlineKeyboardButton("🏀 Баскет", callback_data="basket"), InlineKeyboardButton("⚽ Футбол", callback_data="football")],
        [InlineKeyboardButton("🎯 Дартс", callback_data="darts"), InlineKeyboardButton("🪙 Монетка", callback_data="flip")],
        [InlineKeyboardButton("🎰 Слоти", callback_data="slots"), InlineKeyboardButton("🎳 Кеглі", callback_data="bowling")],
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
        [InlineKeyboardButton("🏀 Залетить (x2.5)", callback_data="bchoice_in")],
        [InlineKeyboardButton("❌ Мимо (x1.8)", callback_data="bchoice_miss")],
    ])


def flip_choice_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Орел (x1.9)", callback_data="fchoice_heads")],
        [InlineKeyboardButton("🪙 Решка (x1.9)", callback_data="fchoice_tails")],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Видати баланс", callback_data="admin_add")],
        [InlineKeyboardButton("➖ Забрати баланс", callback_data="admin_sub")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🎟 Інфо по промокодах", callback_data="admin_promo_help")],
        [InlineKeyboardButton("⬅ У головне меню", callback_data="menu")],
    ])


def pvp_accept_keyboard(pvp_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Прийняти дуель", callback_data=f"pvp_accept_{pvp_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"pvp_decline_{pvp_id}"),
        ]
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
            f"💰 Забрати x{round(game['mult'], 2)}",
            callback_data="mine_cashout",
        )
    ])
    return InlineKeyboardMarkup(keyboard)


# ---------- COMMAND HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id, user.username or "")
    is_admin = (user.username == ADMIN_USERNAME)

    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0].split("_")[1])
            if db_user[4] == 0 and referrer_id != user.id:
                ref_user = get_user(referrer_id)
                if ref_user:
                    add_referral(user.id, referrer_id)
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            f"🎉 Новий гравець перейшов за вашим реферальним посиланням!\n💰 Вам зараховано +100 монет!"
                        )
                    except Exception:
                        pass
        except ValueError:
            pass

    await update.message.reply_text("🎰 NEON CASINO", reply_markup=menu(is_admin))


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username != ADMIN_USERNAME:
        return
    await update.message.reply_text("👑 Адмін Панель Управління:", reply_markup=admin_menu())


async def create_promo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username != ADMIN_USERNAME:
        return

    if len(context.args) < 3:
        await update.message.reply_text("❌ Використання: `/create_promo КОД СУМА АКТИВАЦІЙ`\n\nПриклад: `/create_promo FREE100 100 50`", parse_mode="Markdown")
        return

    code = context.args[0].upper()
    try:
        reward = int(context.args[1])
        uses = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Сума та кількість активацій мають бути числами!")
        return

    if reward <= 0 or uses <= 0:
        await update.message.reply_text("❌ Значення мають бути більшими за 0!")
        return

    try:
        cur.execute("INSERT INTO promo_codes (code, reward, uses_left) VALUES (?, ?, ?)", (code, reward, uses))
        conn.commit()
        await update.message.reply_text(f"✅ Промокод створено!\n\n🎟 Код: `{code}`\n💰 Нагорода: **{reward}**\n👥 Активацій: **{uses}**", parse_mode="Markdown")
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Промокод з таким ім'ям вже існує!")


async def promo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id, user.username or "")

    if len(context.args) < 1:
        await update.message.reply_text("❌ Використання: `/promo ВАШ_КОД`", parse_mode="Markdown")
        return

    code = context.args[0].upper()

    cur.execute("SELECT reward, uses_left FROM promo_codes WHERE code=?", (code,))
    promo = cur.fetchone()

    if not promo:
        await update.message.reply_text("❌ Такого промокоду не існує!")
        return

    reward, uses_left = promo

    if uses_left <= 0:
        await update.message.reply_text("❌ У цього промокоду закінчилися активації!")
        return

    cur.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (user.id, code))
    if cur.fetchone():
        await update.message.reply_text("❌ Ви вже активували цей промокод!")
        return

    cur.execute("INSERT INTO promo_uses (user_id, code) VALUES (?, ?)", (user.id, code))
    cur.execute("UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code=?", (code,))
    set_balance(user.id, db_user[2] + reward)
    conn.commit()

    await update.message.reply_text(f"🎉 Промокод `{code}` успішно активовано!\n💰 Вам зараховано: **+{reward} монет**", parse_mode="Markdown")


async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = update.effective_user
    sender_db = get_user(sender.id, sender.username or "")

    if len(context.args) < 2:
        await update.message.reply_text("❌ Використання: `/pay @username сума` або `/pay ID сума`", parse_mode="Markdown")
        return

    target_input = context.args[0]
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Сума має бути цілим числом!")
        return

    if amount <= 0 or sender_db[2] < amount:
        await update.message.reply_text("❌ Недостатньо грошей або сума <= 0!")
        return

    target_db = get_by_identifier(target_input)
    if not target_db:
        await update.message.reply_text("❌ Користувача не знайдено!")
        return

    if target_db[0] == sender.id:
        await update.message.reply_text("❌ Не можна переводити самому собі!")
        return

    set_balance(sender.id, sender_db[2] - amount)
    set_balance(target_db[0], target_db[2] + amount)

    target_name = f"@{target_db[1]}" if target_db[1] else str(target_db[0])
    await update.message.reply_text(f"✅ Ви успішно перевели {amount} 💰 користувачу {target_name}!")
    try:
        sender_name = f"@{sender.username}" if sender.username else str(sender.id)
        await context.bot.send_message(target_db[0], f"💸 Гравець {sender_name} перевів вам {amount} 💰!")
    except Exception:
        pass


async def pvp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global next_pvp_id
    challenger = update.effective_user
    challenger_db = get_user(challenger.id, challenger.username or "")

    if len(context.args) < 2:
        await update.message.reply_text("❌ Використання: `/pvp @username ставка`", parse_mode="Markdown")
        return

    target_input = context.args[0]
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Ставка має бути числом!")
        return

    if amount <= 0 or challenger_db[2] < amount:
        await update.message.reply_text("❌ Недостатньо коштів або некоректна ставка!")
        return

    opponent_db = get_by_identifier(target_input)
    if not opponent_db or opponent_db[0] == challenger.id:
        await update.message.reply_text("❌ Гравець не знайдений або ви вказали самого себе!")
        return

    if opponent_db[2] < amount:
        await update.message.reply_text("❌ У суперника недостатньо коштів!")
        return

    pvp_id = next_pvp_id
    next_pvp_id += 1

    pvp_requests[pvp_id] = {
        "challenger_id": challenger.id,
        "opponent_id": opponent_db[0],
        "amount": amount,
    }

    challenger_name = f"@{challenger.username}" if challenger.username else str(challenger.id)
    opponent_name = f"@{opponent_db[1]}" if opponent_db[1] else str(opponent_db[0])

    await update.message.reply_text(f"⚔️ Викликано на дуель {opponent_name} на {amount} 💰!\nОчікуємо підтвердження...")

    try:
        await context.bot.send_message(
            opponent_db[0],
            f"⚔️ **PvP Виклику!**\n\nГравець {challenger_name} викликає вас на дуель на кубиках 🎲!\nСтавка: **{amount} 💰**",
            parse_mode="Markdown",
            reply_markup=pvp_accept_keyboard(pvp_id)
        )
    except Exception:
        await update.message.reply_text("❌ Не вдалося надіслати запит супернику.")


# ---------- PAYMENTS (TELEGRAM STARS) ----------
async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user = update.effective_user

    try:
        coins = int(payment.invoice_payload.split("_")[1])
    except Exception:
        coins = 0

    if coins > 0:
        db_user = get_user(user.id, user.username or "")
        set_balance(user.id, db_user[2] + coins)
        await update.message.reply_text(
            f"🎉 **ОПЛАТА УСПІШНА!**\n\nВам зараховано: **+{coins} 💰**\nДякуємо за покупку! 🔥",
            parse_mode="Markdown",
            reply_markup=menu(user.username == ADMIN_USERNAME)
        )


# ---------- TEXT & PHOTO HANDLER ----------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in awaiting_receipt:
        awaiting_receipt.remove(user.id)
        photo_id = update.message.photo[-1].file_id

        admin_db = get_by_identifier(ADMIN_USERNAME)
        if not admin_db:
            await update.message.reply_text("❌ Адміністратор тимчасово недоступний. Напишіть напряму @Legendjau2")
            return

        admin_id = admin_db[0]
        user_info = f"@{user.username}" if user.username else f"ID: {user.id}"

        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_id,
                caption=f"💳 **НОВА ЗАЯВКА НА ПОПОВНЕННЯ!**\n\nГравець: {user_info}\nID: `{user.id}`",
                parse_mode="Markdown",
                reply_markup=admin_receipt_keyboard(user.id)
            )
            await update.message.reply_text("✅ Скріншот надіслано адміністратору на перевірку! Очікуйте зарахування монет.")
        except Exception:
            await update.message.reply_text("❌ Помилка надсилання чеку адміну. Напишіть напряму @Legendjau2")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    db_user = get_user(user.id, user.username or "")
    is_admin = (user.username == ADMIN_USERNAME)

    if user.id in awaiting_deposit_amount:
        awaiting_deposit_amount.remove(user.id)
        if not text.isdigit() or int(text) < 100:
            await update.message.reply_text("❌ Мінімальна сума поповнення — **100 монет**!", parse_mode="Markdown")
            return

        coins = int(text)
        uah_cost = round(coins / 100, 2)
        # Курс 1 Star = 0.8 грн -> Stars = грн / 0.8
        stars_cost = max(1, math.ceil(uah_cost / XTR_RATE_UAH))

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⭐ Оплатити {stars_cost} Stars", callback_data=f"buy_stars_{coins}_{stars_cost}")],
            [InlineKeyboardButton(f"💳 Оплатити карткою ({uah_cost} грн)", callback_data=f"buy_card_{coins}_{uah_cost}")],
            [InlineKeyboardButton("⬅ Назад", callback_data="deposit")],
        ])

        msg_text = (
            f"💳 **ПОПОВНЕННЯ БАЛАНСУ**\n\n"
            f"💰 Ви отримуєте: **{coins} монет**\n"
            f"💵 Вартість: **{uah_cost} грн** (або **⭐ {stars_cost} Stars**)\n\n"
            f"Оберіть зручний спосіб оплати:"
        )
        await update.message.reply_text(msg_text, parse_mode="Markdown", reply_markup=kb)
        return

    if is_admin and user.id in awaiting_admin:
        action_data = awaiting_admin.pop(user.id)

        if isinstance(action_data, dict) and action_data.get("action") == "approve_deposit":
            target_id = action_data["target_id"]
            if not text.isdigit():
                await update.message.reply_text("❌ Введіть число монет!")
                return

            add_coins = int(text)
            target_db = get_user(target_id)
            set_balance(target_id, target_db[2] + add_coins)

            await update.message.reply_text(f"✅ Баланс гравця `{target_id}` поповнено на **+{add_coins} монет**!", parse_mode="Markdown")
            try:
                await context.bot.send_message(target_id, f"🎉 Ваш баланс успішно поповнено на **+{add_coins} 💰**!", parse_mode="Markdown")
            except Exception:
                pass
            return

        action = action_data
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ Формат: `@username_або_id сума`", reply_markup=admin_menu())
            return

        target = get_by_identifier(parts[0])
        if not target:
            await update.message.reply_text("❌ Користувача не знайдено!", reply_markup=admin_menu())
            return

        try:
            amount = int(parts[1])
        except ValueError:
            await update.message.reply_text("❌ Сума має бути числом!", reply_markup=admin_menu())
            return

        current_bal = target[2]
        new_bal = current_bal + amount if action == "add" else max(0, current_bal - amount)
        set_balance(target[0], new_bal)

        sign = "+" if action == "add" else "-"
        await update.message.reply_text(
            f"✅ Користувачу @{target[1] or target[0]} змінено: {sign}{amount}\nНовий баланс: {new_bal}",
            reply_markup=admin_menu()
        )
        return

    if user.id in awaiting_custom:
        game = awaiting_custom.pop(user.id)
        if not text.isdigit():
            await update.message.reply_text("❌ Введіть число!", reply_markup=menu(is_admin))
            return

        amount = int(text)
        if amount <= 0 or db_user[2] < amount:
            await update.message.reply_text("❌ Помилка в сумі або недостатньо монет!", reply_markup=menu(is_admin))
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

    if data == "menu":
        await query.edit_message_text("🎰 NEON CASINO", reply_markup=menu(is_admin))

    elif data == "games":
        await query.edit_message_text("🎮 Оберіть гру:", reply_markup=games())

    elif data == "bal":
        await query.edit_message_text(f"💰 Ваш баланс: {db_user[2]} монет", reply_markup=menu(is_admin))

    elif data == "top":
        await query.edit_message_text(top10(), reply_markup=menu(is_admin))

    elif data == "deposit":
        awaiting_deposit_amount.add(user.id)
        text = (
            "💳 **ПОПОВНЕННЯ БАЛАНСУ**\n\n"
            f"📌 Курс обміну: **100 💰 = 1 грн (~1.25 Stars ⭐)**\n\n"
            "✏ Напишіть у чат **суму монет**, яку ви бажаєте придбати:\n"
            "_(Наприклад: `500` або `1000`)_"
        )
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data.startswith("buy_stars_"):
        _, _, coins_str, stars_str = data.split("_")
        coins = int(coins_str)
        stars = int(stars_str)

        title = f"Поповнення {coins} монет"
        description = f"Зарахування {coins} монет на ігровий баланс у NEON CASINO"
        payload = f"deposit_{coins}"
        prices = [LabeledPrice(label=f"{coins} Монет", amount=stars)]

        await query.delete_message()
        # provider_token="" ОБОВ'ЯЗКОВО ДЛЯ TELEGRAM STARS
        await context.bot.send_invoice(
            chat_id=user.id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="deposit_stars"
        )

    elif data.startswith("buy_card_"):
        _, _, coins_str, uah_str = data.split("_")
        text = (
            f"💳 **Оплата карткою**\n\n"
            f"💰 Ви отримуєте: **{coins_str} монет**\n"
            f"💵 До оплати: **{uah_str} грн**\n\n"
            f"📌 Реквізити картки:\n`{CARD_NUMBER}`\n\n"
            f"Після переказу натисніть **«Перевірити»** та надішліть чек!"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=deposit_card_menu())

    elif data == "send_receipt":
        awaiting_receipt.add(user.id)
        await query.edit_message_text("📸 **Надішліть скріншот чека прямо сюди в чат:**", parse_mode="Markdown")

    elif data.startswith("approve_dep_") and is_admin:
        target_id = int(data.split("_")[2])
        awaiting_admin[user.id] = {"action": "approve_deposit", "target_id": target_id}
        await query.message.reply_text(f"✏ Введіть суму монет для зарахування гравцю `{target_id}`:", parse_mode="Markdown")

    elif data.startswith("decline_dep_") and is_admin:
        target_id = int(data.split("_")[2])
        await query.message.edit_caption(caption=f"{query.message.caption}\n\n❌ **ВІДХИЛЕНО АДМІНОМ**", parse_mode="Markdown")
        try:
            await context.bot.send_message(target_id, "❌ Ваша заявка на поповнення була відхилена адміністратором.")
        except Exception:
            pass

    elif data == "pay_info":
        await query.edit_message_text("💸 **Переказ коштів**\n\nКоманда:\n`/pay @username сума`", parse_mode="Markdown", reply_markup=menu(is_admin))

    elif data == "pvp_info":
        await query.edit_message_text("⚔️ **PvP Дуелі на кубиках**\n\nЩоб викликати гравця, введіть:\n`/pvp @username ставка`", parse_mode="Markdown", reply_markup=menu(is_admin))

    elif data == "ref_info":
        bot_username = context.bot.username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
        ref_text = (
            f"👥 **Реферальна програма**\n\n"
            f"Запрошуйте друзів та отримуйте **+100 💰** за кожного!\n\n"
            f"🔗 Ваше посилання:\n`{ref_link}`\n\n"
            f"📊 Запрошено друзів: **{db_user[5]}**"
        )
        await query.edit_message_text(ref_text, parse_mode="Markdown", reply_markup=menu(is_admin))

    elif data == "promo_info":
        await query.edit_message_text("🎟 **Активація промокоду**\n\nЩоб активувати промокод, введіть:\n`/promo ВАШ_КОД`", parse_mode="Markdown", reply_markup=menu(is_admin))

    elif data == "bonus":
        now = int(time.time())
        last_bonus = db_user[3]
        cooldown = 86400

        if now - last_bonus >= cooldown:
            set_balance(user.id, db_user[2] + 50)
            update_bonus_time(user.id)
            await query.edit_message_text("🎁 Ви отримали бонус +50 монет!", reply_markup=menu(is_admin))
        else:
            left_seconds = cooldown - (now - last_bonus)
            hours = left_seconds // 3600
            minutes = (left_seconds % 3600) // 60
            await query.edit_message_text(f"⏳ Приходьте через: {hours} год. {minutes} хв.", reply_markup=menu(is_admin))

    elif data == "admin_panel" and is_admin:
        await query.edit_message_text("👑 Панель Адміністратора", reply_markup=admin_menu())

    elif data == "admin_add" and is_admin:
        awaiting_admin[user.id] = "add"
        await query.edit_message_text("✏ Введіть `@username` (або ID) та суму:\nПриклад: `@steve 500`")

    elif data == "admin_sub" and is_admin:
        awaiting_admin[user.id] = "sub"
        await query.edit_message_text("✏ Введіть `@username` (або ID) та суму:\nПриклад: `@steve 200`")

    elif data == "admin_stats" and is_admin:
        await query.edit_message_text(get_stats(), reply_markup=admin_menu())

    elif data == "admin_promo_help" and is_admin:
        text = (
            "🎟 **Команда створення промокодів (Тільки для Адміна):**\n\n"
            "`/create_promo КОД СУМА КІЛЬКІСТЬ`\n\n"
            "Приклад:\n`/create_promo NEON2026 150 20`"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_menu())

    # CRASH CASHOUT
    elif data == "crash_cashout":
        game = crash_games.get(user.id)
        if game and not game["crashed"] and not game["cashed_out"]:
            game["cashed_out"] = True
            reward = int(game["bet"] * game["mult"])
            set_balance(user.id, db_user[2] + reward)
            await query.answer(f"🎉 Ви успішно забрали {reward} 💰 (x{game['mult']:.2f})!", show_alert=True)

    # PVP ACCEPT / DECLINE
    elif data.startswith("pvp_accept_"):
        pvp_id = int(data.split("_")[2])
        req = pvp_requests.pop(pvp_id, None)

        if not req:
            await query.edit_message_text("❌ Дуель не знайдено або вже завершено.")
            return

        if user.id != req["opponent_id"]:
            await query.answer("❌ Це виклик не для вас!", show_alert=True)
            return

        c_db = get_user(req["challenger_id"])
        o_db = get_user(req["opponent_id"])
        amount = req["amount"]

        if c_db[2] < amount or o_db[2] < amount:
            await query.edit_message_text("❌ У одного з гравців недостатньо коштів!")
            return

        set_balance(c_db[0], c_db[2] - amount)
        set_balance(o_db[0], o_db[2] - amount)

        await query.edit_message_text("⚔️ **Дуель розпочалася! Кидаємо кубики...**", parse_mode="Markdown")

        chat_id = query.message.chat_id
        await run_pvp_match(chat_id, context, c_db[0], o_db[0], amount)

    elif data.startswith("pvp_decline_"):
        pvp_id = int(data.split("_")[2])
        req = pvp_requests.pop(pvp_id, None)
        if req:
            await query.edit_message_text("❌ Ви відхилили виклик на дуель.")
            try:
                await context.bot.send_message(req["challenger_id"], "❌ Суперник відхилив ваш виклик на дуель.")
            except Exception:
                pass

    # GAMES SELECTION & BETS
    elif data in {"basket", "football", "darts", "flip", "slots", "bowling", "mines", "crash"}:
        await query.edit_message_text("💸 Оберіть або введіть ставку:", reply_markup=bets(data))

    elif data.startswith("custom_"):
        game = data.split("_")[1]
        awaiting_custom[user.id] = game
        await query.edit_message_text("✏ Напишіть суму ставки повідомленням у чат:")

    elif data.startswith("bet_"):
        _, game, amount_str = data.split("_")
        amount = db_user[2] if amount_str == "all" else int(amount_str)

        if amount <= 0 or db_user[2] < amount:
            await query.edit_message_text("❌ Недостатньо коштів!", reply_markup=menu(is_admin))
            return

        await start_bet_process(query, context, user, game, amount)

    # BASKET / FLIP CHOICE HANDLERS
    elif data.startswith("bchoice_"):
        choice = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)
        if not bet_info:
            await query.edit_message_text("❌ Сесія закінчилася.", reply_markup=menu(is_admin))
            return
        await query.delete_message()
        await play_basket(query.message.chat_id, context, user, bet_info["amount"], choice)

    elif data.startswith("fchoice_"):
        choice = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)
        if not bet_info:
            await query.edit_message_text("❌ Сесія закінчилася.", reply_markup=menu(is_admin))
            return
        await query.delete_message()
        await play_flip(query.message.chat_id, context, user, bet_info["amount"], choice)

    # MINES
    elif data.startswith("mine_") and data != "mine_cashout":
        index = int(data.split("_")[1])
        game = mines_games.get(user.id)
        if not game or game["opened"][index]:
            return

        game["opened"][index] = True
        if game["grid"][index] == "💣":
            mines_games.pop(user.id)
            await query.edit_message_text(f"💥 БОМБА! Ви підірвалися!\n❌ Втрачено: {game['bet']}", reply_markup=menu(is_admin))
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
        await query.edit_message_text(f"💰 Забрано x{round(game['mult'], 2)}!\n🎉 Виграш: +{reward}", reply_markup=menu(is_admin))


# ---------- НИЗЬКОПРИБУТКОВИЙ CRASH ALGORITHM ----------
def generate_crash_multiplier() -> float:
    """
    Агресивний алгоритм із мінімальним RTP (Return to Player):
    - 15% шанс миттєвого крашу на 1.00x
    - ~50% ігор падають у діапазоні 1.01x - 1.15x
    - ~30% ігор падають у діапазоні 1.16x - 1.40x
    - ~4% ігор доходять до 1.41x - 1.80x
    - <1% шанс дійти до 1.81x - 2.20x
    """
    rand = random.random()

    # 1. Миттєвий слив (1.00x) — 15% випадків
    if rand < 0.15:
        return 1.00

    # 2. Ранній краш (1.01x - 1.15x) — 50% випадків
    if rand < 0.65:
        return math.floor((1.01 + random.random() * 0.14) * 100) / 100

    # 3. Середній краш (1.16x - 1.40x) — 30% випадків
    if rand < 0.95:
        return math.floor((1.16 + random.random() * 0.24) * 100) / 100

    # 4. Рідкісний коефіцієнт (1.41x - 1.80x) — 4% випадків
    if rand < 0.99:
        return math.floor((1.41 + random.random() * 0.39) * 100) / 100

    # 5. Максимально можливий коефіцієнт (1.81x - 2.20x) — 1% випадків
    return math.floor((1.81 + random.random() * 0.39) * 100) / 100


async def run_crash_game(chat_id, context, user, amount):
    is_admin = (user.username == ADMIN_USERNAME)
    set_balance(user.id, get_user(user.id)[2] - amount)

    # Генерація виключно збиткового для гравця коефіцієнта
    crash_mult = generate_crash_multiplier()

    crash_games[user.id] = {
        "bet": amount,
        "crashed": False,
        "cashed_out": False,
        "mult": 1.00,
    }

    current_mult = 1.00
    msg = await context.bot.send_message(
        chat_id,
        f"🚀 **AVIATOR / КРАШ**\n\nСтавка: **{amount} 💰**\nКоефіцієнт: **x1.00**",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Забрати x1.00", callback_data="crash_cashout")]])
    )

    # Якщо одразу 1.00x — миттєвий програш
    if crash_mult == 1.00:
        crash_games.pop(user.id, None)
        await asyncio.sleep(0.5)
        await msg.edit_text(
            f"💥 **МИТТЄВИЙ КРАШ!**\n\n📈 Гра упала на: **x1.00**\n💸 Втрачено: **{amount} 💰**",
            reply_markup=menu(is_admin)
        )
        return

    # Динаміка зростання коефіцієнта
    while current_mult < crash_mult:
        await asyncio.sleep(1.0)
        game = crash_games.get(user.id)

        if not game or game["cashed_out"]:
            break

        # Невеликий крок зростання (0.02 - 0.05), щоб гравець частіше не встигав забрати
        step = round(random.uniform(0.02, 0.05), 2)
        current_mult = round(current_mult + step, 2)
        game["mult"] = current_mult

        if current_mult >= crash_mult:
            game["crashed"] = True
            break

        try:
            await msg.edit_text(
                f"🚀 **AVIATOR / КРАШ**\n\nСтавка: **{amount} 💰**\nКоефіцієнт: **x{current_mult:.2f}** 📈",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"💰 Забрати x{current_mult:.2f}", callback_data="crash_cashout")]])
            )
        except Exception:
            pass

    game = crash_games.pop(user.id, None)

    if game:
        if game["cashed_out"]:
            reward = int(amount * game["mult"])
            await msg.edit_text(
                f"🎉 **УСПІШНО ЗАБРАНО!**\n\n📈 Коефіцієнт: **x{game['mult']:.2f}**\n💰 Виграш: **+{reward} монет**",
                reply_markup=menu(is_admin)
            )
        else:
            await msg.edit_text(
                f"💥 **КРАШ! Літак полетів!**\n\n📈 Коефіцієнт зупинився на: **x{crash_mult:.2f}**\n💸 Втрачено: **{amount} 💰**",
                reply_markup=menu(is_admin)
            )


# ---------- VISIBLE SINGLE-CHAT PVP MATCH ----------
async def run_pvp_match(chat_id, context, challenger_id, opponent_id, amount):
    c_user = get_user(challenger_id)
    o_user = get_user(opponent_id)

    c_name = f"@{c_user[1]}" if c_user[1] else f"ID:{c_user[0]}"
    o_name = f"@{o_user[1]}" if o_user[1] else f"ID:{o_user[0]}"

    await context.bot.send_message(chat_id, f"🎲 Кидає {c_name}...")
    dice1 = await context.bot.send_dice(chat_id, emoji="🎲")
    
    await asyncio.sleep(3.5)

    await context.bot.send_message(chat_id, f"🎲 Кидає {o_name}...")
    dice2 = await context.bot.send_dice(chat_id, emoji="🎲")

    await asyncio.sleep(3.5)

    c_val = dice1.dice.value
    o_val = dice2.dice.value

    result_msg = (
        f"📊 **Підсумки PvP дуелі:**\n\n"
        f"{c_name}: **{c_val}** 🎲\n"
        f"{o_name}: **{o_val}** 🎲\n\n"
    )

    if c_val > o_val:
        win_amount = amount * 2
        set_balance(challenger_id, get_user(challenger_id)[2] + win_amount)
        result_msg += f"🏆 Переможець: {c_name}!\nВиграш: **+{win_amount} 💰**"
    elif o_val > c_val:
        win_amount = amount * 2
        set_balance(opponent_id, get_user(opponent_id)[2] + win_amount)
        result_msg += f"🏆 Переможець: {o_name}!\nВиграш: **+{win_amount} 💰**"
    else:
        set_balance(challenger_id, get_user(challenger_id)[2] + amount)
        set_balance(opponent_id, get_user(opponent_id)[2] + amount)
        result_msg += "🤝 **Нічия!** Ставки повернуто гравцям."

    targets = {chat_id, challenger_id, opponent_id}
    for tid in targets:
        try:
            await context.bot.send_message(tid, result_msg, parse_mode="Markdown")
        except Exception:
            pass


# ---------- GAME STARTER ROUTER ----------
async def start_bet_process(event_obj, context, user, game, amount):
    is_admin = (user.username == ADMIN_USERNAME)
    chat_id = event_obj.chat.id if hasattr(event_obj, 'chat') else event_obj.message.chat_id

    if game == "mines":
        set_balance(user.id, get_user(user.id)[2] - amount)
        generate_mines(user.id, amount)
        text = "💣 Поле заміновано! Відкривайте осередки:"
        reply_markup = mines_keyboard(user.id)

        if isinstance(event_obj, Update) or hasattr(event_obj, 'reply_text'):
            await event_obj.reply_text(text, reply_markup=reply_markup)
        else:
            await event_obj.edit_message_text(text, reply_markup=reply_markup)
        return

    if game == "crash":
        if hasattr(event_obj, 'delete_message'):
            await event_obj.delete_message()
        asyncio.create_task(run_crash_game(chat_id, context, user, amount))
        return

    if game == "basket":
        pending_bets[user.id] = {"amount": amount}
        text, reply_markup = "🏀 Куди влучить м'яч?", basket_choice_menu()
    elif game == "flip":
        pending_bets[user.id] = {"amount": amount}
        text, reply_markup = "🪙 Оберіть сторону:", flip_choice_menu()
    else:
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
        text = f"🎉 ВИГРАШ!\nРезультат: {'Залетіло! 🗑️' if is_in else 'Мимо! ❌'}\n💰 +{reward}"
    else:
        text = f"❌ ПРОГРАШ!\nРезультат: {'Залетіло! 🗑️' if is_in else 'Мимо! ❌'}\n💸 -{amount}"

    await context.bot.send_message(chat_id, text, reply_markup=menu(is_admin))


async def play_flip(chat_id, context, user, amount, choice):
    is_admin = (user.username == ADMIN_USERNAME)
    set_balance(user.id, get_user(user.id)[2] - amount)

    msg = await context.bot.send_message(chat_id, "🪙 Монетка підкидається...")
    await asyncio.sleep(1.5)

    result = random.choice(["heads", "tails"])
    res_text = "Орел 🪙" if result == "heads" else "Решка 🪙"

    new_bal = get_user(user.id)[2]
    if choice == result:
        reward = int(amount * 1.9)
        set_balance(user.id, new_bal + reward)
        text = f"🎉 ВИГРАШ!\nВипав: {res_text}\n💰 +{reward}"
    else:
        text = f"❌ ПРОГРАШ!\nВипав: {res_text}\n💸 -{amount}"

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
        text = f"🎉 ВИГРАШ (x{coef})!\n💰 +{reward}"
    else:
        text = f"❌ ПРОГРАШ!\n💸 -{amount}"

    await context.bot.send_message(chat_id, text, reply_markup=menu(is_admin))


# ---------- LAUNCH ----------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = ApplicationBuilder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("pay", pay_cmd))
    app.add_handler(CommandHandler("pvp", pvp_cmd))
    app.add_handler(CommandHandler("create_promo", create_promo_cmd))
    app.add_handler(CommandHandler("promo", promo_cmd))

    # Payment Handlers (Telegram Stars)
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # General Handlers
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # run_polling з обов'язково вказаною підтримкою pre_checkout_query
    app.run_polling(allowed_updates=["message", "callback_query", "pre_checkout_query"])
