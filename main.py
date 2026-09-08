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

---------- DB ----------

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

---------- STATE ----------

user_custom = {}
basket_pending = {}
coin_pending = {}
mines_games = {}
pvp_requests = {}
next_pvp_id = 1

---------- USERS ----------

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
    user = (user[0], username, *user[2:])

return user

def set_balance(uid, balance):
cur.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, uid))
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
    SET balance = balance + 50, last_daily = ?, last_bonus = ?
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

---------- TOP ----------

def top10():
cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
rows = cur.fetchall()

text = "🏆 TOP 10\n\n"
for index, row in enumerate(rows, 1):
    username = row[0] or "без_ника"
    text += f"{index}. @{username} — {row[1]}\n"

return text

---------- MENUS ----------

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
InlineKeyboardButton("10", callback_data=f"bet_{game}10"),
InlineKeyboardButton("50", callback_data=f"bet{game}50"),
],
[
InlineKeyboardButton("100", callback_data=f"bet{game}100"),
InlineKeyboardButton("✏ Своя", callback_data=f"custom{game}"),
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

---------- MINES ----------

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
            row.append(
                InlineKeyboardButton(
                    game["grid"][index] * 2,
                    callback_data="noop",
                )
            )
        else:
            row.append(
                InlineKeyboardButton(
                    "❓❓",
                    callback_data=f"mine_{index}",
                )
            )
    keyboard.append(row)

keyboard.append([
    InlineKeyboardButton(
        f"💰 Забрать x{round(game['mult'], 2)}",
        callback_data="mine_cashout",
    )
])

return InlineKeyboardMarkup(keyboard)

---------- PVP ----------

def pvp_keyboard(request_id):
return InlineKeyboardMarkup([
[
InlineKeyboardButton(
"✅ Принять",
callback_data=f"pvp_accept_{request_id}",
),
InlineKeyboardButton(
"❌ Отклонить",
callback_data=f"pvp_decline_{request_id}",
),
]
])

async def pvp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user

if len(context.args) < 2:
    await update.message.reply_text("Использование: /pvp @user сумма")
    return

username = context.args[0].lstrip("@")

try:
    amount = int(context.args[1])
except (TypeError, ValueError):
    await update.message.reply_text("❌ сумма должна быть числом")
    return

if amount <= 0:
    await update.message.reply_text("❌ ставка должна быть больше нуля")
    return

challenger = get_user(user.id, user.username or "")
target = get_by_username(username)

if not target:
    await update.message.reply_text(
        "❌ пользователь не найден. Он должен сначала открыть бота через /start."
    )
    return

if target[0] == user.id:
    await update.message.reply_text("❌ нельзя вызвать самого себя")
    return

if challenger[2] < amount:
    await update.message.reply_text("❌ у тебя нет такой суммы")
    return

global next_pvp_id
request_id = next_pvp_id
next_pvp_id += 1

pvp_requests[request_id] = {
    "challenger_id": user.id,
    "challenger_username": user.username or "",
    "opponent_id": target[0],
    "opponent_username": target[1] or username,
    "amount": amount,
}

try:
    await context.bot.send_message(
        chat_id=target[0],
        text=(
            f"⚔️ Вызов на PvP от {user_label(user.id, user.username or '')}\n"
            f"💰 Ставка: {amount}\n\n"
            "Принять дуэль?"
        ),
        reply_markup=pvp_keyboard(request_id),
    )
except TelegramError:
    pvp_requests.pop(request_id, None)
    await update.message.reply_text(
        "❌ Не удалось отправить вызов. Пользователь должен открыть бота через /start."
    )
    return

await update.message.reply_text(
    f"⚔️ Вызов отправлен пользователю @{username}."
)

async def resolve_pvp(
query,
context: ContextTypes.DEFAULT_TYPE,
request_id,
):
request = pvp_requests.get(request_id)

if not request:
    await query.edit_message_text("❌ Этот вызов уже недействителен.")
    return

if query.from_user.id != request["opponent_id"]:
    await query.answer("Это не твой вызов.", show_alert=True)
    return

challenger = get_user(
    request["challenger_id"],
    request["challenger_username"],
)
opponent = get_user(
    request["opponent_id"],
    request["opponent_username"],
)
amount = request["amount"]

if challenger[2] < amount or opponent[2] < amount:
    pvp_requests.pop(request_id, None)
    await query.edit_message_text(
        "❌ У одного из игроков уже недостаточно монет."
    )
    return

pvp_requests.pop(request_id, None)
set_balance(challenger[0], challenger[2] - amount)
set_balance(opponent[0], opponent[2] - amount)

challenger_label = user_label(
    challenger[0],
    request["challenger_username"],
)
opponent_label = user_label(
    opponent[0],
    request["opponent_username"],
)

await query.edit_message_text(
    f"⚔️ Дуэль началась!\n🎲 Кидает {challenger_label}:"
)

try:
    await context.bot.send_message(
        chat_id=challenger[0],
        text=f"⚔️ Дуэль началась!\n🎲 Кидает {challenger_label}:",
    )

    first_dice = await query.message.reply_dice(emoji="🎲")
    await asyncio.sleep(2)

    await query.message.reply_text(f"🎲 Кидает {opponent_label}:")
    await context.bot.send_message(
        chat_id=challenger[0],
        text=f"🎲 Кидает {opponent_label}:",
    )

    second_dice = await query.message.reply_dice(emoji="🎲")
    await asyncio.sleep(2)
except TelegramError:
    set_balance(
        challenger[0],
        get_user(challenger[0])[2] + amount,
    )
    set_balance(
        opponent[0],
        get_user(opponent[0])[2] + amount,
    )
    error_text = "❌ Не удалось завершить бросок. Ставки возвращены."
    await query.message.reply_text(error_text, reply_markup=menu())
    await context.bot.send_message(
        chat_id=challenger[0],
        text=error_text,
        reply_markup=menu(),
    )
    return

first_value = first_dice.dice.value
second_value = second_dice.dice.value

if first_value > second_value:
    winner_id = challenger[0]
    loser_id = opponent[0]
    winner_label = challenger_label
    loser_label = opponent_label
    result_text = (
        f"🎲 {challenger_label}: {first_value}\n"
        f"🎲 {opponent_label}: {second_value}\n\n"
        f"🏆 Выиграл: {winner_label}\n"
        f"💔 Проиграл: {loser_label}\n"
        f"💰 Победа: +{amount * 2}"
    )
elif second_value > first_value:
    winner_id = opponent[0]
    loser_id = challenger[0]
    winner_label = opponent_label
    loser_label = challenger_label
    result_text = (
        f"🎲 {challenger_label}: {first_value}\n"
        f"🎲 {opponent_label}: {second_value}\n\n"
        f"🏆 Выиграл: {winner_label}\n"
        f"💔 Проиграл: {loser_label}\n"
        f"💰 Победа: +{amount * 2}"
    )
else:
    set_balance(
        challenger[0],
        get_user(challenger[0])[2] + amount,
    )
    set_balance(
        opponent[0],
        get_user(opponent[0])[2] + amount,
    )
    result_text = (
        f"🎲 {challenger_label}: {first_value}\n"
        f"🎲 {opponent_label}: {second_value}\n\n"
        "🤝 Ничья!\n"
        f"💰 Ставки возвращены: {amount}"
    )
    await query.message.reply_text(result_text, reply_markup=menu())
    await context.bot.send_message(
        chat_id=challenger[0],
        text=result_text,
        reply_markup=menu(),
    )
    return

set_balance(
    winner_id,
    get_user(winner_id)[2] + amount * 2,
)

await query.message.reply_text(result_text, reply_markup=menu())
await context.bot.send_message(
    chat_id=challenger[0],
    text=result_text,
    reply_markup=menu(),
)

---------- START ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user
get_user(user.id, user.username or "")
await update.message.reply_text(
"🎰 NEON CASINO",
reply_markup=menu(),
)

---------- CALLBACK ----------

async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
user = query.from_user
data = query.data

await query.answer()

current_user = get_user(user.id, user.username or "")

if data == "menu":
    await query.edit_message_text(
        "🎰 NEON CASINO",
        reply_markup=menu(),
    )

elif data == "games":
    await query.edit_message_text(
        "🎮 Игры",
        reply_markup=games(),
    )

elif data == "bal":
    await query.edit_message_text(
        f"💰 Баланс: {current_user[2]}",
        reply_markup=menu(),
    )

elif data == "top":
    await query.edit_message_text(
        top10(),
        reply_markup=menu(),
    )

elif data == "bonus":
    received, remaining = claim_bonus(user.id)

    if received:
        await query.edit_message_text(
            "🎁 Бонус получен!\n💰 +50 монет",
            reply_markup=menu(),
        )
    else:
        await query.edit_message_text(
            f"⏳ Бонус уже получен.\nСледующий будет через {format_remaining(remaining)}.",
            reply_markup=menu(),
        )

elif data == "admin":
    if user.username != ADMIN:
        await query.edit_message_text(
            "❌ нет доступа",
            reply_markup=menu(),
        )
        return

    await query.edit_message_text(
        "👑 ADMIN:\n/add @user 100",
        reply_markup=menu(),
    )

elif data == "pvp":
    await query.edit_message_text(
        "⚔️ Вызов на дуэль:\n/pvp @user сумма",
        reply_markup=menu(),
    )

elif data.startswith("pvp_accept_"):
    request_id = int(data.rsplit("_", 1)[1])
    await resolve_pvp(query, context, request_id)

elif data.startswith("pvp_decline_"):
    request_id = int(data.rsplit("_", 1)[1])
    request = pvp_requests.get(request_id)

    if not request:
        await query.edit_message_text("❌ Этот вызов уже недействителен.")
    elif query.from_user.id != request["opponent_id"]:
        await query.answer("Это не твой вызов.", show_alert=True)
    else:
        pvp_requests.pop(request_id, None)
        await query.edit_message_text("❌ Вызов отклонён.")

        try:
            await context.bot.send_message(
                chat_id=request["challenger_id"],
                text=(
                    f"❌ {user_label(request['opponent_id'], request['opponent_username'])} "
                    "отклонил PvP-вызов."
                ),
            )
        except TelegramError:
            pass

elif data in {"basket_hit", "basket_miss"}:
    amount = basket_pending.pop(user.id, None)

    if amount is None:
        await query.edit_message_text(
            "❌ Ставка устарела",
            reply_markup=menu(),
        )
        return

    prediction = "hit" if data == "basket_hit" else "miss"
    await play_game(
        query.message,
        user,
        "basket",
        amount,
        basket_prediction=prediction,
    )

elif data in {"flip_heads", "flip_tails"}:
    amount = coin_pending.pop(user.id, None)

    if amount is None:
        await query.edit_message_text(
            "❌ Ставка устарела",
            reply_markup=menu(),
        )
        return

    prediction = "heads" if data == "flip_heads" else "tails"
    await play_game(
        query.message,
        user,
        "flip",
        amount,
        coin_prediction=prediction,
    )

elif data in {
    "basket",
    "football",
    "darts",
    "flip",
    "slots",
    "bowling",
    "mines",
}:
    await query.edit_message_text(
        "💸 ставка",
        reply_markup=bets(data),
    )

elif data.startswith("custom_"):
    user_custom[user.id] = data.split("_", 1)[1]
    await query.message.reply_text("✏ введи сумму")

elif data.startswith("bet_"):
    _, game, amount = data.split("_")
    amount = int(amount)

    if game == "basket":
        if current_user[2] < amount:
            await query.edit_message_text(
                "❌ нет денег",
                reply_markup=menu(),
            )
            return

        basket_pending[user.id] = amount
        await query.edit_message_text(
            f"🏀 Ставка: {amount}\nВыбери прогноз:",
            reply_markup=basket_predictions(),
        )
    elif game == "flip":
        if current_user[2] < amount:
            await query.edit_message_text(
                "❌ нет денег",
                reply_markup=menu(),
            )
            return

        coin_pending[user.id] = amount
        await query.edit_message_text(
            f"🪙 Ставка: {amount}\nВыбери сторону монетки:",
            reply_markup=coin_predictions(),
        )
    else:
        await play_game(query.message, user, game, amount)

# ---------- MINES ----------
elif data.startswith("mine_") and data != "mine_cashout":
    index = int(data.split("_", 1)[1])
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
        await query.edit_message_reply_markup(
            reply_markup=mines_keyboard(user.id)
        )

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

---------- GAME LOGIC ----------

async def play_game(
message,
user,
game,
amount,
basket_prediction=None,
coin_prediction=None,
):
current_user = get_user(user.id, user.username or "")

if amount <= 0:
    await message.reply_text("❌ ставка должна быть больше нуля")
    return

if current_user[2] < amount:
    await message.reply_text("❌ нет денег")
    return

balance = current_user[2] - amount

# ---------- BASKET ----------
if game == "basket":
    if basket_prediction not in {"hit", "miss"}:
        await message.reply_text(
            "🏀 Выбери прогноз:",
            reply_markup=basket_predictions(),
        )
        basket_pending[user.id] = amount
        return

    dice_message = await message.reply_dice(emoji="🏀")
    await asyncio.sleep(2)

    value = dice_message.dice.value
    hit = value >= 4
    actual = "ЗАЛЕТЕЛО" if hit else "МИМО"
    prediction_correct = (
        basket_prediction == "hit" and hit
    ) or (
        basket_prediction == "miss" and not hit
    )

    if prediction_correct:
        win = amount * 2
        balance += win
        result_text = f"🏀 {actual} ✅ WIN +{win}"
    else:
        result_text = f"🏀 {actual} ❌ LOSE -{amount}"

    set_balance(user.id, balance)
    await message.reply_text(result_text, reply_markup=menu())
    return

elif game == "football":
    dice_message = await message.reply_dice(emoji="⚽")
    await asyncio.sleep(2)

    if dice_message.dice.value >= 4:
        balance += amount * 2
        result_text = "⚽ ГОЛ"
    else:
        result_text = "⚽ МИМО"

    set_balance(user.id, balance)
    await message.reply_text(result_text, reply_markup=menu())

elif game == "darts":
    dice_message = await message.reply_dice(emoji="🎯")
    await asyncio.sleep(2)

    if dice_message.dice.value == 6:
        balance += amount * 3
        result_text = "🎯 ЦЕНТР"
    else:
        result_text = "🎯 МИМО"

    set_balance(user.id, balance)
    await message.reply_text(result_text, reply_markup=menu())

elif game == "flip":
    if coin_prediction not in {"heads", "tails"}:
        await message.reply_text(
            "🪙 Выбери сторону монетки:",
            reply_markup=coin_predictions(),
        )
        coin_pending[user.id] = amount
        return

    await message.reply_text("🪙")
    await asyncio.sleep(1)

    actual = random.choice(["heads", "tails"])
    actual_text = "🦅 ОРЁЛ" if actual == "heads" else "🪙 РЕШКА"

    if actual == coin_prediction:
        balance += amount * 2
        result_text = f"{actual_text} ✅ WIN +{amount * 2}"
    else:
        result_text = f"{actual_text} ❌ LOSE -{amount}"

    set_balance(user.id, balance)
    await message.reply_text(result_text, reply_markup=menu())

elif game == "slots":
    dice_message = await message.reply_dice(emoji="🎰")
    await asyncio.sleep(2)

    # Telegram encodes the four possible triple combinations as
    # 1, 22, 43 and 64; 64 is the triple-seven result.
    if dice_message.dice.value == 64:
        balance += amount * 3
        result_text = f"🎰 ТРИ 7️⃣ ✅ WIN +{amount * 3}"
    elif dice_message.dice.value in {1, 22, 43}:
        balance += amount * 2
        result_text = f"🎰 ТРИ ОДИНАКОВЫХ ✅ WIN +{amount * 2}"
    else:
        result_text = "🎰 НЕ СОВПАЛО ❌ LOSE"

    set_balance(user.id, balance)
    await message.reply_text(result_text, reply_markup=menu())

elif game == "bowling":
    dice_message = await message.reply_dice(emoji="🎳")
    await asyncio.sleep(2)

    if dice_message.dice.value >= 5:
        balance += amount * 2
        result_text = "🎳 STRIKE"
    else:
        result_text = "🎳 MISS"

    set_balance(user.id, balance)
    await message.reply_text(result_text, reply_markup=menu())

elif game == "mines":
    generate_mines(user.id, amount)
    set_balance(user.id, balance)
    await message.reply_text(
        "💣 Mines started",
        reply_markup=mines_keyboard(user.id),
    )

---------- CUSTOM BET ----------

async def input_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user

if user.id not in user_custom:
    return

try:
    amount = int(update.message.text.strip())
except (TypeError, ValueError):
    await update.message.reply_text("❌ введи число")
    return

game = user_custom.pop(user.id)

if game == "basket":
    current_user = get_user(user.id, user.username or "")

    if amount <= 0:
        await update.message.reply_text("❌ ставка должна быть больше нуля")
        return

    if current_user[2] < amount:
        await update.message.reply_text("❌ нет денег")
        return

    basket_pending[user.id] = amount
    await update.message.reply_text(
        f"🏀 Ставка: {amount}\nВыбери прогноз:",
        reply_markup=basket_predictions(),
    )
    return

if game == "flip":
    current_user = get_user(user.id, user.username or "")

    if amount <= 0:
        await update.message.reply_text("❌ ставка должна быть больше нуля")
        return

    if current_user[2] < amount:
        await update.message.reply_text("❌ нет денег")
        return

    coin_pending[user.id] = amount
    await update.message.reply_text(
        f"🪙 Ставка: {amount}\nВыбери сторону монетки:",
        reply_markup=coin_predictions(),
    )
    return

await play_game(update.message, user, game, amount)

---------- ADMIN ----------

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
if update.effective_user.username != ADMIN:
return

if len(context.args) < 2:
    await update.message.reply_text("Использование: /add @user сумма")
    return

username = context.args[0].lstrip("@")

try:
    amount = int(context.args[1])
except (TypeError, ValueError):
    await update.message.reply_text("❌ сумма должна быть числом")
    return

if amount <= 0:
    await update.message.reply_text("❌ сумма должна быть больше нуля")
    return

target = get_by_username(username)
if not target:
    await update.message.reply_text("❌ пользователь не найден")
    return

set_balance(target[0], target[2] + amount)
await update.message.reply_text(
    f"✅ начислено {amount} пользователю @{username}"
)

---------- RUN ----------

if not TOKEN:
raise RuntimeError("BOT_TOKEN is not configured")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("add", add))
app.add_handler(CommandHandler("pvp", pvp_command))
app.add_handler(CallbackQueryHandler(cb))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, input_bet))

app.run_polling()
