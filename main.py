import asyncio
import logging
import os
import random
import time
import psycopg2
import threading

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import (
    AIORateLimiter,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ---------- ЛОГИРОВАНИЕ ----------
# Пишем и в консоль (видно в логах Render), и в файл bot.log на диске.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
# Библиотека telegram сама логирует каждое HTTP-обращение на DEBUG — это шумно, приглушаем
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("casino_bot")

# Сюда main() положит event loop бота, чтобы Flask (другой поток) мог
# безопасно передавать апдейты из вебхука в очередь бота.
BOT_LOOP = None
BOT_APP = None

import threading
from flask import Flask, request

# --- 1. Создаем веб-сервер для Render ---
app = Flask(__name__)

@app.route('/')
def ping():
    return "Bot is alive!", 200


@app.route("/health")
def health():
    """UptimeRobot должен пинговать именно этот путь, а не '/'.
    Он реально проверяет, что бот жив и база данных отвечает."""
    try:
        _db_sync("SELECT 1")
    except Exception as e:
        logger.exception("Health-check: база данных не отвечает")
        return {"status": "error", "db": str(e)}, 500

    if BOT_APP is None or BOT_LOOP is None:
        return {"status": "starting"}, 503

    return {"status": "ok"}, 200


@app.route("/webhook/<secret>", methods=["POST"])
def telegram_webhook(secret):
    """Telegram стучится сюда при каждом новом сообщении/клике.
    Мы просто кладём апдейт в очередь бота и сразу отвечаем 200 —
    вся обработка идёт асинхронно в основном event loop бота."""
    if secret != WEBHOOK_SECRET:
        return "forbidden", 403

    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "forbidden", 403

    if BOT_APP is None or BOT_LOOP is None:
        return "not ready", 503

    data = request.get_json(force=True)
    update = Update.de_json(data, BOT_APP.bot)
    asyncio.run_coroutine_threadsafe(BOT_APP.update_queue.put(update), BOT_LOOP)
    return "ok", 200


def run_web_server():
    # Render сам передает порт через переменную PORT
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# Запускаем веб-сервер в отдельном фоновом потоке
threading.Thread(target=run_web_server, daemon=True).start()

# ---------- DB SETUP (Neon PostgreSQL) ----------
from concurrent.futures import ThreadPoolExecutor
from psycopg2 import pool as pg_pool

DB_URL = os.getenv("DATABASE_URL")

# Пул соединений: каждый запрос берёт СВОЁ соединение и выполняется в отдельном потоке,
# поэтому запросы разных игроков идут параллельно и не блокируют бота.
# Для Neon лучше использовать "pooled connection" строку (хост с -pooler).
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "15"))
DB_POOL = pg_pool.ThreadedConnectionPool(
    1,
    DB_POOL_SIZE,
    DB_URL,
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=3,
)
DB_EXECUTOR = ThreadPoolExecutor(max_workers=DB_POOL_SIZE, thread_name_prefix="db")


def _with_conn(func):
    """Берёт соединение из пула, выполняет func(conn), коммитит.
    Если Neon разорвал соединение — берёт свежее и повторяет один раз."""
    for attempt in (1, 2):
        conn = DB_POOL.getconn()
        try:
            result = func(conn)
            conn.commit()
            DB_POOL.putconn(conn)
            return result
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            logger.warning("Соединение с Neon разорвано, переподключаюсь...")
            DB_POOL.putconn(conn, close=True)
            if attempt == 2:
                raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            DB_POOL.putconn(conn)
            raise


def _db_sync(query, params=None, fetch=None):
    """fetch: None -> вернёт rowcount, "one" -> строку, "all" -> список строк."""
    q = query.replace("?", "%s")

    def work(conn):
        with conn.cursor() as c:
            c.execute(q, params)
            if fetch == "one":
                return c.fetchone()
            if fetch == "all":
                return c.fetchall()
            return c.rowcount

    return _with_conn(work)


async def db(query, params=None, fetch=None):
    """Асинхронный запрос к БД (не блокирует event loop)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(DB_EXECUTOR, _db_sync, query, params, fetch)


# Создаем таблицы в синтаксисе PostgreSQL
_db_sync("""
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    balance INTEGER DEFAULT 100,
    last_bonus BIGINT DEFAULT 0,
    referrer_id BIGINT DEFAULT 0,
    referrals_count INTEGER DEFAULT 0
)
""")
_db_sync("""
CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    reward INTEGER,
    uses_left INTEGER
)
""")
_db_sync("""
CREATE TABLE IF NOT EXISTS promo_uses (
    user_id BIGINT,
    code TEXT,
    PRIMARY KEY (user_id, code)
)
""")
_db_sync("""
CREATE TABLE IF NOT EXISTS active_games (
    user_id BIGINT PRIMARY KEY,
    game_type TEXT,
    chat_id BIGINT,
    bet INTEGER,
    started_at BIGINT
)
""")


# ---------- GRACEFUL SHUTDOWN: сохранение активных ставок ----------
# Пока игрок играет в mines/crash, деньги уже списаны, а игра живёт только
# в памяти процесса (mines_games / crash_games). Если Render перезапустит
# бота посреди игры, эта память исчезнет вместе с ней — но строка в
# active_games останется, и при следующем старте мы вернём ставку.
async def track_active_game(uid, game_type, chat_id, bet):
    await db(
        "INSERT INTO active_games (user_id, game_type, chat_id, bet, started_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id) DO UPDATE SET game_type=EXCLUDED.game_type, "
        "chat_id=EXCLUDED.chat_id, bet=EXCLUDED.bet, started_at=EXCLUDED.started_at",
        (uid, game_type, chat_id, bet, int(time.time())),
    )


async def clear_active_game(uid):
    await db("DELETE FROM active_games WHERE user_id=?", (uid,))


async def recover_orphaned_games(bot):
    """Вызывается один раз при старте бота. Возвращает деньги за игры,
    которые остались висеть из-за падения/перезапуска процесса."""
    rows = await db(
        "SELECT user_id, game_type, chat_id, bet FROM active_games", None, "all"
    )
    if not rows:
        return

    logger.warning("Найдено %d незавершённых игр после перезапуска — возвращаю ставки", len(rows))
    for uid, game_type, chat_id, bet in rows:
        try:
            await add_balance(uid, bet)
            await clear_active_game(uid)
            await bot.send_message(
                chat_id,
                f"🔄 Бот перезапустился во время вашей игры ({game_type}).\n"
                f"💰 Ставка {bet} монет возвращена на баланс.",
            )
            logger.info("Возвращена ставка %s игроку %s (игра %s)", bet, uid, game_type)
        except Exception:
            logger.exception("Не удалось вернуть ставку игроку %s", uid)



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

# Секрет для адреса вебхука (/webhook/<secret>), чтобы никто чужой не мог
# слать нам поддельные "апдейты". Задайте свой в переменных окружения Render.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me-in-render-env")
# Render сам прописывает этот адрес для веб-сервисов. Если его нет (локальный
# запуск) — бот сам упадёт обратно на polling, ничего настраивать не нужно.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
CARD_NUMBER = "XXXX-XXXX-XXXX-XXXX"  # Укажите номер вашей карты

# Курс: 100 монет = 1 грн, 1 Star (XTR) = 1 грн
STARS_PER_UAH = 1.0


# ---------- STATE ----------
mines_games = {}
crash_games = {}
pending_bets = {}
awaiting_custom = {}
awaiting_admin = {}
awaiting_deposit_amount = set()
awaiting_receipt = set()
awaiting_broadcast = set()

# Anti-Fraud Memory
user_transfers_history = {}
user_wins_history = {}
suspicious_users_log = {}  # {user_id: {"username": str, "reason": str, "time": str}}

# PvP
pvp_requests = {}
next_pvp_id = 1

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


# ---------- DB HELPERS (async, атомарные операции) ----------
USER_COLS = "user_id, username, balance, last_bonus, referrer_id, referrals_count"


async def get_user(uid, username=""):
    user = await db(f"SELECT {USER_COLS} FROM users WHERE user_id=?", (uid,), "one")

    if not user:
        await db(
            "INSERT INTO users (user_id, username) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid, username),
        )
        # Повторно запрашиваем свежие данные из базы
        return await db(f"SELECT {USER_COLS} FROM users WHERE user_id=?", (uid,), "one")

    if username and user[1] != username:
        await db("UPDATE users SET username=? WHERE user_id=?", (username, uid))
        user = (user[0], username, user[2], user[3], user[4], user[5])

    return user


async def set_balance(uid, balance):
    await db("UPDATE users SET balance=? WHERE user_id=?", (balance, uid))


async def add_balance(uid, amount):
    """Атомарно прибавляет amount к балансу (без гонок при одновременных действиях)."""
    await db("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, uid))


async def spend(uid, amount):
    """Атомарно списывает amount. Возвращает False, если не хватает монет."""
    n = await db(
        "UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?",
        (amount, uid, amount),
    )
    return n == 1


async def claim_bonus(uid, amount=50, cooldown=86400):
    """Атомарно выдаёт ежедневный бонус. False — если ещё рано."""
    now = int(time.time())
    n = await db(
        "UPDATE users SET balance = balance + ?, last_bonus = ? "
        "WHERE user_id=? AND last_bonus <= ?",
        (amount, now, uid, now - cooldown),
    )
    return n == 1


async def add_referral(new_user_id, referrer_id):
    await db(
        "UPDATE users SET referrer_id=? WHERE user_id=?",
        (referrer_id, new_user_id),
    )
    await db(
        "UPDATE users SET referrals_count = referrals_count + 1, balance = balance + 100 WHERE user_id=?",
        (referrer_id,),
    )


async def get_by_identifier(identifier):
    identifier = str(identifier).strip().lstrip("@")

    if identifier.isdigit():
        return await db("SELECT * FROM users WHERE user_id=?", (int(identifier),), "one")
    return await db("SELECT * FROM users WHERE username=?", (identifier,), "one")


async def top10():
    rows = await db(
        "SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10",
        None,
        "all",
    )
    text = "🏆 TOP 10 ИГРОКОВ\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. @{r[0] or 'без_ника'} — {r[1]} 💰\n"
    return text


async def get_stats():
    count, total_bal = await db("SELECT COUNT(*), SUM(balance) FROM users", None, "one")
    return f"📊 СТАТИСТИКА БОТА\n\n👥 Всего пользователей: {count}\n💰 Всего монет в системе: {total_bal or 0}"


def _redeem_promo_sync(uid, code):
    """Вся активация промокода — одной транзакцией (нельзя активировать дважды одновременно)."""

    def work(conn):
        with conn.cursor() as c:
            c.execute(
                "SELECT reward, uses_left FROM promo_codes WHERE code=%s FOR UPDATE",
                (code,),
            )
            promo = c.fetchone()
            if not promo:
                return ("none", 0)
            reward, uses_left = promo
            if uses_left <= 0:
                return ("empty", 0)
            c.execute(
                "SELECT 1 FROM promo_uses WHERE user_id=%s AND code=%s",
                (uid, code),
            )
            if c.fetchone():
                return ("used", 0)
            c.execute(
                "INSERT INTO promo_uses (user_id, code) VALUES (%s, %s)",
                (uid, code),
            )
            c.execute(
                "UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code=%s",
                (code,),
            )
            c.execute(
                "UPDATE users SET balance = balance + %s WHERE user_id=%s",
                (reward, uid),
            )
            return ("ok", reward)

    return _with_conn(work)


async def redeem_promo(uid, code):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(DB_EXECUTOR, _redeem_promo_sync, uid, code)


# ---------- ANTI-FRAUD LOGIC ----------
async def notify_admin_fraud(context: ContextTypes.DEFAULT_TYPE, log_text: str):
    admin_db = await get_by_identifier(ADMIN_USERNAME)
    if admin_db:
        try:
            await context.bot.send_message(
                admin_db[0],
                f"🚨 **АНТИ-ФРОД СИСТЕМА**\n\n{log_text}",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит ЛЮБОЕ необработанное исключение в любом хендлере.
    Без этого один баг в одной игре мог бы уронить обработку апдейта
    молча — теперь это видно в логах и прилетает алертом админу."""
    logger.error("Необработанная ошибка при обработке апдейта", exc_info=context.error)

    try:
        admin_db = await get_by_identifier(ADMIN_USERNAME)
        if admin_db:
            err_text = str(context.error)[:500]
            await context.bot.send_message(
                admin_db[0],
                f"🚨 **ОШИБКА В БОТЕ**\n\n`{err_text}`",
                parse_mode="Markdown",
            )
    except Exception:
        logger.exception("Не удалось отправить алерт админу об ошибке")


def check_transfer_fraud(sender_id, sender_name):
    now = time.time()
    history = user_transfers_history.get(sender_id, [])
    history = [t for t in history if now - t < 60]
    history.append(now)
    user_transfers_history[sender_id] = history

    if len(history) >= 5:
        reason = f"Спам переводами: {len(history)} за 1 мин"
        time_str = time.strftime("%H:%M:%S")
        suspicious_users_log[sender_id] = {
            "username": sender_name,
            "reason": reason,
            "time": time_str,
        }
        return f"⚠️ Пользователь {sender_name} (`ID:{sender_id}`) совершил **{len(history)} переводов за 1 минуту**!"
    return None


def track_game_win(user_id, username, is_win):
    if is_win:
        user_wins_history[user_id] = user_wins_history.get(user_id, 0) + 1
        wins = user_wins_history[user_id]
        if wins >= 7:
            reason = f"Серия из {wins} побед подряд"
            time_str = time.strftime("%H:%M:%S")
            suspicious_users_log[user_id] = {
                "username": username or str(user_id),
                "reason": reason,
                "time": time_str,
            }
            return f"🔥 Игрок @{username or user_id} одержал **{wins} побед подряд**!"
    else:
        user_wins_history[user_id] = 0
    return None


# ---------- MENUS ----------
def menu(is_admin=False):
    kb = [
        [
            InlineKeyboardButton("🎮 Игры", callback_data="games"),
            InlineKeyboardButton("⚔️ PvP дуэль", callback_data="pvp_info"),
        ],
        [
            InlineKeyboardButton("🏆 Топ", callback_data="top"),
            InlineKeyboardButton("💰 Баланс", callback_data="bal"),
        ],
        [
            InlineKeyboardButton("💳 Пополнить", callback_data="deposit"),
            InlineKeyboardButton("🎁 Бонус +50", callback_data="bonus"),
        ],
        [
            InlineKeyboardButton("💸 Перевод", callback_data="pay_info"),
            InlineKeyboardButton(
                "👥 Рефералы (+100 💰)", callback_data="ref_info"
            ),
        ],
        [InlineKeyboardButton("🎟 Промокод", callback_data="promo_info")],
    ]
    if is_admin:
        kb.append(
            [
                InlineKeyboardButton(
                    "👑 Админ Панель", callback_data="admin_panel"
                )
            ]
        )
    return InlineKeyboardMarkup(kb)


def deposit_card_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔍 Проверить (Отправить чек)", callback_data="send_receipt"
            )
        ],
        [InlineKeyboardButton("⬅ Назад", callback_data="deposit")],
    ])


def admin_receipt_keyboard(user_id, requested_coins):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"✅ Зачислить {requested_coins} 💰", callback_data=f"approve_exact_{user_id}_{requested_coins}"
            )
        ],
        [
            InlineKeyboardButton(
                "✏ Другая сумма", callback_data=f"approve_custom_{user_id}"
            ),
            InlineKeyboardButton(
                "❌ Отклонить", callback_data=f"decline_dep_{user_id}"
            ),
        ]
    ])


def games():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Краш (Aviator)", callback_data="crash"),
            InlineKeyboardButton("🎰 Рулетка", callback_data="roulette"),
        ],
        [
            InlineKeyboardButton("💣 Мины", callback_data="mines"),
            InlineKeyboardButton("🏀 Баскет", callback_data="basket"),
        ],
        [
            InlineKeyboardButton("⚽ Футбол", callback_data="football"),
            InlineKeyboardButton("🎯 Дартс", callback_data="darts"),
        ],
        [
            InlineKeyboardButton("🪙 Монетка", callback_data="flip"),
            InlineKeyboardButton("🎰 Слоты", callback_data="slots"),
        ],
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


def roulette_type_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Красное (x2)", callback_data="rtype_red"),
            InlineKeyboardButton("⚫ Черное (x2)", callback_data="rtype_black"),
        ],
        [
            InlineKeyboardButton("2️⃣ Четное (x2)", callback_data="rtype_even"),
            InlineKeyboardButton("1️⃣ Нечетное (x2)", callback_data="rtype_odd"),
        ],
        [InlineKeyboardButton("🎯 Число (x36)", callback_data="rtype_number")],
        [InlineKeyboardButton("⬅ Назад", callback_data="games")],
    ])


def basket_choice_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏀 Залетит (x2.5)", callback_data="bchoice_in"
            )
        ],
        [InlineKeyboardButton("❌ Мимо (x1.8)", callback_data="bchoice_miss")],
    ])


def flip_choice_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🪙 Орел (x1.9)", callback_data="fchoice_heads"
            )
        ],
        [
            InlineKeyboardButton(
                "🪙 Решка (x1.9)", callback_data="fchoice_tails"
            )
        ],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"),
            InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("➕ Выдать баланс", callback_data="admin_add"),
            InlineKeyboardButton("➖ Забрать баланс", callback_data="admin_sub"),
        ],
        [
            InlineKeyboardButton("🔥 Топ побед", callback_data="admin_top_wins"),
            InlineKeyboardButton("🚨 Подозрительные", callback_data="admin_suspicious"),
        ],
        [InlineKeyboardButton("🎟 Промокоды", callback_data="admin_promo_help")],
        [InlineKeyboardButton("⬅ В главное меню", callback_data="menu")],
    ])


def pvp_accept_keyboard(pvp_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Принять дуэль", callback_data=f"pvp_accept_{pvp_id}"
            ),
            InlineKeyboardButton(
                "❌ Отклонить", callback_data=f"pvp_decline_{pvp_id}"
            ),
        ]
    ])


# ---------- MINES SYSTEM ----------
async def generate_mines(uid, bet, chat_id):
    grid = ["💣"] * 5 + ["💎"] * 20
    random.shuffle(grid)
    mines_games[uid] = {
        "grid": grid,
        "opened": [False] * 25,
        "bet": bet,
        "mult": 1.0,
    }
    await track_active_game(uid, "mines", chat_id, bet)


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
                        game["grid"][index], callback_data="noop"
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
    users = await db("SELECT user_id FROM users", None, "all")
    success, blocked = 0, 0
    status_msg = await context.bot.send_message(
        source_message.chat.id, "🚀 Рассылка запущена..."
    )

    # Определяем, что за отправка
    is_command_msg = source_message.text and source_message.text.startswith("/broadcast")

    for (uid,) in users:
        try:
            if is_command_msg:
                # Если была команда вида /broadcast текст
                text_to_send = source_message.text.replace("/broadcast", "").strip()
                await context.bot.send_message(
                    uid, text_to_send, parse_mode="Markdown"
                )
            else:
                # В остальных случаях копируем сообщение 1-в-1 (включая Фото с Подписью)
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=source_message.chat_id,
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

    # Защита от огромных чисел и отрицательных значений
    if reward <= 0 or uses <= 0 or reward > 1_000_000_000:
        await update.message.reply_text(
            "❌ Некорректные значения суммы или активаций (максимум 1 млрд)!"
        )
        return

    try:
        await db(
            "INSERT INTO promo_codes (code, reward, uses_left) VALUES (?, ?, ?)",
            (code, reward, uses),
        )
        await update.message.reply_text(
            f"✅ Промокод создан!\n\n🎟 Код: `{code}`\n💰 Награда: **{reward}**\n👥 Активаций: **{uses}**",
            parse_mode="Markdown",
        )
    except psycopg2.IntegrityError:
        await update.message.reply_text(
            "❌ Промокод с таким именем уже существует!"
        )
    except Exception as e:
        logger.exception("Ошибка БД при создании промокода")
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
        status, reward = await redeem_promo(user.id, code)

        if status == "none":
            await update.message.reply_text(
                "❌ Такого промокода не существует!"
            )
            return

        if status == "empty":
            await update.message.reply_text(
                "❌ У этого промокода закончились активации!"
            )
            return

        if status == "used":
            await update.message.reply_text(
                "❌ Вы уже активировали этот промокод!"
            )
            return

        await update.message.reply_text(
            f"🎉 Промокод `{code}` успешно активирован!\n💰 Вам зачислено: **+{reward} монет**",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Ошибка при активации промокода")
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

    if not await spend(sender.id, amount):
        await update.message.reply_text(
            "❌ Недостаточно денег или сумма <= 0!"
        )
        return
    await add_balance(target_db[0], amount)
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
        await get_user(user.id, user.username or "")
        await add_balance(user.id, coins)
        await update.message.reply_text(
            f"🎉 **ОПЛАТА УСПЕШНА!**\n\nВам зачислено: **+{coins} 💰**\nСпасибо за покупку! 🔥",
            parse_mode="Markdown",
            reply_markup=menu(user.username == ADMIN_USERNAME),
        )


# ---------- TEXT & PHOTO HANDLER ----------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = user.username == ADMIN_USERNAME

    # Рассылка фото с подписью через кнопку в Админ-Панели
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

    # Если администратор делает рассылку текста через кнопку
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
            await get_user(target_id)
            await add_balance(target_id, add_coins)
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
        await query.edit_message_text(await top10(), reply_markup=menu(is_admin))
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
        await get_user(target_id)
        await add_balance(target_id, add_coins)
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
        if await claim_bonus(user.id, 50, cooldown):
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
        await query.edit_message_text(await get_stats(), reply_markup=admin_menu())
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
    # CRASH CASHOUT
    elif data == "crash_cashout":
        game = crash_games.get(user.id)
        if game and not game["crashed"] and not game["cashed_out"]:
            game["cashed_out"] = True
            reward = int(game["bet"] * game["mult"])
            await add_balance(user.id, reward)
            await query.answer(
                f"🎉 Вы успешно забрали {reward} 💰 (x{game['mult']:.2f})!",
                show_alert=True,
            )
    # PVP ACCEPT / DECLINE
    elif data.startswith("pvp_accept_"):
        pvp_id = int(data.split("_")[2])
        req = pvp_requests.pop(pvp_id, None)
        if not req:
            await query.edit_message_text(
                "❌ Дуэль не найдена или уже завершена."
            )
            return
        if user.id != req["opponent_id"]:
            await query.answer("❌ Это вызов не для вас!", show_alert=True)
            return
        c_db = await get_user(req["challenger_id"])
        o_db = await get_user(req["opponent_id"])
        amount = req["amount"]
        if c_db[2] < amount or o_db[2] < amount:
            await query.edit_message_text(
                "❌ У одного из игроков недостаточно средств!"
            )
            return
        if not await spend(c_db[0], amount):
            await query.edit_message_text(
                "❌ У одного из игроков недостаточно средств!"
            )
            return
        if not await spend(o_db[0], amount):
            await add_balance(c_db[0], amount)
            await query.edit_message_text(
                "❌ У одного из игроков недостаточно средств!"
            )
            return
        await query.edit_message_text(
            "⚔️ **Дуэль началась! Бросаем кубики...**", parse_mode="Markdown"
        )
        chat_id = query.message.chat_id
        await run_pvp_match(chat_id, context, c_db[0], o_db[0], amount)
    elif data.startswith("pvp_decline_"):
        pvp_id = int(data.split("_")[2])
        req = pvp_requests.pop(pvp_id, None)
        if req:
            await query.edit_message_text("❌ Вы отклонили вызов на дуэль.")
            try:
                await context.bot.send_message(
                    req["challenger_id"],
                    "❌ Противник отклонил ваш вызов на дуэль.",
                )
            except Exception:
                pass
    # ROULETTE TYPES
    elif data.startswith("rtype_"):
        btype = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)
        if not bet_info:
            await query.edit_message_text(
                "❌ Сессия истекла.", reply_markup=menu(is_admin)
            )
            return

        if btype == "number":
            awaiting_custom[user.id] = f"roulette_num_{bet_info['amount']}"
            await query.edit_message_text("🎯 Напишите число от 0 до 36 в чат:")
        else:
            await query.delete_message()
            await play_roulette(
                query.message.chat_id, context, user, bet_info["amount"], btype
            )
    # GAMES SELECTION & BETS
    elif data in {
        "basket",
        "football",
        "darts",
        "flip",
        "slots",
        "bowling",
        "mines",
        "crash",
        "roulette",
    }:
        await query.edit_message_text(
            "💸 Выберите или введите ставку:", reply_markup=bets(data)
        )
    elif data.startswith("custom_"):
        game = data.split("_")[1]
        awaiting_custom[user.id] = game
        await query.edit_message_text(
            "✏ Напишите сумму ставки сообщением в чат:"
        )
    elif data.startswith("bet_"):
        _, game, amount_str = data.split("_")
        amount = db_user[2] if amount_str == "all" else int(amount_str)
        if amount <= 0 or db_user[2] < amount:
            await query.edit_message_text(
                "❌ Недостаточно средств!", reply_markup=menu(is_admin)
            )
            return
        await start_bet_process(query, context, user, game, amount)
    # BASKET / FLIP CHOICE HANDLERS
    elif data.startswith("bchoice_"):
        choice = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)
        if not bet_info:
            await query.edit_message_text(
                "❌ Сессия истекла.", reply_markup=menu(is_admin)
            )
            return
        await query.delete_message()
        await play_basket(
            query.message.chat_id, context, user, bet_info["amount"], choice
        )
    elif data.startswith("fchoice_"):
        choice = data.split("_")[1]
        bet_info = pending_bets.pop(user.id, None)
        if not bet_info:
            await query.edit_message_text(
                "❌ Сессия истекла.", reply_markup=menu(is_admin)
            )
            return
        await query.delete_message()
        await play_flip(
            query.message.chat_id, context, user, bet_info["amount"], choice
        )
    # MINES
    elif data.startswith("mine_") and data != "mine_cashout":
        index = int(data.split("_")[1])
        game = mines_games.get(user.id)
        if not game or game["opened"][index]:
            return
        game["opened"][index] = True
        if game["grid"][index] == "💣":
            mines_games.pop(user.id)
            await clear_active_game(user.id)
            await query.edit_message_text(
                f"💥 БОМБА! Вы подорвались!\n❌ Потеряно: {game['bet']}",
                reply_markup=menu(is_admin),
            )
        else:
            game["mult"] += 0.25
            await query.edit_message_reply_markup(mines_keyboard(user.id))
    elif data == "mine_cashout":
        game = mines_games.pop(user.id, None)
        if not game:
            return
        await clear_active_game(user.id)
        reward = int(game["bet"] * game["mult"])
        await add_balance(user.id, reward)
        await query.edit_message_text(
            f"💰 Забрано x{round(game['mult'], 2)}!\n🎉 Выигрыш: +{reward}",
            reply_markup=menu(is_admin),
        )


# ---------- AVIATOR / CRASH GAME ENGINE ----------
def generate_crash_multiplier() -> float:
    rand = random.uniform(0, 100)
    # 65% шанс: Падение в диапазоне 1.05 - 1.30 (Частый краш на старте)
    if rand < 65:
        return round(random.uniform(1.05, 1.30), 2)
    # 20% шанс: Средний полет 1.31 - 1.60
    elif rand < 85:
        return round(random.uniform(1.31, 1.60), 2)
    # 10% шанс: Хороший полет 1.61 - 2.20
    elif rand < 95:
        return round(random.uniform(1.61, 2.20), 2)
    # 5% шанс: Высокий коэффициент 2.21 - 3.50
    else:
        return round(random.uniform(2.21, 3.50), 2)


async def run_crash_game(chat_id, context, user, amount):
    is_admin = user.username == ADMIN_USERNAME
    if not await spend(user.id, amount):
        await context.bot.send_message(
            chat_id,
            "❌ Недостаточно средств!",
            reply_markup=menu(user.username == ADMIN_USERNAME),
        )
        return

    crash_mult = generate_crash_multiplier()

    crash_games[user.id] = {
        "bet": amount,
        "crashed": False,
        "cashed_out": False,
        "mult": 1.00,
    }
    await track_active_game(user.id, "crash", chat_id, amount)
    current_mult = 1.00
    msg = await context.bot.send_message(
        chat_id,
        f"🚀 **AVIATOR / КРАШ**\n\nСтавка: **{amount} 💰**\nКоэффициент: **x1.00**",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "💰 Забрать x1.00", callback_data="crash_cashout"
            )
        ]]),
    )

    while current_mult < crash_mult:
        await asyncio.sleep(0.8)
        game = crash_games.get(user.id)
        if not game or game["cashed_out"]:
            break
        current_mult = round(
            current_mult + random.choice([0.03, 0.05, 0.08]), 2
        )
        game["mult"] = current_mult
        if current_mult >= crash_mult:
            game["crashed"] = True
            break
        try:
            await msg.edit_text(
                f"🚀 **AVIATOR / КРАШ**\n\nСтавка: **{amount} 💰**\nКоэффициент: **x{current_mult:.2f}** 📈",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"💰 Забрать x{current_mult:.2f}",
                        callback_data="crash_cashout",
                    )
                ]]),
            )
        except Exception:
            pass

    game = crash_games.pop(user.id, None)
    await clear_active_game(user.id)
    if game:
        if game["cashed_out"]:
            reward = int(amount * game["mult"])
            alert = track_game_win(user.id, user.username, True)
            if alert:
                asyncio.create_task(notify_admin_fraud(context, alert))

            await msg.edit_text(
                f"🎉 **УСПЕШНЫЙ ЗАБОР!**\n\nВы успели забрать до краша!\n📈 Коэффициент: **x{game['mult']:.2f}**\n💰 Выигрыш: **+{reward} монет**",
                reply_markup=menu(is_admin),
            )
        else:
            track_game_win(user.id, user.username, False)
            await msg.edit_text(
                f"💥 **КРАШ! Самолет улетел!**\n\n📈 Самолет улетел на: **x{crash_mult:.2f}**\n💸 Потеряно: **{amount} 💰**",
                reply_markup=menu(is_admin),
            )


# ---------- ROULETTE LOGIC ----------
async def play_roulette(chat_id, context, user, amount, bet_type, target_value=None):
    is_admin = user.username == ADMIN_USERNAME
    if not await spend(user.id, amount):
        await context.bot.send_message(
            chat_id,
            "❌ Недостаточно средств!",
            reply_markup=menu(user.username == ADMIN_USERNAME),
        )
        return

    msg = await context.bot.send_message(
        chat_id, "🎰 **Рулетка запускается...**", parse_mode="Markdown"
    )
    anim_frames = ["🟢 [0]", "🔴 [32]", "⚫ [15]", "🔴 [19]", "⚫ [4]", "🔴 [21]"]
    for frame in anim_frames:
        await asyncio.sleep(0.4)
        try:
            await msg.edit_text(
                f"🎰 **Рулетка крутится:** {frame}", parse_mode="Markdown"
            )
        except Exception:
            pass

    win_num = random.randint(0, 36)
    color = (
        "🟢 Зеленое (Зеро)"
        if win_num == 0
        else ("🔴 Красное" if win_num in RED_NUMBERS else "⚫ Черное")
    )

    is_win = False
    multiplier = 0.0

    if bet_type == "red" and win_num in RED_NUMBERS:
        is_win, multiplier = True, 2.0
    elif bet_type == "black" and win_num != 0 and win_num not in RED_NUMBERS:
        is_win, multiplier = True, 2.0
    elif bet_type == "even" and win_num != 0 and win_num % 2 == 0:
        is_win, multiplier = True, 2.0
    elif bet_type == "odd" and win_num % 2 != 0:
        is_win, multiplier = True, 2.0
    elif bet_type == "number" and win_num == target_value:
        is_win, multiplier = True, 36.0

    alert = track_game_win(user.id, user.username, is_win)
    if alert:
        asyncio.create_task(notify_admin_fraud(context, alert))

    if is_win:
        reward = int(amount * multiplier)
        await add_balance(user.id, reward)
        result_text = (
            f"🎉 **ВЫИГРЫШ!**\n\n"
            f"Выпало число: **{win_num}** ({color})\n"
            f"💰 Выигрыш: **+{reward} монет** (x{multiplier})"
        )
    else:
        result_text = (
            f"❌ **ПРОИГРЫШ!**\n\n"
            f"Выпало число: **{win_num}** ({color})\n"
            f"💸 Потеряно: **{amount} 💰**"
        )

    await msg.edit_text(result_text, parse_mode="Markdown", reply_markup=menu(is_admin))


# ---------- SYNCHRONIZED PVP MATCH ----------
async def run_pvp_match(chat_id, context, challenger_id, opponent_id, amount):
    c_user = await get_user(challenger_id)
    o_user = await get_user(opponent_id)
    c_name = f"@{c_user[1]}" if c_user[1] else f"ID:{c_user[0]}"
    o_name = f"@{o_user[1]}" if o_user[1] else f"ID:{o_user[0]}"

    await context.bot.send_message(chat_id, f"🎲 Кидает {c_name}...")
    dice1_msg = await context.bot.send_dice(chat_id, emoji="🎲")

    for uid in (challenger_id, opponent_id):
        if uid != chat_id:
            try:
                await context.bot.send_message(uid, f"🎲 Кидает {c_name}...")
                await context.bot.forward_message(
                    chat_id=uid,
                    from_chat_id=chat_id,
                    message_id=dice1_msg.message_id,
                )
            except Exception:
                pass

    await asyncio.sleep(3.5)

    await context.bot.send_message(chat_id, f"🎲 Кидает {o_name}...")
    dice2_msg = await context.bot.send_dice(chat_id, emoji="🎲")

    for uid in (challenger_id, opponent_id):
        if uid != chat_id:
            try:
                await context.bot.send_message(uid, f"🎲 Кидает {o_name}...")
                await context.bot.forward_message(
                    chat_id=uid,
                    from_chat_id=chat_id,
                    message_id=dice2_msg.message_id,
                )
            except Exception:
                pass

    await asyncio.sleep(3.5)

    c_val = dice1_msg.dice.value
    o_val = dice2_msg.dice.value

    result_msg = (
        f"📊 **Итоги PvP дуэли:**\n\n"
        f"{c_name}: **{c_val}** 🎲\n"
        f"{o_name}: **{o_val}** 🎲\n\n"
    )

    win_amount = amount * 2

    if c_val > o_val:
        await add_balance(challenger_id, win_amount)
        result_msg += f"🏆 Победитель: {c_name}!\nВыигрыш: **+{win_amount} 💰**"

    elif o_val > c_val:
        await add_balance(opponent_id, win_amount)
        result_msg += f"🏆 Победитель: {o_name}!\nВыигрыш: **+{win_amount} 💰**"

    else:
        await add_balance(challenger_id, amount)
        await add_balance(opponent_id, amount)
        result_msg += "🤝 **Ничья!** Ставки возвращены игрокам."

    targets = {chat_id, challenger_id, opponent_id}
    for tid in targets:
        try:
            await context.bot.send_message(
                tid, result_msg, parse_mode="Markdown"
            )
        except Exception:
            pass


# ---------- GAME STARTER ROUTER ----------
async def start_bet_process(event_obj, context, user, game, amount):
    chat_id = (
        event_obj.chat.id
        if hasattr(event_obj, "chat")
        else event_obj.message.chat_id
    )
    if game == "mines":
        if not await spend(user.id, amount):
            await context.bot.send_message(
                chat_id,
                "❌ Недостаточно средств!",
                reply_markup=menu(user.username == ADMIN_USERNAME),
            )
            return
        await generate_mines(user.id, amount, chat_id)
        text = "💣 Поле заминировано! Открывайте ячейки:"
        reply_markup = mines_keyboard(user.id)
        if isinstance(event_obj, Update) or hasattr(event_obj, "reply_text"):
            await event_obj.reply_text(text, reply_markup=reply_markup)
        else:
            await event_obj.edit_message_text(text, reply_markup=reply_markup)
        return
    if game == "crash":
        if hasattr(event_obj, "delete_message"):
            await event_obj.delete_message()
        asyncio.create_task(run_crash_game(chat_id, context, user, amount))
        return
    if game == "roulette":
        pending_bets[user.id] = {"amount": amount}
        text, reply_markup = "🎰 На что делаем ставку в рулетке?", roulette_type_menu()
    elif game == "basket":
        pending_bets[user.id] = {"amount": amount}
        text, reply_markup = "🏀 Куда попадет мяч?", basket_choice_menu()
    elif game == "flip":
        pending_bets[user.id] = {"amount": amount}
        text, reply_markup = "🪙 Выберите сторону:", flip_choice_menu()
    else:
        if hasattr(event_obj, "delete_message"):
            await event_obj.delete_message()
        await play_game(chat_id, context, user, game, amount)
        return

    if hasattr(event_obj, "edit_message_text"):
        await event_obj.edit_message_text(text, reply_markup=reply_markup)
    else:
        await event_obj.reply_text(text, reply_markup=reply_markup)


# ---------- LOGIC FOR GAMES ----------
async def play_basket(chat_id, context, user, amount, choice):
    is_admin = user.username == ADMIN_USERNAME
    if not await spend(user.id, amount):
        await context.bot.send_message(
            chat_id,
            "❌ Недостаточно средств!",
            reply_markup=menu(user.username == ADMIN_USERNAME),
        )
        return
    dice_msg = await context.bot.send_dice(chat_id, emoji="🏀")
    await asyncio.sleep(3.5)
    value = dice_msg.dice.value
    is_in = value in [4, 5]
    win = (choice == "in" and is_in) or (choice == "miss" and not is_in)
    coef = 2.5 if choice == "in" else 1.8

    alert = track_game_win(user.id, user.username, win)
    if alert:
        asyncio.create_task(notify_admin_fraud(context, alert))

    if win:
        reward = int(amount * coef)
        await add_balance(user.id, reward)
        text = f"🎉 ВЫИГРЫШ!\nРезультат: {'Залетело! 🗑️' if is_in else 'Мимо! ❌'}\n💰 +{reward}"
    else:
        text = f"❌ ПРОИГРЫШ!\nРезультат: {'Залетело! 🗑️' if is_in else 'Мимо! ❌'}\n💸 -{amount}"
    await context.bot.send_message(chat_id, text, reply_markup=menu(is_admin))



# ---------- STICKERS FOR FLIP GAME ----------
# Вставьте сюда file_id ваших стикеров (узнать можно через @idstickerbot)
HEADS_STICKERS = [
    "CAACAgEAAxkBAAEHWdJqv02Nw2a6v0zd_nDJP8F_KpPPTwACoAYAAq296EWX6cVgvAABsSc9BA",
    # "file_id_стикера_орел_2", # если есть ещё
]

TAILS_STICKERS = [
    "CAACAgEAAxkBAAEHWdRqv02s8Nv6Dc09UtKRZoHVIQRWBgACogcAAgVT6EVnHd3TBppi9j0E",
    # "file_id_стикера_решка_2", # если есть ещё
]

# ✅ ВСТАВИТЬ ВМЕСТО УДАЛЕННЫХ СТРОК:
async def play_flip(chat_id, context, user, amount, choice):
    is_admin = user.username == ADMIN_USERNAME
    if not await spend(user.id, amount):
        await context.bot.send_message(
            chat_id,
            "❌ Недостаточно средств!",
            reply_markup=menu(user.username == ADMIN_USERNAME),
        )
        return
    
    msg = await context.bot.send_message(chat_id, "🪙 Монетка подбрасывается...")
    await asyncio.sleep(1.2)
    
    # Случайный выбор результата (heads / tails)
    result = random.choice(["heads", "tails"])
    
    # Выбор стикера и названия
    if result == "heads":
        res_text = "🦅 Орел"
        sticker_id = random.choice(HEADS_STICKERS)
    else:
        res_text = "🪙 Решка"
        sticker_id = random.choice(TAILS_STICKERS)

    # Удаляем сообщение "Монетка подбрасывается..." и отправляем стикер
    try:
        await msg.delete()
    except Exception:
        pass

    # Отправка выбранного стикера
    await context.bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
    is_win = (choice == result)
    alert = track_game_win(user.id, user.username, is_win)
    if alert:
        asyncio.create_task(notify_admin_fraud(context, alert))
        
    if is_win:
        reward = int(amount * 1.9)
        await add_balance(user.id, reward)
        text = f"🎉 **ВЫИГРЫШ!**\n\nВыпало: **{res_text}**\n💰 Выигрыш: **+{reward} монет**"
    else:
        text = f"❌ **ПРОИГРЫШ!**\n\nВыпало: **{res_text}**\n💸 Потеряно: **{amount} 💰**"
        
    await context.bot.send_message(
        chat_id=chat_id, 
        text=text, 
        parse_mode="Markdown", 
        reply_markup=menu(is_admin)
    )


async def play_game(chat_id, context, user, game, amount):
    is_admin = user.username == ADMIN_USERNAME
    if not await spend(user.id, amount):
        await context.bot.send_message(
            chat_id,
            "❌ Недостаточно средств!",
            reply_markup=menu(user.username == ADMIN_USERNAME),
        )
        return
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
    is_win = coef > 0

    alert = track_game_win(user.id, user.username, is_win)
    if alert:
        asyncio.create_task(notify_admin_fraud(context, alert))

    if is_win:
        reward = int(amount * coef)
        await add_balance(user.id, reward)
        text = f"🎉 ВЫИГРЫШ (x{coef})!\n💰 +{reward}"
    else:
        text = f"❌ ПРОИГРЫШ!\n💸 -{amount}"
    await context.bot.send_message(chat_id, text, reply_markup=menu(is_admin))

#__Lunch______

ALLOWED_UPDATES = ["message", "callback_query", "pre_checkout_query"]


async def _post_init(application):
    """Выполняется один раз сразу после старта бота — возвращаем ставки
    за игры, которые остались висеть с прошлого запуска."""
    await recover_orphaned_games(application.bot)


async def run_bot():
    global BOT_LOOP, BOT_APP

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    # 1. Flask для Render уже запущен выше (один раз) — обслуживает "/",
    #    "/health" и, в режиме вебхука, "/webhook/<secret>"

    # 2. Создание Telegram-бота
    bot_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)  # обрабатывать действия разных игроков ОДНОВРЕМЕННО
        .connection_pool_size(256)  # много параллельных запросов к Telegram API
        .pool_timeout(30.0)
        .rate_limiter(AIORateLimiter(max_retries=3))  # сам ждёт и повторяет при 429 Too Many Requests
        .post_init(_post_init)
        .build()
    )
    BOT_APP = bot_app
    BOT_LOOP = asyncio.get_running_loop()

    bot_app.add_error_handler(global_error_handler)

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_cmd))
    bot_app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    bot_app.add_handler(CommandHandler("pay", pay_cmd))
    bot_app.add_handler(CommandHandler("pvp", pvp_cmd))
    bot_app.add_handler(CommandHandler("create_promo", create_promo_cmd))
    bot_app.add_handler(CommandHandler("promo", promo_cmd))
    bot_app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    bot_app.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT, successful_payment_callback
        )
    )
    bot_app.add_handler(CallbackQueryHandler(cb))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    bot_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    await bot_app.initialize()
    await bot_app.start()

    if RENDER_EXTERNAL_URL:
        # 3а. Render знает свой публичный адрес — используем вебхук.
        # Telegram сам будет стучаться к нам на /webhook/<secret>, а Flask-роут
        # выше кладёт апдейты в очередь бота. Это быстрее и надёжнее polling'а
        # под нагрузкой, и не держит лишнее соединение открытым.
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook/{WEBHOOK_SECRET}"
        await bot_app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=ALLOWED_UPDATES,
            max_connections=100,
        )
        logger.info("Бот запущен через webhook: %s", webhook_url)
        # Держим процесс живым — апдейты приходят через Flask-роут, а не отсюда.
        try:
            await asyncio.Event().wait()
        finally:
            await bot_app.stop()
            await bot_app.shutdown()
    else:
        # 3б. Нет публичного адреса (например, локальный запуск) — обычный polling.
        await bot_app.bot.delete_webhook(drop_pending_updates=False)
        logger.warning("RENDER_EXTERNAL_URL не задан — запускаюсь через polling")
        await bot_app.updater.start_polling(allowed_updates=ALLOWED_UPDATES)
        try:
            await asyncio.Event().wait()
        finally:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(run_bot())
