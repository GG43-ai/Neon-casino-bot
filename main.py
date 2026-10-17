import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
import urllib.parse
import psycopg2
import threading

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
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
from flask import Flask, request, send_from_directory

# --- 1. Создаем веб-сервер для Render ---
app = Flask(__name__)


@app.before_request
def _handle_preflight():
    # Браузер перед настоящим запросом с кастомным заголовком шлёт OPTIONS —
    # отвечаем сразу пустым 200, без этого CORS не пройдёт.
    if request.method == "OPTIONS":
        return "", 200


@app.after_request
def _add_cors(resp):
    # Mini App открывается со страницы на claude.ai/artifact, а API живёт на
    # Render — разные origin'ы, поэтому браузеру нужен явный CORS-заголовок.
    # Реальная защита — не CORS, а проверка подписи initData ниже.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def verify_telegram_init_data(init_data: str, max_age=86400):
    """Проверяет, что initData реально подписана Telegram нашим токеном бота,
    а не подделана пользователем в devtools. Возвращает dict с данными
    пользователя (id, username, ...) или None, если подпись неверна/просрочена.
    Алгоритм — официальный, из документации Telegram Mini Apps."""
    try:
        pairs = urllib.parse.parse_qsl(init_data, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    recv_hash = data.pop("hash", None)
    if not recv_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, recv_hash):
        return None

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if time.time() - auth_date > max_age:
        return None

    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except (TypeError, ValueError):
        return None


def call_async(coro):
    """Мост из синхронного потока Flask в асинхронный event loop бота —
    чтобы переиспользовать те же атомарные spend()/add_balance() и не
    городить вторую версию логики баланса специально для API."""
    if BOT_LOOP is None:
        raise RuntimeError("Бот ещё не запущен")
    fut = asyncio.run_coroutine_threadsafe(coro, BOT_LOOP)
    return fut.result(timeout=10)


def fire_and_forget(coro):
    """Как call_async, но не ждёт результата — для длинных фоновых задач
    (например, рассылка сотням пользователей), которые не должны держать
    HTTP-запрос открытым."""
    if BOT_LOOP is None:
        raise RuntimeError("Бот ещё не запущен")
    asyncio.run_coroutine_threadsafe(coro, BOT_LOOP)


WEB_LOGIN_TOKENS = {}
WEB_LOGIN_LOCK = threading.Lock()
WEB_LOGIN_TTL = 600          # скільки секунд дійсне посилання для входу
WEB_SESSION_TTL = 60 * 60 * 24 * 30   # 30 днів — сесія сайту після підтвердженого входу


def new_web_login_token():
    """Створює одноразовий токен для входу через бота (кнопка «Увійти» на
    сайті). Чистимо заразом прострочені токени, щоб словник не ріс вічно."""
    now = time.time()
    with WEB_LOGIN_LOCK:
        for k in [k for k, v in WEB_LOGIN_TOKENS.items() if now - v["created"] > WEB_LOGIN_TTL]:
            WEB_LOGIN_TOKENS.pop(k, None)
        token = hashlib.sha256(f"{now}-{random.random()}".encode()).hexdigest()[:24]
        WEB_LOGIN_TOKENS[token] = {"status": "pending", "created": now}
    return token


def confirm_web_login(token, uid, username):
    """Викликається з бота, коли людина перейшла за посиланням і натиснула
    Start. Повертає True, якщо токен ще дійсний."""
    with WEB_LOGIN_LOCK:
        rec = WEB_LOGIN_TOKENS.get(token)
        if not rec or time.time() - rec["created"] > WEB_LOGIN_TTL:
            return False
        rec["status"] = "confirmed"
        rec["session"] = sign_web_session(uid)
        rec["user_id"] = uid
        rec["username"] = username
    return True


def sign_web_session(uid):
    """Довгоживуча підписана сесія для сайту поза Telegram (без initData).
    Формат: <uid>.<expiry>.<hmac>. Підпис на секреті бот-токена — підробити
    без TOKEN неможливо, окремої таблиці сесій тримати не треба."""
    expiry = int(time.time()) + WEB_SESSION_TTL
    payload = f"{uid}.{expiry}"
    sig = hmac.new(TOKEN.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_web_session(token):
    try:
        uid_s, expiry_s, sig = token.split(".")
        payload = f"{uid_s}.{expiry_s}"
        expected = hmac.new(TOKEN.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(expiry_s) < time.time():
            return None
        return int(uid_s)
    except (ValueError, AttributeError):
        return None


def authed_user():
    """Достаёт и проверяет initData из запроса (Mini App внутри Telegram),
    либо подписанную веб-сессию (сайт вне Telegram после входа через бота).
    Возвращает (user_id, username) или None — тогда нужно вернуть 401."""
    init_data = request.headers.get("X-Telegram-Init-Data", "") or (
        request.get_json(silent=True) or {}
    ).get("initData", "")
    user = verify_telegram_init_data(init_data)
    if user:
        return user["id"], user.get("username", "")

    web_session = request.headers.get("X-Web-Session", "") or (
        request.get_json(silent=True) or {}
    ).get("webSession", "")
    if web_session:
        uid = verify_web_session(web_session)
        if uid:
            return uid, ""
    return None


def admin_auth():
    """Повертає (uid, username), лише якщо авторизований користувач — адмін
    (ADMIN_USERNAME); інакше None. На сайті поза Telegram authed_user() не
    знає юзернейм (там лише підписана сесія на uid), тому в такому разі
    підтягуємо його з бази перед звіркою."""
    auth = authed_user()
    if not auth:
        return None
    uid, username = auth
    if not username:
        row = call_async(get_user(uid))
        username = row[1] if row else ""
    if username != ADMIN_USERNAME:
        return None
    return uid, username


# Живое состояние игр, открытых через Mini App (отдельно от игр в самом
# чате бота). Источник истины по деньгам — всегда таблица active_games +
# атомарные spend()/add_balance(), это лишь оперативное состояние раунда.
web_mines_games = {}
web_crash_games = {}
WEB_GAME_LOCK = threading.Lock()

# Якщо раунд "зависає" (втрачене з'єднання, закритий застосунок, обірваний
# запит тощо), запис у словнику ніколи сам не зникає, і гравця назавжди
# блокує "активна гра вже є". Тому перед новою ставкою ми прибираємо
# застарілі записи: Crash триває щонайбільше ~11.4с (до x100), тож 30с —
# великий запас; Mines темп задає сам гравець, тож поріг довший.
MINES_STALE_SEC = 900
CRASH_STALE_SEC = 30


def _clear_stale_games(uid):
    """Викликати ВСЕРЕДИНІ WEB_GAME_LOCK, перед перевіркою на 'активна гра'."""
    mg = web_mines_games.get(uid)
    if mg and time.time() - mg.get("started_at", 0) > MINES_STALE_SEC:
        web_mines_games.pop(uid, None)
        call_async(clear_active_game(uid))
    cg = web_crash_games.get(uid)
    if cg and time.time() - cg.get("start", 0) > CRASH_STALE_SEC:
        web_crash_games.pop(uid, None)
        call_async(clear_active_game(uid))

MIN_BET = int(os.getenv("MINI_APP_MIN_BET", "10"))
# Бизнес-лимита на максимальную ставку больше нет — "Усе" должно реально
# ставить весь баланс, сколько бы там ни было. Верхняя граница ниже — не
# лимит на игру, а защита от абсурдных/переполненных значений из
# подделанного запроса; реальный потолок всё равно — баланс игрока,
# это гарантирует атомарный spend() (he ушедший ниже нуля UPDATE).
MAX_BET = int(os.getenv("MINI_APP_MAX_BET", "100000000"))


# ---------- КРАМНИЦЯ: обводки аватарки та теми оформлення ----------
# Статичний каталог — фронтенд знає ті самі id й малює відповідний
# вигляд сам (кольори/емодзі), сервер лише перевіряє ціну/власність.
SHOP_ITEMS = {
    "border_cyan":   {"type": "border", "name": "Блакитна обводка",   "price": 300},
    "border_green":  {"type": "border", "name": "Смарагдова обводка", "price": 400},
    "border_pink":   {"type": "border", "name": "Рожева обводка",     "price": 400},
    "border_gold":   {"type": "border", "name": "Золота обводка",     "price": 600},
    "border_silver": {"type": "border", "name": "Срібна обводка",     "price": 450},
    "border_violet": {"type": "border", "name": "Фіолетова обводка",  "price": 500},
    "border_ice":    {"type": "border", "name": "❄️ Крижана обводка",  "price": 700},
    "border_fire":   {"type": "border", "name": "🔥 Вогняна обводка",  "price": 1200},
    "border_skull":  {"type": "border", "name": "💀 Череп",            "price": 1000},
    "border_ears":   {"type": "border", "name": "🐰 Вушка",            "price": 1500},
    "border_cat":    {"type": "border", "name": "🐱 Котячі вушка",     "price": 1500},
    "border_horns":  {"type": "border", "name": "😈 Рога",             "price": 1600},
    "border_halo":   {"type": "border", "name": "👼 Німб",             "price": 1600},
    "border_rainbow":{"type": "border", "name": "🌈 Веселка",          "price": 1800},
    "border_crown":  {"type": "border", "name": "👑 Королівська",      "price": 2500},
    "border_galaxy": {"type": "border", "name": "🌌 Галактика",        "price": 3000},
    "border_diamond":{"type": "border", "name": "💎 Діамантова",       "price": 3500},
    # Теми оформлення — безкоштовна косметика, обирається прямо в профілі.
    "theme_gold":    {"type": "theme", "name": "Золота тема",     "price": 0},
    "theme_ice":     {"type": "theme", "name": "Крижана тема",    "price": 0},
    "theme_sunset":  {"type": "theme", "name": "Тема заходу",     "price": 0},
    "theme_ocean":   {"type": "theme", "name": "Океанічна тема",  "price": 0},
    "theme_cyber":   {"type": "theme", "name": "Кіберпанк",       "price": 0},
    "theme_emerald": {"type": "theme", "name": "Смарагдова тема", "price": 0},
    "theme_crimson": {"type": "theme", "name": "Багряна тема",    "price": 0},
    "theme_violet":  {"type": "theme", "name": "Фіолетова тема",  "price": 0},
    "theme_matrix":  {"type": "theme", "name": "Матриця",         "price": 0},
    "theme_royal":   {"type": "theme", "name": "Королівська тема","price": 0},
}

# ---------- АПГРЕЙД: колесо з жовтим/сірим сектором ----------
UPGRADE_MIN_MULT = 1.2
UPGRADE_MAX_MULT = 20.0
UPGRADE_HOUSE_EDGE = 0.05  # той самий дух хаус-еджу, що й в інших іграх


def _parse_amount(body):
    """Игрок сам выбирает ставку — проверяем, что это вменяемое целое
    число, а не отрицательное/дробное/огромное значение из подделанного
    запроса. Верхней бизнес-границы нет: если ставка больше реального
    баланса, её всё равно отклонит атомарный spend()."""
    amount = body.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool):
        return None
    if amount < MIN_BET or amount > MAX_BET:
        return None
    return amount


def _crash_mult_at(elapsed_seconds):
    """Детерминированная кривая роста множителя от времени. И клиент (для
    анимации), и сервер (для проверки честности кешаута) считают по ней же,
    поэтому сервер может проверить результат, не раскрывая точку краша заранее."""
    return round(1 + elapsed_seconds * 0.22, 2)


def _crash_target_point():
    r = random.uniform(0, 100)
    if r < 65:
        return round(random.uniform(1.05, 1.30), 2)
    if r < 85:
        return round(random.uniform(1.31, 1.60), 2)
    if r < 95:
        return round(random.uniform(1.61, 2.20), 2)
    return round(random.uniform(2.21, 3.50), 2)


@app.route("/api/login/start", methods=["POST"])
def api_login_start():
    token = new_web_login_token()
    bot_username = None
    try:
        bot_username = BOT_APP.bot.username if BOT_APP else None
    except Exception:
        bot_username = None
    bot_username = bot_username or os.getenv("BOT_USERNAME", "NeonCasinoXBot")
    link = f"https://t.me/{bot_username}?start=weblogin_{token}"
    return {"token": token, "link": link}


@app.route("/api/login/poll", methods=["POST"])
def api_login_poll():
    token = (request.get_json(silent=True) or {}).get("token", "")
    with WEB_LOGIN_LOCK:
        rec = WEB_LOGIN_TOKENS.get(token)
        if not rec:
            return {"status": "expired"}
        if rec["status"] != "confirmed":
            return {"status": "pending"}
        session = rec["session"]
        uid = rec["user_id"]
        # одноразовий — більше не віддаємо сесію по цьому токену повторно
        WEB_LOGIN_TOKENS.pop(token, None)
    balance = call_async(get_user(uid))[2]
    return {"status": "confirmed", "session": session, "balance": balance}


@app.route("/api/me", methods=["POST"])
def api_me():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    db_user = call_async(get_user(uid, username))
    last_bonus = db_user[3] or 0
    next_bonus_at = last_bonus + 86400
    border, theme = call_async(get_equipped(uid))
    return {
        "balance": db_user[2],
        "username": db_user[1] or "",
        "min_bet": MIN_BET,
        "max_bet": MAX_BET,
        "referrals_count": db_user[5] or 0,
        "bonus_ready": int(time.time()) >= next_bonus_at,
        "next_bonus_at": next_bonus_at,
        "equipped_border": border,
        "equipped_theme": theme,
    }


@app.route("/api/bonus/claim", methods=["POST"])
def api_bonus_claim():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    call_async(get_user(uid, username))  # убеждаемся, что юзер есть в базе
    ok = call_async(claim_bonus(uid, 50, 86400))
    db_user = call_async(get_user(uid, username))
    if not ok:
        next_bonus_at = (db_user[3] or 0) + 86400
        return {"error": "too_early", "next_bonus_at": next_bonus_at}, 400
    return {"balance": db_user[2], "amount": 50, "next_bonus_at": (db_user[3] or 0) + 86400}


@app.route("/api/leaderboard", methods=["POST"])
def api_leaderboard():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, _ = auth
    rows = call_async(db(
        "SELECT user_id, username, balance, equipped_border FROM users ORDER BY balance DESC LIMIT 10",
        None, "all",
    ))
    items = [
        {
            "user_id": r[0], "name": r[1] or f"Гравець {r[0]}", "balance": r[2],
            "you": r[0] == uid, "border": r[3] or "",
        }
        for r in rows
    ]
    return {"items": items}


@app.route("/api/promo/redeem", methods=["POST"])
def api_promo_redeem():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    code = (request.get_json(silent=True) or {}).get("code", "")
    code = str(code).strip().upper()
    if not code:
        return {"error": "bad_code"}, 400
    call_async(get_user(uid, username))
    status, reward = call_async(redeem_promo(uid, code))
    if status != "ok":
        return {"error": status}, 400
    balance = call_async(get_user(uid))[2]
    return {"reward": reward, "balance": balance}


@app.route("/api/referral", methods=["POST"])
def api_referral():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, _ = auth
    db_user = call_async(get_user(uid))
    bot_username = None
    try:
        bot_username = BOT_APP.bot.username if BOT_APP else None
    except Exception:
        bot_username = None
    link = f"https://t.me/{bot_username}?start=ref_{uid}" if bot_username else None
    return {"referrals_count": db_user[5] or 0, "link": link, "reward_per_invite": 100}


@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    """Переказ монет іншому гравцю з Mini App — та сама логіка, що й
    команда /pay в чаті бота (пошук за @username або id, анти-фрод лічильник,
    атомарні spend()/add_balance())."""
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    target_input = str(body.get("target", "")).strip()
    amount = body.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return {"error": "bad_amount"}, 400
    if not target_input:
        return {"error": "bad_target"}, 400

    call_async(get_user(uid, username))
    target_db = call_async(get_by_identifier(target_input))
    if not target_db:
        return {"error": "not_found"}, 404
    if target_db[0] == uid:
        return {"error": "self"}, 400

    fraud_alert = check_transfer_fraud(uid, username or str(uid))
    if fraud_alert and BOT_APP:
        call_async(notify_admin_fraud(BOT_APP, fraud_alert))

    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400
    call_async(add_balance(target_db[0], amount))

    try:
        sender_name = f"@{username}" if username else str(uid)
        call_async(BOT_APP.bot.send_message(
            target_db[0], f"💸 Гравець {sender_name} переказав вам {amount} 💰!"
        ))
    except Exception:
        pass

    balance = call_async(get_user(uid))[2]
    target_name = target_db[1] or str(target_db[0])
    return {"balance": balance, "target_name": target_name, "amount": amount}


# ---------- ПОПОВНЕННЯ БАЛАНСУ З MINI APP ----------
# Той самий конвертер і ті самі два способи оплати, що й у чаті бота:
# Telegram Stars (миттєво, автоматично — той самий successful_payment_callback)
# або картка зі скриншотом чека на підтвердження адміном.

def _bot_username():
    try:
        if BOT_APP and BOT_APP.bot.username:
            return BOT_APP.bot.username
    except Exception:
        pass
    return os.getenv("BOT_USERNAME", "NeonCasinoXBot")


@app.route("/api/donate/invoice", methods=["POST"])
def api_donate_invoice():
    """Поповнення Stars відбувається В ЧАТІ бота (а не через tg.openInvoice()
    прямо в Mini App) — так адмін і сам гравець бачать один спільний
    журнал оплат, і не треба тримати два різні шляхи нарахування. Тому цей
    ендпоінт лише повертає deep-link, який одразу виставляє рахунок Stars
    у чаті (/start deposit_stars_<amount> → обробляється в start())."""
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    coins = body.get("amount")
    if not isinstance(coins, int) or isinstance(coins, bool) or coins < 100:
        return {"error": "bad_amount"}, 400

    call_async(get_user(uid, username))
    uah_cost = round(coins / 100, 2)
    stars_cost = max(1, int(uah_cost * STARS_PER_UAH))
    deeplink = f"https://t.me/{_bot_username()}?start=deposit_stars_{coins}"
    return {"deeplink": deeplink, "stars": stars_cost, "uah": uah_cost}


@app.route("/api/donate/card", methods=["POST"])
def api_donate_card():
    """Для оплати карткою — deep-link у бота, який одразу переводить у
    режим "чекаю скриншот чека" (/start deposit_<amount>, обробляється в
    start()); реквізити картки гравець бачить вже в чаті."""
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    coins = body.get("amount")
    if not isinstance(coins, int) or isinstance(coins, bool) or coins < 100:
        return {"error": "bad_amount"}, 400

    uah_cost = round(coins / 100, 2)
    deeplink = f"https://t.me/{_bot_username()}?start=deposit_{coins}"
    return {"card": CARD_NUMBER, "uah": uah_cost, "deeplink": deeplink}


# ---------- КРАМНИЦЯ: каталог, покупка, екіпіровка ----------
@app.route("/api/shop/catalog", methods=["POST"])
def api_shop_catalog():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    call_async(get_user(uid, username))
    owned = call_async(owned_items(uid))
    border, theme = call_async(get_equipped(uid))
    items = [
        dict(id=iid, type=meta["type"], name=meta["name"], price=meta["price"],
             owned=(meta["type"] == "theme") or iid in owned)
        for iid, meta in SHOP_ITEMS.items()
    ]
    balance = call_async(get_user(uid))[2]
    return {"items": items, "equipped_border": border, "equipped_theme": theme, "balance": balance}


@app.route("/api/shop/buy", methods=["POST"])
def api_shop_buy():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    item_id = str((request.get_json(silent=True) or {}).get("item_id", ""))
    meta = SHOP_ITEMS.get(item_id)
    if not meta:
        return {"error": "not_found"}, 404
    if meta["type"] == "theme":
        return {"error": "free_item"}, 400

    call_async(get_user(uid, username))
    if item_id in call_async(owned_items(uid)):
        return {"error": "already_owned"}, 400
    if not call_async(spend(uid, meta["price"])):
        return {"error": "insufficient_funds"}, 400
    call_async(buy_item(uid, item_id))

    balance = call_async(get_user(uid))[2]
    return {"balance": balance, "item_id": item_id}


@app.route("/api/shop/equip", methods=["POST"])
def api_shop_equip():
    """item_id порожній рядок знімає екіпіровку (повертає стандартний
    вигляд) — власність при цьому перевіряти не треба."""
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, _ = auth
    body = request.get_json(silent=True) or {}
    item_id = str(body.get("item_id", ""))
    slot = body.get("slot")
    if slot not in ("border", "theme"):
        return {"error": "bad_slot"}, 400

    if item_id:
        meta = SHOP_ITEMS.get(item_id)
        if not meta or meta["type"] != slot:
            return {"error": "not_found"}, 404
        # Теми оформлення безкоштовні й обираються прямо в профілі — лише
        # обводки аватарки лишаються платною косметикою з крамниці.
        if slot != "theme" and item_id not in call_async(owned_items(uid)):
            return {"error": "not_owned"}, 400

    call_async(set_equipped(uid, slot, item_id))
    return {"ok": True, "slot": slot, "item_id": item_id}


# ---------- АПГРЕЙД: колесо з жовтим (виграш) і сірим (програш) сектором ----------
@app.route("/api/upgrade/play", methods=["POST"])
def api_upgrade_play():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    amount = _parse_amount(body)
    if amount is None:
        return {"error": "bad_amount"}, 400
    multiplier = body.get("multiplier")
    if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool):
        return {"error": "bad_multiplier"}, 400
    multiplier = round(float(multiplier), 2)
    if multiplier < UPGRADE_MIN_MULT or multiplier > UPGRADE_MAX_MULT:
        return {"error": "bad_multiplier"}, 400

    call_async(get_user(uid, username))
    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400

    # Шанс на виграш — обернено пропорційний бажаному множнику, з
    # хаус-еджем, той самий принцип, що й на CS-скін сайтах: що вищий
    # множник обираєш, то вужчий жовтий сектор колеса.
    chance = max(0.02, min(0.95, (1 / multiplier) * (1 - UPGRADE_HOUSE_EDGE)))
    roll = random.random()
    win = roll < chance
    reward = int(amount * multiplier) if win else 0
    if reward:
        call_async(add_balance(uid, reward))

    balance = call_async(get_user(uid))[2]
    return {
        "win": win,
        "reward": reward,
        "chance": round(chance, 4),
        "roll": roll,
        "multiplier": multiplier,
        "balance": balance,
    }


# ---------- WEB ADMIN PANEL (тільки для ADMIN_USERNAME) ----------
# Той самий набір можливостей, що й у адмін-меню бота (баланс, розсилка,
# статистика, підозрілі гравці), але як API для вкладки "Адмін" у Mini App.
# Жодних нових прав не додає — лише дублює дію на тому самому Telegram-акаунті.

@app.route("/api/admin/whoami", methods=["POST"])
def api_admin_whoami():
    """Фронтенд викликає це, щоб тихо перевірити, чи показувати вкладку
    "Адмін" у профілі — без помилок у консолі для звичайних гравців."""
    auth = admin_auth()
    return {"is_admin": bool(auth)}


@app.route("/api/admin/stats", methods=["POST"])
def api_admin_stats():
    if not admin_auth():
        return {"error": "forbidden"}, 403
    count, total_bal = call_async(db("SELECT COUNT(*), SUM(balance) FROM users", None, "one"))
    top_rows = call_async(db(
        "SELECT username, balance, user_id FROM users ORDER BY balance DESC LIMIT 10",
        None, "all",
    ))
    top = [{"username": r[0] or "", "balance": r[1], "user_id": r[2]} for r in top_rows]
    suspicious = [
        {"user_id": uid, "username": info["username"], "reason": info["reason"], "time": info["time"]}
        for uid, info in suspicious_users_log.items()
    ]
    return {
        "users": count,
        "total_balance": total_bal or 0,
        "top": top,
        "suspicious": suspicious,
    }


@app.route("/api/admin/balance", methods=["POST"])
def api_admin_balance():
    """Видати/забрати баланс у гравця — те саме, що кнопки "Видати баланс" /
    "Забрать баланс" у чаті бота, лише з веб-панелі."""
    if not admin_auth():
        return {"error": "forbidden"}, 403
    body = request.get_json(silent=True) or {}
    target_input = str(body.get("target", "")).strip()
    amount = body.get("amount")
    mode = body.get("mode")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return {"error": "bad_amount"}, 400
    if mode not in ("add", "sub"):
        return {"error": "bad_mode"}, 400
    if not target_input:
        return {"error": "bad_target"}, 400

    target_db = call_async(get_by_identifier(target_input))
    if not target_db:
        return {"error": "not_found"}, 404

    if mode == "add":
        call_async(add_balance(target_db[0], amount))
    else:
        if not call_async(spend(target_db[0], amount)):
            return {"error": "insufficient_funds"}, 400

    new_row = call_async(get_user(target_db[0]))
    try:
        note = "➕ Адміністратор нарахував вам " if mode == "add" else "➖ Адміністратор списав у вас "
        call_async(BOT_APP.bot.send_message(
            target_db[0], f"{note}{amount} 💰. Баланс: {new_row[2]}"
        ))
    except Exception:
        pass

    return {"target_name": new_row[1] or str(new_row[0]), "balance": new_row[2]}


@app.route("/api/admin/broadcast", methods=["POST"])
def api_admin_broadcast():
    """Розсилка тексту всім гравцям бота — фонова задача, HTTP-відповідь не
    чекає на завершення (користувачів може бути багато)."""
    if not admin_auth():
        return {"error": "forbidden"}, 403
    text = str((request.get_json(silent=True) or {}).get("text", "")).strip()
    if not text:
        return {"error": "bad_request"}, 400

    count = call_async(db("SELECT COUNT(*) FROM users", None, "one"))[0]
    fire_and_forget(_web_broadcast(text))
    return {"queued": count}


@app.route("/api/admin/deposits", methods=["POST"])
def api_admin_deposits():
    """Заявки на поповнення карткою (скриншот чека вже надіслано в чат) —
    та сама черга, що й повідомлення в Telegram, лише видно й на сайті."""
    if not admin_auth():
        return {"error": "forbidden"}, 403
    return {"items": call_async(list_pending_deposits())}


@app.route("/api/admin/deposits/approve", methods=["POST"])
def api_admin_deposits_approve():
    if not admin_auth():
        return {"error": "forbidden"}, 403
    body = request.get_json(silent=True) or {}
    dep_id = body.get("id")
    if not isinstance(dep_id, int) or isinstance(dep_id, bool):
        return {"error": "bad_request"}, 400

    row = call_async(db(
        "SELECT user_id, coins, status FROM pending_deposits WHERE id=?", (dep_id,), "one",
    ))
    if not row:
        return {"error": "not_found"}, 404
    target_id, coins, status = row
    amount = body.get("amount")
    add_coins = amount if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0 else coins
    if status != "pending":
        return {"error": "already_resolved"}, 409

    call_async(get_user(target_id))
    call_async(add_balance(target_id, add_coins))
    call_async(resolve_pending_deposit(dep_id, "approved"))
    try:
        call_async(BOT_APP.bot.send_message(
            target_id, f"🎉 Ваш баланс успішно поповнено на +{add_coins} 💰!"
        ))
    except Exception:
        pass
    return {"ok": True}


@app.route("/api/admin/deposits/decline", methods=["POST"])
def api_admin_deposits_decline():
    if not admin_auth():
        return {"error": "forbidden"}, 403
    body = request.get_json(silent=True) or {}
    dep_id = body.get("id")
    if not isinstance(dep_id, int) or isinstance(dep_id, bool):
        return {"error": "bad_request"}, 400

    row = call_async(db("SELECT user_id, status FROM pending_deposits WHERE id=?", (dep_id,), "one"))
    if not row:
        return {"error": "not_found"}, 404
    target_id, status = row
    if status != "pending":
        return {"error": "already_resolved"}, 409

    call_async(resolve_pending_deposit(dep_id, "declined"))
    try:
        call_async(BOT_APP.bot.send_message(
            target_id, "❌ Вашу заявку на поповнення відхилено адміністратором."
        ))
    except Exception:
        pass
    return {"ok": True}


async def _web_broadcast(text):
    rows = await db("SELECT user_id FROM users", None, "all")
    success, blocked = 0, 0
    for (uid,) in rows:
        try:
            await BOT_APP.bot.send_message(uid, text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            blocked += 1
    try:
        admin_db = await get_by_identifier(ADMIN_USERNAME)
        if admin_db:
            await BOT_APP.bot.send_message(
                admin_db[0],
                f"✅ Розсилка із сайту завершена!\n📥 Доставлено: {success}\n🚫 Блок: {blocked}",
            )
    except Exception:
        pass


@app.route("/api/mines/bet", methods=["POST"])
def api_mines_bet():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    amount = _parse_amount(request.get_json(silent=True) or {})
    if amount is None:
        return {"error": "bad_amount"}, 400

    with WEB_GAME_LOCK:
        _clear_stale_games(uid)
        if uid in web_mines_games or uid in web_crash_games:
            return {"error": "active_game"}, 409

        if not call_async(spend(uid, amount)):
            return {"error": "insufficient_funds"}, 400

        grid = ["bomb"] * 5 + ["gem"] * 20
        random.shuffle(grid)
        web_mines_games[uid] = {"grid": grid, "opened": [False] * 25, "mult": 1.0, "bet": amount, "started_at": time.time()}
        call_async(track_active_game(uid, "mines_webapp", uid, amount))

    balance = call_async(get_user(uid))[2]
    return {"bet": amount, "mult": 1.0, "balance": balance}


@app.route("/api/mines/open", methods=["POST"])
def api_mines_open():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, _ = auth
    index = (request.get_json(silent=True) or {}).get("index")
    if not isinstance(index, int) or not (0 <= index < 25):
        return {"error": "bad_index"}, 400

    with WEB_GAME_LOCK:
        game = web_mines_games.get(uid)
        if not game or game["opened"][index]:
            return {"error": "no_game"}, 409

        game["opened"][index] = True
        if game["grid"][index] == "bomb":
            grid = game["grid"]
            web_mines_games.pop(uid)
            call_async(clear_active_game(uid))
            return {"bomb": True, "grid": grid}

        game["mult"] = round(game["mult"] + 0.25, 2)
        return {"gem": True, "mult": game["mult"]}


@app.route("/api/mines/cashout", methods=["POST"])
def api_mines_cashout():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, _ = auth

    with WEB_GAME_LOCK:
        game = web_mines_games.pop(uid, None)
        if not game or not any(game["opened"]):
            return {"error": "no_game"}, 409
        call_async(clear_active_game(uid))

    reward = int(game["bet"] * game["mult"])
    call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]
    return {"reward": reward, "mult": game["mult"], "balance": balance}


@app.route("/api/crash/bet", methods=["POST"])
def api_crash_bet():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    amount = _parse_amount(request.get_json(silent=True) or {})
    if amount is None:
        return {"error": "bad_amount"}, 400

    with WEB_GAME_LOCK:
        _clear_stale_games(uid)
        if uid in web_mines_games or uid in web_crash_games:
            return {"error": "active_game"}, 409

        if not call_async(spend(uid, amount)):
            return {"error": "insufficient_funds"}, 400

        crash_point = _crash_target_point()
        web_crash_games[uid] = {"start": time.time(), "crash_point": crash_point, "bet": amount}
        call_async(track_active_game(uid, "crash_webapp", uid, amount))

    balance = call_async(get_user(uid))[2]
    return {"bet": amount, "balance": balance}


@app.route("/api/crash/status", methods=["POST"])
def api_crash_status():
    """Фронтенд опитує це під час польоту, щоб вибух показувався одразу,
    поки гра ще триває, а не лише в момент натискання "Забрати". Важливо:
    цей ендпоінт лише ПІДГЛЯДАЄ стан і нічого не видаляє з web_crash_games —
    раніше він сам забирав (pop) гру при виявленні краху, і якщо гравець
    саме в цю мить тиснув "Забрати", запит кешауту приходив вже на порожнє
    місце й бачив помилку "гру вже завершено" замість чесного "вибух"/
    виграшу. Єдине місце, що реально завершує раунд (pop) — /api/crash/cashout."""
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, _ = auth

    with WEB_GAME_LOCK:
        game = web_crash_games.get(uid)
        if not game:
            return {"error": "no_game"}, 409

        elapsed = time.time() - game["start"]
        current_mult = _crash_mult_at(elapsed)
        if current_mult >= game["crash_point"]:
            return {"crashed": True, "crash_point": game["crash_point"]}

    return {"crashed": False, "mult": current_mult}


@app.route("/api/crash/cashout", methods=["POST"])
def api_crash_cashout():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, _ = auth

    with WEB_GAME_LOCK:
        game = web_crash_games.pop(uid, None)
        if not game:
            return {"error": "no_game"}, 409

        elapsed = time.time() - game["start"]
        current_mult = _crash_mult_at(elapsed)
        if current_mult >= game["crash_point"]:
            call_async(clear_active_game(uid))
            return {"crashed": True, "crash_point": game["crash_point"]}

        call_async(clear_active_game(uid))

    reward = int(game["bet"] * current_mult)
    call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]
    return {"reward": reward, "mult": current_mult, "balance": balance}


# Настоящие красные числа европейской рулетки (чёрные — все остальные 1-36)
ROULETTE_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


@app.route("/api/roulette/play", methods=["POST"])
def api_roulette_play():
    """Европейская/французская рулетка — одно число 0, 37 ячеек.
    mode='color' — ставка на красное/чёрное (x2); 0 проигрывает в
    европейском варианте, а во французском действует правило La Partage —
    половина ставки возвращается (как в настоящих казино).
    mode='number' — ставка на точное число (x36, аналог 35:1 + возврат ставки)."""
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    amount = _parse_amount(body)
    mode = body.get("mode")
    choice = body.get("choice")
    variant = body.get("variant", "european")

    if amount is None or mode not in ("color", "number"):
        return {"error": "bad_request"}, 400
    if mode == "color" and choice not in ("red", "black"):
        return {"error": "bad_request"}, 400
    if mode == "number" and (not isinstance(choice, int) or isinstance(choice, bool) or not (0 <= choice <= 36)):
        return {"error": "bad_request"}, 400

    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400

    number = random.randint(0, 36)
    color = "green" if number == 0 else ("red" if number in ROULETTE_RED else "black")

    reward = 0
    partage = False
    if mode == "number":
        if choice == number:
            reward = amount * 36
    else:
        if number == 0:
            if variant == "french":
                reward = amount // 2
                partage = True
        elif choice == color:
            reward = amount * 2

    won = reward > amount if not partage else False
    if reward:
        call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]
    return {
        "number": number, "color": color, "won": won, "partage": partage,
        "reward": reward, "balance": balance,
    }


@app.route("/api/coinflip/play", methods=["POST"])
def api_coinflip_play():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    amount = _parse_amount(body)
    choice = body.get("choice")
    if amount is None or choice not in ("heads", "tails"):
        return {"error": "bad_request"}, 400

    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400

    outcome = random.choice(["heads", "tails"])
    won = outcome == choice
    reward = int(amount * 1.9) if won else 0
    if reward:
        call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]
    return {"outcome": outcome, "won": won, "reward": reward, "balance": balance}


@app.route("/api/basket/play", methods=["POST"])
def api_basket_play():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    amount = _parse_amount(body)
    choice = body.get("choice")
    if amount is None or choice not in ("in", "miss"):
        return {"error": "bad_request"}, 400
    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400

    value = random.randint(1, 5)
    is_in = value in (4, 5)
    won = (choice == "in" and is_in) or (choice == "miss" and not is_in)
    coef = 2.5 if choice == "in" else 1.8
    reward = int(amount * coef) if won else 0
    if reward:
        call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]
    return {"is_in": is_in, "won": won, "reward": reward, "balance": balance}


@app.route("/api/football/play", methods=["POST"])
def api_football_play():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    amount = _parse_amount(body)
    if amount is None:
        return {"error": "bad_request"}, 400
    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400

    value = random.randint(1, 5)
    won = value in (3, 4, 5)
    reward = amount * 2 if won else 0
    if reward:
        call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]
    return {"value": value, "won": won, "reward": reward, "balance": balance}


@app.route("/api/darts/play", methods=["POST"])
def api_darts_play():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    amount = _parse_amount(body)
    if amount is None:
        return {"error": "bad_request"}, 400
    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400

    value = random.randint(1, 6)
    coef = 3.0 if value == 6 else (1.5 if value in (4, 5) else 0)
    reward = int(amount * coef)
    if reward:
        call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]
    return {"value": value, "won": coef > 0, "reward": reward, "balance": balance}


@app.route("/api/bowling/play", methods=["POST"])
def api_bowling_play():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    amount = _parse_amount(body)
    if amount is None:
        return {"error": "bad_request"}, 400
    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400

    value = random.randint(1, 6)
    # RTP ~92% (було ~125% - гравець у середньому вигравав більше ставки)
    coef = 2.2 if value == 6 else (1.1 if value in (3, 4, 5) else 0)
    reward = int(amount * coef)
    if reward:
        call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]
    return {"value": value, "won": coef > 0, "reward": reward, "balance": balance}


@app.route("/api/slots/play", methods=["POST"])
def api_slots_play():
    auth = authed_user()
    if not auth:
        return {"error": "unauthorized"}, 401
    uid, username = auth
    body = request.get_json(silent=True) or {}
    amount = _parse_amount(body)
    if amount is None:
        return {"error": "bad_request"}, 400
    if not call_async(spend(uid, amount)):
        return {"error": "insufficient_funds"}, 400

    value = random.randint(1, 64)
    coef = 5.0 if value == 64 else (3.0 if value in (1, 22, 43) else 0)
    reward = int(amount * coef)
    if reward:
        call_async(add_balance(uid, reward))
    balance = call_async(get_user(uid))[2]

    pool = ["7", "🔔", "🍇", "🍋"]  # 7 🔔 🍇 🍋 (plain 7, not keycap, avoids the "70" render bug)
    if value == 64:
        reel = [pool[0], pool[0], pool[0]]
    elif value in (1, 22, 43):
        sym = {1: pool[1], 22: pool[2], 43: pool[3]}[value]
        reel = [sym, sym, sym]
    else:
        reel = [random.choice(pool) for _ in range(3)]
        if reel[0] == reel[1] == reel[2]:
            reel[2] = pool[(pool.index(reel[2]) + 1) % 4]

    return {"value": value, "reel": reel, "won": coef > 0, "reward": reward, "balance": balance}


@app.route('/')
def ping():
    return "Bot is alive!", 200


@app.route("/app")
def mini_app_page():
    """Отдаёт саму страницу Mini App прямо с этого же сервера — чтобы не
    зависеть от внешнего claude.ai-ссылки. Файл webapp.html должен лежать
    в той же папке, что и main.py (положите его туда и закоммитьте)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(here, "webapp.html")


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
    # threaded=True — иначе Flask обрабатывает запросы по одному: webhook от
    # Telegram и пинг от UptimeRobot становились в очередь друг за другом.
    app.run(host='0.0.0.0', port=port, threaded=True)

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
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "3"))
DB_POOL = pg_pool.ThreadedConnectionPool(
    DB_POOL_MIN,
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
        if conn.closed:
            # Соединение из пула уже мертво (Neon/прокси его разорвали, пока
            # оно простаивало) — не ждём, пока это обнаружится на самом
            # запросе, а сразу берём свежее.
            DB_POOL.putconn(conn, close=True)
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
_db_sync("""
CREATE TABLE IF NOT EXISTS user_items (
    user_id BIGINT,
    item_id TEXT,
    PRIMARY KEY (user_id, item_id)
)
""")
# Косметика профілю (крамниця) — додаємо колонки на вже існуючу таблицю
# users, якщо її ще не було (ALTER ... IF NOT EXISTS, щоб спокійно
# перезапускатись на вже створеній базі).
_db_sync("ALTER TABLE users ADD COLUMN IF NOT EXISTS equipped_border TEXT DEFAULT ''")
_db_sync("ALTER TABLE users ADD COLUMN IF NOT EXISTS equipped_theme TEXT DEFAULT ''")
_db_sync("""
CREATE TABLE IF NOT EXISTS pending_deposits (
    id SERIAL PRIMARY KEY,
    user_id BIGINT,
    username TEXT,
    coins INTEGER,
    status TEXT DEFAULT 'pending',
    created_at BIGINT
)
""")


async def add_pending_deposit(uid, username, coins):
    """Заявка на поповнення карткою з'являється тут одразу, як гравець
    скинув чек у чат — щоб адмін бачив її і на сайті, а не лише в Telegram."""
    row = await db(
        "INSERT INTO pending_deposits (user_id, username, coins, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?) RETURNING id",
        (uid, username or "", coins, int(time.time())), "one",
    )
    return row[0] if row else None


async def list_pending_deposits():
    rows = await db(
        "SELECT id, user_id, username, coins, created_at FROM pending_deposits "
        "WHERE status='pending' ORDER BY created_at DESC", None, "all",
    )
    return [
        {"id": r[0], "user_id": r[1], "username": r[2], "coins": r[3], "created_at": r[4]}
        for r in (rows or [])
    ]


async def resolve_pending_deposit(dep_id, status):
    if dep_id:
        await db("UPDATE pending_deposits SET status=? WHERE id=?", (status, dep_id))


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

# Ссылка на Mini App (опубликованную HTML-страницу). Пока не задано —
# нативная кнопка меню рядом с полем ввода не выставляется вообще.
MINI_APP_URL = os.getenv("MINI_APP_URL", "")
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


# ---------- КРАМНИЦЯ: власність і екіпіровка косметики ----------
async def owned_items(uid):
    rows = await db("SELECT item_id FROM user_items WHERE user_id=?", (uid,), "all")
    return {r[0] for r in rows}


async def buy_item(uid, item_id):
    await db(
        "INSERT INTO user_items (user_id, item_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (uid, item_id),
    )


async def get_equipped(uid):
    row = await db(
        "SELECT equipped_border, equipped_theme FROM users WHERE user_id=?", (uid,), "one"
    )
    if not row:
        return "", ""
    return row[0] or "", row[1] or ""


async def set_equipped(uid, slot, item_id):
    col = "equipped_border" if slot == "border" else "equipped_theme"
    await db(f"UPDATE users SET {col}=? WHERE user_id=?", (item_id, uid))


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
    # Кнопка входу в Mini App тепер лише внизу біля поля вводу (нативна
    # кнопка меню Telegram, виставляється через BotFather/set_chat_menu_button),
    # тому дублювати її інлайн-кнопкою в головному меню більше не треба.
    kb = [
        [
            InlineKeyboardButton("🎮 Игры", callback_data="games"),
            InlineKeyboardButton("🏆 Топ", callback_data="top"),
        ],
        [
            InlineKeyboardButton("💰 Баланс", callback_data="bal"),
            InlineKeyboardButton("🎁 Бонус +50", callback_data="bonus"),
        ],
        [InlineKeyboardButton("💳 Пополнить", callback_data="deposit")],
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


def admin_receipt_keyboard(dep_id, user_id, requested_coins):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"✅ Зачислить {requested_coins} 💰", callback_data=f"approve_exact_{dep_id}_{user_id}_{requested_coins}"
            )
        ],
        [
            InlineKeyboardButton(
                "✏ Другая сумма", callback_data=f"approve_custom_{dep_id}_{user_id}"
            ),
            InlineKeyboardButton(
                "❌ Отклонить", callback_data=f"decline_dep_{dep_id}_{user_id}"
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

    if context.args and context.args[0].startswith("weblogin_"):
        token = context.args[0][len("weblogin_"):]
        confirmed = confirm_web_login(token, user.id, user.username or "")
        if confirmed:
            await update.message.reply_text(
                "✅ Вхід підтверджено!\n\nПовертайся на сайт — там вже видно твій реальний баланс.",
            )
        else:
            await update.message.reply_text(
                "❌ Посилання для входу застаріло. Відкрий сайт ще раз і натисни «Увійти».",
            )
        await update.message.reply_text("🎰 NEON CASINO", reply_markup=menu(is_admin))
        return

    if context.args and context.args[0].startswith("deposit_stars_"):
        try:
            coins = int(context.args[0][len("deposit_stars_"):])
        except ValueError:
            coins = 0
        if coins >= 100:
            uah_cost = round(coins / 100, 2)
            stars_cost = max(1, int(uah_cost * STARS_PER_UAH))
            await context.bot.send_invoice(
                chat_id=user.id,
                title=f"Поповнення {coins} монет",
                description=f"Зарахування {coins} монет на ігровий баланс у NEON CASINO",
                payload=f"deposit_{coins}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=f"{coins} Монет", amount=stars_cost)],
                start_parameter="deposit_stars",
            )
            return

    if context.args and context.args[0].startswith("deposit_"):
        try:
            coins = int(context.args[0][len("deposit_"):])
        except ValueError:
            coins = 0
        if coins >= 100:
            context.user_data["deposit_requested_coins"] = coins
            awaiting_receipt.add(user.id)
            uah_cost = round(coins / 100, 2)
            await update.message.reply_text(
                f"💳 **Поповнення з сайту**\n\n"
                f"💰 Монет: **{coins}**\n"
                f"💵 До сплати: **{uah_cost} грн**\n"
                f"📌 Реквізити картки:\n`{CARD_NUMBER}`\n\n"
                f"Після переказу надішли сюди скріншот чека 📸",
                parse_mode="Markdown",
            )
            return

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
        dep_id = await add_pending_deposit(user.id, user.username or "", requested_coins)

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
                reply_markup=admin_receipt_keyboard(dep_id, user.id, requested_coins),
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
            await resolve_pending_deposit(action_data.get("dep_id"), "approved")
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
        dep_id = int(parts[2])
        target_id = int(parts[3])
        add_coins = int(parts[4])
        await get_user(target_id)
        await add_balance(target_id, add_coins)
        await resolve_pending_deposit(dep_id, "approved")
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
        parts = data.split("_")
        dep_id = int(parts[2])
        target_id = int(parts[3])
        awaiting_admin[user.id] = {
            "action": "approve_deposit",
            "target_id": target_id,
            "dep_id": dep_id,
        }
        await query.message.reply_text(
            f"✏ Введите **любую другую сумму монет** для зачисления игроку `{target_id}`:",
            parse_mode="Markdown",
        )
    elif data.startswith("decline_dep_") and is_admin:
        parts = data.split("_")
        dep_id = int(parts[2])
        target_id = int(parts[3])
        await resolve_pending_deposit(dep_id, "declined")
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
    asyncio.create_task(_keep_db_warm())

    # Нативна кнопка біля поля вводу (не інлайн-кнопка в меню) — виставляємо
    # її самі при кожному старті, щоб не покладатись на ручне налаштування
    # через BotFather і щоб вона завжди вела на актуальний MINI_APP_URL.
    if MINI_APP_URL:
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🎰 Казино", web_app=WebAppInfo(url=MINI_APP_URL)
                )
            )
        except Exception:
            logger.exception("Не удалось выставить кнопку меню Mini App")


async def _keep_db_warm():
    """Neon на бесплатном плане 'засыпает' после нескольких минут без
    запросов, а прокси по пути (Neon pgbouncer / сеть Render) может сам
    обрывать ПРОСТАИВАЮЩЕЕ соединение ещё раньше. Поэтому не просто шлём
    один запрос раз в несколько минут (он прогреет только ОДНО соединение
    из пула), а параллельно дёргаем все соединения пула сразу — так
    ни одно из них не успевает простаивать достаточно долго, чтобы его
    прибили."""
    while True:
        await asyncio.sleep(90)  # с запасом короче любых тайм-аутов простоя
        try:
            await asyncio.gather(
                *[db("SELECT 1") for _ in range(DB_POOL_MIN)],
                return_exceptions=True,
            )
        except Exception:
            logger.exception("Не удалось прогреть соединения с базой")


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
        .build()
    )
    BOT_APP = bot_app
    BOT_LOOP = asyncio.get_running_loop()

    bot_app.add_error_handler(global_error_handler)

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_cmd))
    bot_app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    bot_app.add_handler(CommandHandler("pay", pay_cmd))
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
    # Запускаем вручную: при ручном initialize()+start() (а не run_polling/
    # run_webhook) хук post_init сам по себе НЕ вызывается.
    await _post_init(bot_app)

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
