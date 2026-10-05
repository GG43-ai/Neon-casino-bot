import asyncio
import os
import random
import time
import asyncpg
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
import os
import threading
from flask import Flask

# Создаем минимальное Flask-приложение для Render
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

def run_web_server():
    # Render автоматически передает номер порта в переменную PORT
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Запускаем Flask в отдельном потоке, чтобы он не мешал Telegram-боту
threading.Thread(target=run_web_server, daemon=True).start()
# ---------- CONFIGURATION ----------
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Legendjau2")
CARD_NUMBER = os.getenv("CARD_NUMBER", "4149 4999 9999 9999")
STARS_PER_UAH = float(os.getenv("STARS_PER_UAH", "1.0"))

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

# Глобальный пул соединений БД
db_pool: asyncpg.Pool = None

# ---------- DATABASE SETUP ----------
async def init_db_pool():
    global db_pool
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не задан в переменных окружения!")
    
    # Создаем пул соединений (минимально 5, максимально 20 параллельных подключений)
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=5, max_size=20)
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance BIGINT DEFAULT 100,
                last_bonus BIGINT DEFAULT 0,
                referrer_id BIGINT DEFAULT 0,
                referrals_count INT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                reward BIGINT,
                uses_left INT
            );
            CREATE TABLE IF NOT EXISTS promo_uses (
                user_id BIGINT,
                code TEXT,
                PRIMARY KEY (user_id, code)
            );
        """)

# ---------- IN-MEMORY STATE ----------
awaiting_admin = {}
awaiting_custom = {}
pending_bets = {}
mines_games = {}
crash_games = {}
pvp_requests = {}
next_pvp_id = 1
user_wins_history = {}
suspicious_users_log = {}
awaiting_deposit_amount = set()
awaiting_receipt = set()
awaiting_broadcast = set()

# ---------- DATABASE HELPER FUNCTIONS ----------
async def get_user(user_id: int, username: str = ""):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        if not row:
            await conn.execute(
                "INSERT INTO users (user_id, username, balance, last_bonus, referrer_id, referrals_count) "
                "VALUES ($1, $2, 100, 0, 0, 0)",
                user_id, username
            )
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        elif username and row['username'] != username:
            await conn.execute("UPDATE users SET username = $1 WHERE user_id = $2", username, user_id)
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return list(row.values())

async def set_balance(user_id: int, balance: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", balance, user_id)

async def update_bonus_time(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET last_bonus = $1 WHERE user_id = $2", int(time.time()), user_id)

async def add_referral(user_id: int, referrer_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET referrer_id = $1 WHERE user_id = $2", referrer_id, user_id)
        await conn.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = $1", referrer_id)
        ref = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", referrer_id)
        if ref:
            await conn.execute("UPDATE users SET balance = balance + 100 WHERE user_id = $1", referrer_id)

async def get_by_identifier(identifier: str):
    identifier = identifier.strip()
    async with db_pool.acquire() as conn:
        if identifier.startswith("@"):
            uname = identifier[1:]
            row = await conn.fetchrow("SELECT * FROM users WHERE LOWER(username) = LOWER($1)", uname)
            return list(row.values()) if row else None
        elif identifier.isdigit():
            uid = int(identifier)
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", uid)
            return list(row.values()) if row else None
        else:
            row = await conn.fetchrow("SELECT * FROM users WHERE LOWER(username) = LOWER($1)", identifier)
            return list(row.values()) if row else None

async def top10():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, user_id, balance FROM users ORDER BY balance DESC LIMIT 10")
        msg = "🏆 **ТОП 10 БОГАЧЕЙ** 🏆\n\n"
        for i, row in enumerate(rows, 1):
            name = f"@{row['username']}" if row['username'] else f"ID: {row['user_id']}"
            msg += f"{i}. {name} — **{row['balance']}** 💰\n"
        return msg

async def get_stats():
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_balance = await conn.fetchval("SELECT SUM(balance) FROM users") or 0
        return (
            f"📊 **СТАТИСТИКА БОТА**\n\n"
            f"👥 Всего пользователей: **{total_users}**\n"
            f"💰 Суммарный баланс: **{total_balance} монет**"
        )

# ---------- ANTI-FRAUD ENGINE ----------
def track_game_win(user_id: int, username: str, is_win: bool):
    current_wins = user_wins_history.get(user_id, 0)
    if is_win:
        current_wins += 1
        user_wins_history[user_id] = current_wins
        if current_wins >= 7:
            uname_str = username or f"ID:{user_id}"
            alert_msg = f"Подозрительная серия побед: {current_wins} раз подряд!"
            suspicious_users_log[user_id] = {
                "username": uname_str,
                "reason": alert_msg,
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            return alert_msg
    else:
        user_wins_history[user_id] = 0
    return None

def check_transfer_fraud(sender_id: int, sender_username: str):
    wins = user_wins_history.get(sender_id, 0)
    if wins >= 5:
        uname_str = sender_username or f"ID:{sender_id}"
        alert_msg = f"Крупный перевод при высокой серии побед ({wins} побед подряд)!"
        suspicious_users_log[sender_id] = {
            "username": uname_str,
            "reason": alert_msg,
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        return alert_msg
    return None

async def notify_admin_fraud(context: ContextTypes.DEFAULT_TYPE, alert_text: str):
    admin_db = await get_by_identifier(ADMIN_USERNAME)
    if admin_db:
        try:
            await context.bot.send_message(
                admin_db[0],
                f"🚨 **АНТИ-ФРОД СИСТЕМА**\n\n⚠️ {alert_text}",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ---------- KEYBOARDS & MINES GENERATOR ----------
def menu(is_admin=False):
    kb = [
        [InlineKeyboardButton("🎮 Игры", callback_data="games"), InlineKeyboardButton("💰 Баланс", callback_data="bal")],
        [InlineKeyboardButton("💳 Пополнить", callback_data="deposit"), InlineKeyboardButton("🎁 Ежедневный бонус", callback_data="bonus")],
        [InlineKeyboardButton("🏆 Топ 10", callback_data="top"), InlineKeyboardButton("👥 Рефералы", callback_data="ref_info")],
        [InlineKeyboardButton("💸 Перевод", callback_data="pay_info"), InlineKeyboardButton("⚔️ PvP Дуэль", callback_data="pvp_info")],
        [InlineKeyboardButton("🎟 Промокод", callback_data="promo_info")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton("👑 Админ Панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)

def games():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎰 Рулетка", callback_data="bet_roulette_10"), InlineKeyboardButton("💣 Мины", callback_data="bet_mines_10")],
        [InlineKeyboardButton("🏀 Баскетбол", callback_data="bet_basket_10"), InlineKeyboardButton("⚽ Футбол", callback_data="bet_football_10")],
        [InlineKeyboardButton("🎯 Дартс", callback_data="bet_darts_10"), InlineKeyboardButton("🎰 Слоты", callback_data="bet_slots_10")],
        [InlineKeyboardButton("🎳 Боулинг", callback_data="bet_bowling_10"), InlineKeyboardButton("🪙 Орел и Решка", callback_data="bet_flip_10")],
        [InlineKeyboardButton("🚀 Краш", callback_data="bet_crash_10")],
        [InlineKeyboardButton("⬅ В меню", callback_data="menu")]
    ])

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Выдать баланс", callback_data="admin_add"), InlineKeyboardButton("➖ Забрать баланс", callback_data="admin_sub")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"), InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🔥 Топ по сериям", callback_data="admin_top_wins"), InlineKeyboardButton("🚨 Подозрительные", callback_data="admin_suspicious")],
        [InlineKeyboardButton("🎟 Создать промо", callback_data="admin_promo_help")],
        [InlineKeyboardButton("⬅ В меню", callback_data="menu")]
    ])

def deposit_card_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатил (Отправить чек)", callback_data="send_receipt")],
        [InlineKeyboardButton("⬅ Назад", callback_data="deposit")]
    ])

def admin_receipt_keyboard(user_id: int, coins: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Зачислить exact", callback_data=f"approve_exact_{user_id}_{coins}"),
            InlineKeyboardButton("✏ Ввести другую сумму", callback_data=f"approve_custom_{user_id}")
        ],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"decline_dep_{user_id}")]
    ])

def pvp_accept_keyboard(pvp_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚔️ Принять дуэль", callback_data=f"pvp_accept_{pvp_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"pvp_decline_{pvp_id}")
        ]
    ])

def roulette_type_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Красное (x2)", callback_data="rtype_red"), InlineKeyboardButton("⚫ Черное (x2)", callback_data="rtype_black")],
        [InlineKeyboardButton("🔢 Четное (x2)", callback_data="rtype_even"), InlineKeyboardButton("🔢 Нечетное (x2)", callback_data="rtype_odd")],
        [InlineKeyboardButton("🎯 Точное число (x36)", callback_data="rtype_number")],
        [InlineKeyboardButton("⬅ В меню", callback_data="menu")]
    ])

def basket_choice_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Забросит (x2.5)", callback_data="bchoice_in"), InlineKeyboardButton("❌ Промах (x1.8)", callback_data="bchoice_miss")],
        [InlineKeyboardButton("⬅ В меню", callback_data="menu")]
    ])

def flip_choice_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Орел (x1.9)", callback_data="fchoice_heads"), InlineKeyboardButton("🪙 Решка (x1.9)", callback_data="fchoice_tails")],
        [InlineKeyboardButton("⬅ В меню", callback_data="menu")]
    ])

def generate_mines(user_id, bet):
    grid = ["💎"] * 9
    mine_idx = random.sample(range(9), 2)
    for idx in mine_idx:
        grid[idx] = "💣"
    mines_games[user_id] = {
        "bet": bet,
        "grid": grid,
        "opened": [False] * 9,
        "mult": 1.0
    }

def mines_keyboard(user_id):
    game = mines_games.get(user_id)
    if not game:
        return InlineKeyboardMarkup([])
    buttons = []
    for i in range(9):
        text = "❓"
        if game["opened"][i]:
            text = game["grid"][i]
        buttons.append(InlineKeyboardButton(text, callback_data=f"mine_{i}"))
    kb = [buttons[0:3], buttons[3:6], buttons[6:9]]
    kb.append([InlineKeyboardButton(f"💰 Забрать ({int(game['bet'] * game['mult'])} 💰)", callback_data="mine_cashout")])
    return InlineKeyboardMarkup(kb)
# ---------- COMMAND HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = await get_user(user.id, user.username or "")
    is_admin = user.username == ADMIN_USERNAME
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0].split("_")[1])
            if db_user[4] == 0 and referrer_id != user.id:
                ref_user = await get_user(referrer_id)
                if ref_user:
                    await add_referral(user.id, referrer_id)
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            "🎉 Новый игрок перешел по вашей реферальной ссылке!\n💰 Вам зачислено +100 монет!",
                        )
                    except Exception:
                        pass
        except ValueError:
            pass
    await update.message.reply_text(
        "🎰 NEON CASINO", reply_markup=menu(is_admin)
    )

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username != ADMIN_USERNAME:
        return
    await update.message.reply_text(
        "👑 Админ Панель Управления:", reply_markup=admin_menu()
    )

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username != ADMIN_USERNAME:
        return
    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ **Использование:**\n"
            "1. `/broadcast Ваш текст`\n"
            "2. Или ответьте командой `/broadcast` на фото/сообщение.",
            parse_mode="Markdown",
        )
        return
    target_message = update.message.reply_to_message or update.message
    await perform_broadcast(target_message, context)

async def perform_broadcast(source_message, context: ContextTypes.DEFAULT_TYPE):
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
    success, blocked = 0, 0
    status_msg = await context.bot.send_message(
        source_message.chat.id, "🚀 Рассылка запущена..."
    )
    is_command_msg = source_message.text and source_message.text.startswith("/broadcast")
    for row in users:
        uid = row["user_id"]
        try:
            if is_command_msg:
                text_to_send = source_message.text.replace("/broadcast", "").strip()
                await context.bot.send_message(
                    uid, text_to_send, parse_mode="Markdown"
                )
            else:
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=source_message.chat.id,
                    message_id=source_message.message_id,
                )
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            blocked += 1
    await status_msg.edit_text(
        f"✅ **Рассылка завершена!**\n\n📥 Доставлено: **{success}**\n🚫 Блок: **{blocked}**",
        parse_mode="Markdown",
    )

async def create_promo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username != ADMIN_USERNAME:
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "❌ Использование: `/create_promo КОД СУММА АКТИВАЦИЙ`\n\nПример: `/create_promo FREE100 100 50`",
            parse_mode="Markdown",
        )
        return
    code = context.args[0].upper()
    try:
        reward = int(context.args[1])
        uses = int(context.args[2])
    except ValueError:
        await update.message.reply_text(
            "❌ Сумма и количество активаций должны быть числами!"
        )
        return
    if reward <= 0 or uses <= 0 or reward > 1_000_000_000:
        await update.message.reply_text(
            "❌ Некорректные значения суммы или активаций (максимум 1 млрд)!"
        )
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO promo_codes (code, reward, uses_left) VALUES ($1, $2, $3)",
                code, reward, uses
            )
        await update.message.reply_text(
            f"✅ Промокод создан!\n\n🎟 Код: `{code}`\n💰 Награда: **{reward}**\n👥 Активаций: **{uses}**",
            parse_mode="Markdown",
        )
    except asyncpg.UniqueViolationError:
        await update.message.reply_text(
            "❌ Промокод с таким именем уже существует!"
        )
    except Exception as e:
        print(f"Ошибка БД при создании промокода: {e}")
        await update.message.reply_text(
            "⚠️ Ошибка базы данных при создании промокода."
        )

async def promo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = await get_user(user.id, user.username or "")
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Использование: `/promo ВАШ_КОД`", parse_mode="Markdown"
        )
        return
    code = context.args[0].upper()
    try:
        async with db_pool.acquire() as conn:
            promo = await conn.fetchrow("SELECT reward, uses_left FROM promo_codes WHERE code=$1", code)
            if not promo:
                await update.message.reply_text("❌ Такого промокода не существует!")
                return
            reward, uses_left = promo["reward"], promo["uses_left"]
            if uses_left <= 0:
                await update.message.reply_text("❌ У этого промокода закончились активации!")
                return
            
            used = await conn.fetchrow("SELECT 1 FROM promo_uses WHERE user_id=$1 AND code=$2", user.id, code)
            if used:
                await update.message.reply_text("❌ Вы уже активировали этот промокод!")
                return
            
            async with conn.transaction():
                await conn.execute("INSERT INTO promo_uses (user_id, code) VALUES ($1, $2)", user.id, code)
                await conn.execute("UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code=$1", code)
                await set_balance(user.id, db_user[2] + reward)

        await update.message.reply_text(
            f"🎉 Промокод `{code}` успешно активирован!\n💰 Вам зачислено: **+{reward} монет**",
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"Ошибка при активации промокода: {e}")
        await update.message.reply_text(
            "⚠️ Произошла ошибка при активации промокода."
        )

async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = update.effective_user
    sender_db = await get_user(sender.id, sender.username or "")
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: `/pay @username сумма` или `/pay ID сумма`",
            parse_mode="Markdown",
        )
        return
    target_input = context.args[0]
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Сумма должна быть целым числом!")
        return
    if amount <= 0 or sender_db[2] < amount:
        await update.message.reply_text(
            "❌ Недостаточно денег или сумма <= 0!"
        )
        return
    target_db = await get_by_identifier(target_input)
    if not target_db:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    if target_db[0] == sender.id:
        await update.message.reply_text("❌ Нельзя переводить самому себе!")
        return
    fraud_alert = check_transfer_fraud(sender.id, sender.username or str(sender.id))
    if fraud_alert:
        asyncio.create_task(notify_admin_fraud(context, fraud_alert))
    await set_balance(sender.id, sender_db[2] - amount)
    await set_balance(target_db[0], target_db[2] + amount)
    target_name = f"@{target_db[1]}" if target_db[1] else str(target_db[0])
    await update.message.reply_text(
        f"✅ Вы успешно перевели {amount} 💰 пользователю {target_name}!"
    )
    try:
        sender_name = (
            f"@{sender.username}" if sender.username else str(sender.id)
        )
        await context.bot.send_message(
            target_db[0],
            f"💸 Игрок {sender_name} перевел вам {amount} 💰!",
        )
    except Exception:
        pass

async def pvp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global next_pvp_id
    challenger = update.effective_user
    challenger_db = await get_user(challenger.id, challenger.username or "")
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: `/pvp @username ставка`", parse_mode="Markdown"
        )
        return
    target_input = context.args[0]
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Ставка должна быть числом!")
        return
    if amount <= 0 or challenger_db[2] < amount:
        await update.message.reply_text(
            "❌ Недостаточно средств или некорректная ставка!"
        )
        return
    opponent_db = await get_by_identifier(target_input)
    if not opponent_db or opponent_db[0] == challenger.id:
        await update.message.reply_text(
            "❌ Игрок не найден или вы указали самого себя!"
        )
        return
    if opponent_db[2] < amount:
        await update.message.reply_text("❌ У противника недостаточно средств!")
        return
    pvp_id = next_pvp_id
    next_pvp_id += 1
    pvp_requests[pvp_id] = {
        "challenger_id": challenger.id,
        "opponent_id": opponent_db[0],
        "amount": amount,
    }
    challenger_name = (
        f"@{challenger.username}" if challenger.username else str(challenger.id)
    )
    opponent_name = (
        f"@{opponent_db[1]}" if opponent_db[1] else str(opponent_db[0])
    )
    await update.message.reply_text(
        f"⚔️ Вызвал на дуэль {opponent_name} на {amount} 💰!\nОжидаем подтверждения..."
    )
    try:
        await context.bot.send_message(
            opponent_db[0],
            f"⚔️ **PvP Вызов!**\n\nИгрок {challenger_name} вызывает вас на дуэль на кубиках 🎲!\nСтавка: **{amount} 💰**",
            parse_mode="Markdown",
            reply_markup=pvp_accept_keyboard(pvp_id),
        )
    except Exception:
        await update.message.reply_text(
            "❌ Не удалось отправить запрос противнику."
        )

# ---------- PAYMENTS (TELEGRAM STARS) ----------
async def precheckout_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    payment = update.message.successful_payment
    user = update.effective_user
    try:
        coins = int(payment.invoice_payload.split("_")[1])
    except Exception:
        coins = 0
    if coins > 0:
        db_user = await get_user(user.id, user.username or "")
        await set_balance(user.id, db_user[2] + coins)
        await update.message.reply_text(
            f"🎉 **ОПЛАТА УСПЕШНА!**\n\nВам зачислено: **+{coins} 💰**\nСпасибо за покупку! 🔥",
            parse_mode="Markdown",
            reply_markup=menu(user.username == ADMIN_USERNAME),
        )

# ---------- TEXT & PHOTO HANDLER ----------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = user.username == ADMIN_USERNAME
    if is_admin and user.id in awaiting_broadcast:
        awaiting_broadcast.remove(user.id)
        await perform_broadcast(update.message, context)
        return
    if user.id in awaiting_receipt:
        awaiting_receipt.remove(user.id)
        photo_id = update.message.photo[-1].file_id
        admin_db = await get_by_identifier(ADMIN_USERNAME)
        if not admin_db:
            await update.message.reply_text(
                "❌ Администратор пока недоступен. Напишите напрямую @Legendjau2"
            )
            return
        admin_id = admin_db[0]
        user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
        requested_coins = context.user_data.get("deposit_requested_coins", 0)
        caption_text = (
            f"💳 **НОВАЯ ЗАЯВКА НА ПОПОЛНЕНИЕ!**\n\n"
            f"👤 Игрок: {user_info}\n"
            f"🆔 ID: `{user.id}`\n"
            f"💰 Хочет закинуть: **{requested_coins} монет**"
        )
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_id,
                caption=caption_text,
                parse_mode="Markdown",
                reply_markup=admin_receipt_keyboard(user.id, requested_coins),
            )
            await update.message.reply_text(
                "✅ Скриншот отправлен администратору на проверку! Ожидайте зачисления монет."
            )
        except Exception:
            await update.message.reply_text(
                "❌ Ошибка отправки чека админу. Напишите напрямую @Legendjau2"
            )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    db_user = await get_user(user.id, user.username or "")
    is_admin = user.username == ADMIN_USERNAME
    if is_admin and user.id in awaiting_broadcast:
        awaiting_broadcast.remove(user.id)
        await perform_broadcast(update.message, context)
        return
    if user.id in awaiting_deposit_amount:
        if text.lower() in ["назад", "отмена", "/cancel"]:
            awaiting_deposit_amount.remove(user.id)
            await update.message.reply_text(
                "❌ Пополнение отменено.", reply_markup=menu(is_admin)
            )
            return
        awaiting_deposit_amount.remove(user.id)
        if not text.isdigit() or int(text) < 100:
            await update.message.reply_text(
                "❌ Минимальная сумма пополнения — **100 монет**!",
                parse_mode="Markdown",
                reply_markup=menu(is_admin),
            )
            return
        coins = int(text)
        context.user_data["deposit_requested_coins"] = coins
        uah_cost = round(coins / 100, 2)
        stars_cost = max(1, int(uah_cost * STARS_PER_UAH))
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"⭐ Оплатить {stars_cost} Stars",
                    callback_data=f"buy_stars_{coins}_{stars_cost}",
                )
            ],
            [
                InlineKeyboardButton(
                    f"💳 Оплатить картой ({uah_cost} грн)",
                    callback_data=f"buy_card_{coins}_{uah_cost}",
                )
            ],
            [InlineKeyboardButton("⬅ Назад", callback_data="deposit")],
        ])
        msg_text = (
            f"💳 **ПОПОЛНЕНИЕ БАЛАНСА**\n\n"
            f"💰 Вы получаете: **{coins} монет**\n"
            f"💵 Стоимость: **{uah_cost} грн** (или **⭐ {stars_cost} Stars**)\n\n"
            f"Выберите удобный способ оплаты:"
        )
        await update.message.reply_text(
            msg_text, parse_mode="Markdown", reply_markup=kb
        )
        return
    if is_admin and user.id in awaiting_admin:
        action_data = awaiting_admin.pop(user.id)
        if (
            isinstance(action_data, dict)
            and action_data.get("action") == "approve_deposit"
        ):
            target_id = action_data["target_id"]
            if not text.isdigit():
                await update.message.reply_text("❌ Введите число монет!")
                return
            add_coins = int(text)
            target_db = await get_user(target_id)
            await set_balance(target_id, target_db[2] + add_coins)
            await update.message.reply_text(
                f"✅ Баланс игрока `{target_id}` пополнен на **+{add_coins} монет**!",
                parse_mode="Markdown",
            )
            try:
                await context.bot.send_message(
                    target_id,
                    f"🎉 Ваш баланс успешно пополнен на **+{add_coins} 💰**!",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            return
        action = action_data
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Формат: `@username_или_id сумма`",
                reply_markup=admin_menu(),
            )
            return
        target = await get_by_identifier(parts[0])
        if not target:
            await update.message.reply_text(
                "❌ Пользователь не найден!", reply_markup=admin_menu()
            )
            return
        try:
            amount = int(parts[1])
        except ValueError:
            await update.message.reply_text(
                "❌ Сумма должна быть числом!", reply_markup=admin_menu()
            )
            return
        current_bal = target[2]
        new_bal = (
            current_bal + amount
            if action == "add"
            else max(0, current_bal - amount)
        )
        await set_balance(target[0], new_bal)
        sign = "+" if action == "add" else "-"
        await update.message.reply_text(
            f"✅ Пользователю @{target[1] or target[0]} изменено: {sign}{amount}\nНовый баланс: {new_bal}",
            reply_markup=admin_menu(),
        )
        return
    if user.id in awaiting_custom:
        raw_val = awaiting_custom.pop(user.id)
        if str(raw_val).startswith("roulette_num_"):
            amount = int(raw_val.split("_")[2])
            if not text.isdigit() or not (0 <= int(text) <= 36):
                await update.message.reply_text("❌ Введите число от 0 до 36!")
                return
            target_num = int(text)
            await play_roulette(
                update.message.chat_id, context, user, amount, "number", target_num
            )
            return
        game = raw_val
        if not text.isdigit():
            await update.message.reply_text(
                "❌ Введите число!", reply_markup=menu(is_admin)
            )
            return
        amount = int(text)
        if amount <= 0 or db_user[2] < amount:
            await update.message.reply_text(
                "❌ Ошибка в сумме или недостаточно монет!",
                reply_markup=menu(is_admin),
            )
            return
        await start_bet_process(update.message, context, user, game, amount)

# ---------- CALLBACK QUERY HANDLER ----------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    await query.answer()
    db_user = await get_user(user.id, user.username or "")
    is_admin = user.username == ADMIN_USERNAME
    if data == "menu":
        awaiting_deposit_amount.discard(user.id)
        awaiting_broadcast.discard(user.id)
        await query.edit_message_text(
            "🎰 NEON CASINO", reply_markup=menu(is_admin)
        )
    elif data == "games":
        await query.edit_message_text("🎮 Выберите игру:", reply_markup=games())
    elif data == "bal":
        await query.edit_message_text(
            f"💰 Ваш баланс: {db_user[2]} монет", reply_markup=menu(is_admin)
        )
    elif data == "top":
        top_msg = await top10()
        await query.edit_message_text(top_msg, reply_markup=menu(is_admin))
    elif data == "deposit":
        awaiting_deposit_amount.add(user.id)
        text = (
            "💳 **ПОПОЛНЕНИЕ БАЛАНСА**\n\n"
            "📌 Курс обмена: **100 💰 = 1 грн (1 Star ⭐)**\n\n"
            "✏ Напишите в чат **сумму монет**, которую вы хотите приобрести:\n"
            "_(Например: `500` или `1000`)_"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ Назад", callback_data="menu")]
        ])
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    elif data.startswith("buy_stars_"):
        _, _, coins_str, stars_str = data.split("_")
        coins = int(coins_str)
        stars = int(stars_str)
        title = f"Пополнение {coins} монет"
        description = (
            f"Зачисление {coins} монет на игровой баланс в NEON CASINO"
        )
        payload = f"deposit_{coins}"
        prices = [LabeledPrice(label=f"{coins} Монет", amount=stars)]
        await query.delete_message()
        await context.bot.send_invoice(
            chat_id=user.id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="deposit_stars",
        )
    elif data.startswith("buy_card_"):
        _, _, coins_str, uah_str = data.split("_")
        text = (
            f"💳 **Оплата картой**\n\n"
            f"💰 Вы получаете: **{coins_str} монет**\n"
            f"💵 К оплате: **{uah_str} грн**\n\n"
            f"📌 Реквизиты карты:\n`{CARD_NUMBER}`\n\n"
            f"После перевода нажмите **«Проверить»** и отправьте чек!"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=deposit_card_menu()
        )
    elif data == "send_receipt":
        awaiting_receipt.add(user.id)
        await query.edit_message_text(
            "📸 **Отправьте скриншот чека прямо сюда в чат:**",
            parse_mode="Markdown",
        )
    elif data.startswith("approve_exact_") and is_admin:
        parts = data.split("_")
        target_id = int(parts[2])
        add_coins = int(parts[3])
        target_db = await get_user(target_id)
        await set_balance(target_id, target_db[2] + add_coins)
        await query.message.edit_caption(
            caption=f"{query.message.caption}\n\n✅ **ОДОБРЕНО (+{add_coins} 💰)**",
            parse_mode="Markdown",
        )
        try:
            await context.bot.send_message(
                target_id,
                f"🎉 Ваш баланс успешно пополнен на **+{add_coins} 💰**!",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    elif data.startswith("approve_custom_") and is_admin:
        target_id = int(data.split("_")[2])
        awaiting_admin[user.id] = {
            "action": "approve_deposit",
            "target_id": target_id,
        }
        await query.message.reply_text(
            f"✏ Введите **любую другую сумму монет** для зачисления игроку `{target_id}`:",
            parse_mode="Markdown",
        )
    elif data.startswith("decline_dep_") and is_admin:
        target_id = int(data.split("_")[2])
        await query.message.edit_caption(
            caption=f"{query.message.caption}\n\n❌ **ОТКЛОНЕНО АДМИНОМ**",
            parse_mode="Markdown",
        )
        try:
            await context.bot.send_message(
                target_id,
                "❌ Ваша заявка на пополнение была отклонена администратором.",
            )
        except Exception:
            pass
    elif data == "pay_info":
        await query.edit_message_text(
            "💸 **Перевод средств**\n\nКоманда:\n`/pay @username сумма`",
            parse_mode="Markdown",
            reply_markup=menu(is_admin),
        )
    elif data == "pvp_info":
        await query.edit_message_text(
            "⚔️ **PvP Дуэли на кубиках**\n\nЧтобы вызвать игрока, введите:\n`/pvp @username ставка`",
            parse_mode="Markdown",
            reply_markup=menu(is_admin),
        )
    elif data == "ref_info":
        bot_username = context.bot.username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
        ref_text = (
            f"👥 **Реферальная программа**\n\n"
            f"Приглашайте друзей и получайте **+100 💰** за каждого!\n\n"
            f"🔗 Ваша ссылка:\n`{ref_link}`\n\n"
            f"📊 Приглашено друзей: **{db_user[5]}**"
        )
        await query.edit_message_text(
            ref_text, parse_mode="Markdown", reply_markup=menu(is_admin)
        )
    elif data == "promo_info":
        await query.edit_message_text(
            "🎟 **Активация промокода**\n\nЧтобы активировать промокод, введите:\n`/promo ВАШ_КОД`",
            parse_mode="Markdown",
            reply_markup=menu(is_admin),
        )
    elif data == "bonus":
        now = int(time.time())
        last_bonus = db_user[3]
        cooldown = 86400
        if now - last_bonus >= cooldown:
            await set_balance(user.id, db_user[2] + 50)
            await update_bonus_time(user.id)
            await query.edit_message_text(
                "🎁 Вы получили бонус +50 монет!", reply_markup=menu(is_admin)
            )
        else:
            left_seconds = cooldown - (now - last_bonus)
            hours = left_seconds // 3600
            minutes = (left_seconds % 3600) // 60
            await query.edit_message_text(
                f"⏳ Приходите через: {hours} ч. {minutes} мин.",
                reply_markup=menu(is_admin),
            )
    elif data == "admin_panel" and is_admin:
        await query.edit_message_text(
            "👑 Панель Администратора", reply_markup=admin_menu()
        )
    elif data == "admin_broadcast" and is_admin:
        awaiting_broadcast.add(user.id)
        await query.edit_message_text(
            "📢 **Рассылка сообщений**\n\n"
            "Отправьте текстом или **фотографией с подписью** сообщение, которое увидят все пользователи бота:",
            parse_mode="Markdown",
            reply_markup=admin_menu(),
        )
    elif data == "admin_add" and is_admin:
        awaiting_admin[user.id] = "add"
        await query.edit_message_text(
            "✏ Введите `@username` (или ID) и сумму:\nПример: `@steve 500`"
        )
    elif data == "admin_sub" and is_admin:
        awaiting_admin[user.id] = "sub"
        await query.edit_message_text(
            "✏ Введите `@username` (или ID) и сумму:\nПример: `@steve 200`"
        )
    elif data == "admin_stats" and is_admin:
        stats_msg = await get_stats()
        await query.edit_message_text(stats_msg, reply_markup=admin_menu())
    elif data == "admin_top_wins" and is_admin:
        sorted_wins = sorted(
            user_wins_history.items(), key=lambda x: x[1], reverse=True
        )[:10]
        text = "🔥 **ТОП ИГРОКОВ ПО СЕРИИ ПОБЕД**\n\n"
        if not sorted_wins or sorted_wins[0][1] == 0:
            text += "Пока нет активных серий побед."
        else:
            for i, (uid, wins) in enumerate(sorted_wins, 1):
                if wins > 0:
                    u_db = await get_user(uid)
                    uname = f"@{u_db[1]}" if u_db[1] else f"ID:`{uid}`"
                    text += f"{i}. {uname} — **{wins} побед подряд** 🔥\n"
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=admin_menu()
        )
    elif data == "admin_suspicious" and is_admin:
        text = "🚨 **ПОДОЗРИТЕЛЬНЫЕ ИГРОКИ (Анти-фрод)**\n\n"
        if not suspicious_users_log:
            text += "✅ Подозрительной активности не обнаружено."
        else:
            for uid, info in suspicious_users_log.items():
                uname = (
                    f"@{info['username']}"
                    if not info["username"].startswith("ID:")
                    else info["username"]
                )
                text += f"👤 {uname} (`{uid}`)\n⚠️ {info['reason']}\n🕒 Время: {info['time']}\n──────────────────\n"
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=admin_menu()
        )
    elif data == "admin_promo_help" and is_admin:
        text = (
            "🎟 **Команда создания промокодов (Только для Админа):**\n\n"
            "`/create_promo КОД СУММА КОЛИЧЕСТВО`\n\n"
            "Пример:\n`/create_promo NEON2026 150 20`"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=admin_menu()
        )
    elif data.startswith("bet_"):
        parts = data.split("_")
        game = parts[1]
        raw_amount = parts[2]
        bal = db_user[2]
        amount = bal if raw_amount == "all" else int(raw_amount)
        if amount <= 0 or bal < amount:
            await query.answer("❌ Недостаточно средств!", show_alert=True)
            return
        await start_bet_process(query.message, context, user, game, amount)
    elif data.startswith("custom_"):
        game = data.split("_")[1]
        awaiting_custom[user.id] = game
        await query.edit_message_text(f"✏ Введите сумму ставки для **{game}**:")
    elif data.startswith("rtype_"):
        choice = data.split("_")[1]
        amount = pending_bets.get(user.id)
        if choice == "number":
            awaiting_custom[user.id] = f"roulette_num_{amount}"
            await query.edit_message_text("🎯 Введите число от 0 до 36:")
        else:
            await play_roulette(
                query.message.chat_id, context, user, amount, choice
            )
    elif data.startswith("bchoice_"):
        choice = data.split("_")[1]
        amount = pending_bets.get(user.id)
        await play_basket(
            query.message.chat_id, context, user, amount, choice
        )
    elif data.startswith("fchoice_"):
        choice = data.split("_")[1]
        amount = pending_bets.get(user.id)
        await play_flip(
            query.message.chat_id, context, user, amount, choice
        )
    elif data.startswith("mine_") and user.id in mines_games:
        idx_str = data.split("_")[1]
        if idx_str == "cashout":
            game = mines_games.pop(user.id)
            win_amount = int(game["bet"] * game["mult"])
            await set_balance(user.id, db_user[2] + win_amount)
            
            alert = track_game_win(user.id, user.username, True)
            if alert:
                asyncio.create_task(notify_admin_fraud(context, alert))

            text = f"🎉 **ВЫ ЗАБРАЛИ ВЫИГРЫШ!**\n\n💰 Ваша награда: **{win_amount} монет** (x{round(game['mult'], 2)})"
            if query.message.text:
                await query.edit_message_text(text, reply_markup=menu(is_admin))
            else:
                await query.message.reply_text(text, reply_markup=menu(is_admin))
            return
        idx = int(idx_str)
        game = mines_games[user.id]
        if game["opened"][idx]:
            return
        game["opened"][idx] = True
        if game["grid"][idx] == "💣":
            mines_games.pop(user.id)
            track_game_win(user.id, user.username, False)
            text = f"💥 **БА-БАХ! Вы подорвались на мине!**\n\n💸 Потеряно: **{game['bet']} монет**"
            if query.message.text:
                await query.edit_message_text(text, reply_markup=menu(is_admin))
            else:
                await query.message.reply_text(text, reply_markup=menu(is_admin))
        else:
            game["mult"] += 0.25
            if query.message.text:
                await query.edit_message_text(
                    f"💎 Вы открыли безопасную ячейку!\nТекущий множитель: **x{round(game['mult'], 2)}**",
                    reply_markup=mines_keyboard(user.id),
                )
            else:
                await query.message.reply_text(
                    f"💎 Вы открыли безопасную ячейку!\nТекущий множитель: **x{round(game['mult'], 2)}**",
                    reply_markup=mines_keyboard(user.id),
                )
    elif data.startswith("crash_cashout_") and user.id in crash_games:
        game = crash_games[user.id]
        if game["active"]:
            game["active"] = False
            win_amount = int(game["bet"] * game["current"])
            await set_balance(user.id, db_user[2] + win_amount)
            
            alert = track_game_win(user.id, user.username, True)
            if alert:
                asyncio.create_task(notify_admin_fraud(context, alert))

            await query.edit_message_text(
                f"🚀 **ВЫ УСПЕЛИ ЗАБРАТЬ!**\n\n💰 Выигрыш: **{win_amount} монет** (x{round(game['current'], 2)})",
                reply_markup=menu(is_admin),
            )
    elif data.startswith("pvp_accept_"):
        pvp_id = int(data.split("_")[2])
        if pvp_id not in pvp_requests:
            await query.edit_message_text("❌ Запрос устарел или был отменен!")
            return
        p_data = pvp_requests.pop(pvp_id)
        if user.id != p_data["opponent_id"]:
            await query.answer("❌ Это предложение не для вас!", show_alert=True)
            return
        p1_db = await get_user(p_data["challenger_id"])
        p2_db = await get_user(user.id, user.username or "")
        amount = p_data["amount"]

        if p1_db[2] < amount or p2_db[2] < amount:
            await query.edit_message_text("❌ У одного из участников недостаточно средств!")
            return

        await set_balance(p1_db[0], p1_db[2] - amount)
        await set_balance(p2_db[0], p2_db[2] - amount)

        p1_msg = await context.bot.send_dice(chat_id=p1_db[0], emoji="🎲")
        p2_msg = await context.bot.send_dice(chat_id=p2_db[0], emoji="🎲")
        await asyncio.sleep(3.5)

        v1, v2 = p1_msg.dice.value, p2_msg.dice.value
        bank = amount * 2
        p1_name = f"@{p1_db[1]}" if p1_db[1] else str(p1_db[0])
        p2_name = f"@{p2_db[1]}" if p2_db[1] else str(p2_db[0])

        if v1 > v2:
            await set_balance(p1_db[0], p1_db[2] - amount + bank)
            res_text = f"🏆 **Победа {p1_name}!**\n\n🎲 Броски: {p1_name} ({v1}) vs {p2_name} ({v2})\n💰 Выигрыш: **+{bank} монет**"
        elif v2 > v1:
            await set_balance(p2_db[0], p2_db[2] - amount + bank)
            res_text = f"🏆 **Победа {p2_name}!**\n\n🎲 Броски: {p1_name} ({v1}) vs {p2_name} ({v2})\n💰 Выигрыш: **+{bank} монет**"
        else:
            await set_balance(p1_db[0], p1_db[2])
            await set_balance(p2_db[0], p2_db[2])
            res_text = f"🤝 **НИЧЬЯ!**\n\n🎲 Броски: {p1_name} ({v1}) vs {p2_name} ({v2})\n💰 Ставки возвращены."

        try:
            await context.bot.send_message(p1_db[0], res_text, parse_mode="Markdown")
        except Exception:
            pass
        try:
            await context.bot.send_message(p2_db[0], res_text, parse_mode="Markdown")
        except Exception:
            pass

    elif data.startswith("pvp_decline_"):
        pvp_id = int(data.split("_")[2])
        if pvp_id in pvp_requests:
            p_data = pvp_requests.pop(pvp_id)
            if user.id == p_data["opponent_id"]:
                await query.edit_message_text("❌ Вы отклонили вызов на дуэль.")
                try:
                    await context.bot.send_message(
                        p_data["challenger_id"],
                        "❌ Противник отклонил ваш вызов на PvP дуэль.",
                    )
                except Exception:
                    pass

# ---------- BET ROUTER & GAME ENGINES ----------
async def start_bet_process(target_message, context, user, game, amount):
    db_user = await get_user(user.id, user.username or "")
    if db_user[2] < amount:
        await target_message.reply_text("❌ Недостаточно средств!")
        return

    if game == "roulette":
        pending_bets[user.id] = amount
        await target_message.reply_text(
            f"🎰 Ставка: **{amount} 💰**\nВыберите тип ставки:",
            parse_mode="Markdown",
            reply_markup=roulette_type_menu(),
        )
    elif game == "basket":
        pending_bets[user.id] = amount
        await target_message.reply_text(
            f"🏀 Ставка: **{amount} 💰**\nСделайте прогноз:",
            parse_mode="Markdown",
            reply_markup=basket_choice_menu(),
        )
    elif game == "flip":
        pending_bets[user.id] = amount
        await target_message.reply_text(
            f"🪙 Ставка: **{amount} 💰**\nВыберите сторону:",
            parse_mode="Markdown",
            reply_markup=flip_choice_menu(),
        )
    elif game == "mines":
        await set_balance(user.id, db_user[2] - amount)
        generate_mines(user.id, amount)
        await target_message.reply_text(
            f"💣 **ИГРА МИНЫ**\n\nСтавка: **{amount} 💰**\nНажмите на ячейку, чтобы начать:",
            parse_mode="Markdown",
            reply_markup=mines_keyboard(user.id),
        )
    elif game == "crash":
        await set_balance(user.id, db_user[2] - amount)
        await run_crash_game(target_message, context, user, amount)
    elif game in ["football", "darts", "slots", "bowling"]:
        await set_balance(user.id, db_user[2] - amount)
        await play_dice_game(target_message, context, user, amount, game)

# ---------- ROULETTE ----------
async def play_roulette(
    chat_id, context, user, amount, choice_type, target_num=None
):
    db_user = await get_user(user.id, user.username or "")
    await set_balance(user.id, db_user[2] - amount)

    msg = await context.bot.send_message(
        chat_id, f"🎰 Крутим рулетку... (Ставка: **{amount} 💰**)"
    )
    await asyncio.sleep(2)

    winning_number = random.randint(0, 36)
    win = False
    coeff = 0

    if choice_type == "red":
        win = winning_number in RED_NUMBERS
        coeff = 2.0
    elif choice_type == "black":
        win = winning_number != 0 and winning_number not in RED_NUMBERS
        coeff = 2.0
    elif choice_type == "even":
        win = winning_number != 0 and winning_number % 2 == 0
        coeff = 2.0
    elif choice_type == "odd":
        win = winning_number % 2 != 0
        coeff = 2.0
    elif choice_type == "number":
        win = winning_number == target_num
        coeff = 36.0

    color = (
        "🔴 Красное"
        if winning_number in RED_NUMBERS
        else ("🟢 Зеленое (Зеро)" if winning_number == 0 else "⚫ Черное")
    )

    if win:
        win_amount = int(amount * coeff)
        await set_balance(user.id, db_user[2] - amount + win_amount)
        alert = track_game_win(user.id, user.username, True)
        if alert:
            asyncio.create_task(notify_admin_fraud(context, alert))

        text = f"🎯 Выпало: **{winning_number}** ({color})\n\n🎉 **ВЫ ВЫИГРАЛИ!**\n💰 Зачислено: **+{win_amount} монет**"
    else:
        track_game_win(user.id, user.username, False)
        text = f"🎯 Выпало: **{winning_number}** ({color})\n\n❌ **Вы проиграли!**\n💸 Потеряно: **{amount} монет**"

    await msg.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=menu(user.username == ADMIN_USERNAME),
    )

# ---------- BASKETBALL ----------
async def play_basket(chat_id, context, user, amount, choice):
    db_user = await get_user(user.id, user.username or "")
    await set_balance(user.id, db_user[2] - amount)

    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji="🏀")
    val = dice_msg.dice.value
    await asyncio.sleep(3.5)

    is_goal = val >= 4
    win = (choice == "in" and is_goal) or (choice == "miss" and not is_goal)
    coeff = 2.5 if choice == "in" else 1.8

    if win:
        win_amount = int(amount * coeff)
        await set_balance(user.id, db_user[2] - amount + win_amount)
        alert = track_game_win(user.id, user.username, True)
        if alert:
            asyncio.create_task(notify_admin_fraud(context, alert))

        text = f"🎉 **УГАДАЛИ!**\n💰 Ваш выигрыш: **+{win_amount} монет** (x{coeff})"
    else:
        track_game_win(user.id, user.username, False)
        text = f"❌ **НЕ УГАДАЛИ!**\n💸 Потеряно: **{amount} монет**"

    await context.bot.send_message(
        chat_id,
        text,
        parse_mode="Markdown",
        reply_markup=menu(user.username == ADMIN_USERNAME),
    )

# ---------- COIN FLIP ----------
async def play_flip(chat_id, context, user, amount, choice):
    db_user = await get_user(user.id, user.username or "")
    await set_balance(user.id, db_user[2] - amount)

    msg = await context.bot.send_message(chat_id, "🪙 Монетка крутится...")
    await asyncio.sleep(2)

    res = random.choice(["heads", "tails"])
    res_str = "🪙 Орел" if res == "heads" else "🪙 Решка"

    if choice == res:
        win_amount = int(amount * 1.9)
        await set_balance(user.id, db_user[2] - amount + win_amount)
        alert = track_game_win(user.id, user.username, True)
        if alert:
            asyncio.create_task(notify_admin_fraud(context, alert))

        text = f"Выпало: **{res_str}**!\n\n🎉 **Победа!** Вы выиграли **+{win_amount} монет**!"
    else:
        track_game_win(user.id, user.username, False)
        text = f"Выпало: **{res_str}**!\n\n❌ **Проигрыш!** Потеряно **{amount} монет**."

    await msg.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=menu(user.username == ADMIN_USERNAME),
    )

# ---------- CRASH GAME ENGINE ----------
async def run_crash_game(target_message, context, user, amount):
    crash_mult = round(random.uniform(1.1, 5.0), 2)
    current_mult = 1.00

    crash_games[user.id] = {
        "active": True,
        "bet": amount,
        "current": current_mult,
    }

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 ЗАБРАТЬ", callback_data=f"crash_cashout_{user.id}"
            )
        ]
    ])
    msg = await target_message.reply_text(
        f"🚀 **КРАШ ЗАПУЩЕН!**\n\nМножитель: **x1.00**",
        parse_mode="Markdown",
        reply_markup=kb,
    )

    while current_mult < crash_mult:
        await asyncio.sleep(1.2)
        if not crash_games.get(user.id, {}).get("active", False):
            return

        current_mult = round(current_mult + random.uniform(0.15, 0.40), 2)
        if current_mult >= crash_mult:
            break

        crash_games[user.id]["current"] = current_mult
        try:
            await msg.edit_text(
                f"🚀 **КРАШ ИДЕТ!**\n\nМножитель: **x{current_mult}**",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception:
            pass

    if crash_games.get(user.id, {}).get("active", False):
        crash_games.pop(user.id, None)
        track_game_win(user.id, user.username, False)
        await msg.edit_text(
            f"💥 **КРАШ УЛЕТЕЛ на x{crash_mult}!**\n\n💸 Вы не успели забрали деньги. Потеряно: **{amount} монет**.",
            reply_markup=menu(user.username == ADMIN_USERNAME),
        )

# ---------- OTHER DICE GAMES ----------
async def play_dice_game(target_message, context, user, amount, game):
    emoji_map = {
        "football": "⚽",
        "darts": "🎯",
        "slots": "🎰",
        "bowling": "🎳",
    }
    dice_msg = await target_message.reply_dice(emoji=emoji_map[game])
    val = dice_msg.dice.value
    await asyncio.sleep(3.5)

    db_user = await get_user(user.id, user.username or "")
    coeff = 0

    if game == "football":
        if val in [3, 4, 5]:
            coeff = 1.8
    elif game == "darts":
        if val == 6:
            coeff = 3.0
        elif val in [4, 5]:
            coeff = 1.5
    elif game == "bowling":
        if val == 6:
            coeff = 3.5
        elif val in [4, 5]:
            coeff = 1.5
    elif game == "slots":
        if val in [64, 1, 22, 43]:
            coeff = 5.0
        elif val in [16, 32, 48]:
            coeff = 2.0

    if coeff > 0:
        win_amount = int(amount * coeff)
        await set_balance(user.id, db_user[2] - amount + win_amount)
        alert = track_game_win(user.id, user.username, True)
        if alert:
            asyncio.create_task(notify_admin_fraud(context, alert))

        text = f"🎉 **ПОБЕДА!**\n\n💰 Ваш выигрыш: **+{win_amount} монет** (x{coeff})"
    else:
        track_game_win(user.id, user.username, False)
        text = f"❌ **ПРОИГРЫШ!**\n\n💸 Вы потеряли **{amount} монет**."

    await target_message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=menu(user.username == ADMIN_USERNAME),
    )

# ---------- MAIN BOT RUNNER ----------
async def post_init(application):
    await init_db_pool()
    print("✅ Пул соединений с асинхронной базой данных (asyncpg) успешно инициализирован!")

def main():
    if not TOKEN:
        print("❌ ОШИБКА: Токен BOT_TOKEN не найден в переменных окружения!")
        return

    app_bot = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # Handlers
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("admin", admin_cmd))
    app_bot.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app_bot.add_handler(CommandHandler("create_promo", create_promo_cmd))
    app_bot.add_handler(CommandHandler("promo", promo_cmd))
    app_bot.add_handler(CommandHandler("pay", pay_cmd))
    app_bot.add_handler(CommandHandler("pvp", pvp_cmd))

    # Payments
    app_bot.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app_bot.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback)
    )

    # Callbacks & Text/Photo
    app_bot.add_handler(CallbackQueryHandler(cb))
    app_bot.add_handler(
        MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo)
    )
    app_bot.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    print("🚀 Асинхронный бот NEON CASINO успешно запущен и готов к высокими нагрузкам!")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
