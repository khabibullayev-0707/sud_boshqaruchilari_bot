import asyncio
import os
import re
import sqlite3
from contextlib import contextmanager
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage  # MemoryStorage ishlatamiz
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter
from pypdf import PdfReader
import logging

# --- LOGLASH SOZLAMASI ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- SOZLAMALAR ---
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Token AlwaysData -> Environment ichida saqlanadi
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi! Environment ga BOT_TOKEN=... yozing")

# --- WEBHOOK SOZLAMALARI ---
BASE_URL = os.getenv("BASE_URL", "https://muhammadbilolkhon.alwaysdata.net")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "savolchi_secret_2026")
CHANNEL_ID = "@sud_boshqaruvchilar"  # SENING KANAL NOMINI QO'Y
ADMIN_IDS = [8061103270, 7198606055, 8653619219, 8863967708]  # SENING ADMIN ID'LARI

# --- BOT VA DISPATCHER ---
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()  # Sessiya memory'da saqlanadi (restart'da yo'qoladi)
dp = Dispatcher(storage=storage)

# --- DATABASE CONNECTION ---
DB_PATH = "quiz_bot.db"
DB_TIMEOUT = 10.0


@contextmanager
def get_db():
    """Database connection bilan context manager"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database xatoligi: {e}")
        raise


def init_db():
    """Jadvallarni boshlang'ich sozlash"""
    with get_db() as db:
        cursor = db.cursor()

        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS users
                       (
                           user_id
                           INTEGER
                           PRIMARY
                           KEY,
                           full_name
                           TEXT,
                           username
                           TEXT,
                           score
                           INTEGER
                           DEFAULT
                           0,
                           created_at
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP
                       )
                       """)

        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS questions
                       (
                           id
                           INTEGER
                           PRIMARY
                           KEY
                           AUTOINCREMENT,
                           question
                           TEXT
                           NOT
                           NULL,
                           option_a
                           TEXT
                           NOT
                           NULL,
                           option_b
                           TEXT
                           NOT
                           NULL,
                           option_c
                           TEXT
                           NOT
                           NULL,
                           option_d
                           TEXT
                           NOT
                           NULL,
                           correct
                           INTEGER
                           CHECK (
                           correct
                           IN
                       (
                           0,
                           1,
                           2,
                           3
                       )),
                           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                           )
                       """)

        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS channel_posts
                       (
                           question_id
                           INTEGER
                           PRIMARY
                           KEY,
                           message_id
                           INTEGER,
                           poll_id
                           TEXT,
                           sent_at
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP
                       )
                       """)

        db.commit()
        logger.info("Database boshlandi!")


# --- FSM (HOLATLAR) ---
class AddQuestion(StatesGroup):
    question = State()
    option_a = State()
    option_b = State()
    option_c = State()
    option_d = State()
    correct = State()


class UploadPDF(StatesGroup):
    file = State()


# --- HELPER FUNCTIONS ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Users ro'yxati", callback_data="admin_users_list_1"),
                InlineKeyboardButton(text="🏆 Top-3 Reyting", callback_data="admin_rating")
            ],
            [
                InlineKeyboardButton(text="📜 Barcha savollar", callback_data="admin_all_questions_1"),
                InlineKeyboardButton(text="➕ Qo'lda savol qo'shish", callback_data="admin_add_manual")
            ],
            [
                InlineKeyboardButton(text="📄 PDF savol yuklash", callback_data="admin_add_pdf"),
                InlineKeyboardButton(text="📢 10 ta savol yuborish", callback_data="admin_send_10")
            ],
            [
                InlineKeyboardButton(text="🗑 Bazani tozalash", callback_data="admin_clear_db")
            ]
        ]
    )


# --- KANALGA SAVOL YUBORISH ---
async def send_question_to_channel(payload: dict, number: int):
    try:
        question_text = f"{number}. {payload['question']}"[:300]
        prefixes = ["A) ", "B) ", "C) ", "D) "]
        options = [f"{prefixes[i]}{payload['options'][i]}"[:100] for i in range(4)]
        correct_id = payload["correct"]

        poll_msg = await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=question_text,
            options=options,
            type="quiz",
            correct_option_id=correct_id,
            explanation="To'g'ri javob!",
            is_anonymous=True
        )
        return poll_msg.message_id, poll_msg.poll.id
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await send_question_to_channel(payload, number)
    except Exception as e:
        logger.error(f"Poll yuborish xatoligi (Savol ID {payload.get('id')}): {e}")
        return None


async def send_daily_questions():
    """10 ta yangi savolni kanalga yuborish"""
    try:
        with get_db() as db:
            cursor = db.cursor()

            cursor.execute("""
                           SELECT q.*
                           FROM questions q
                                    LEFT JOIN channel_posts cp ON cp.question_id = q.id
                           WHERE cp.question_id IS NULL
                           ORDER BY q.id ASC LIMIT 10
                           """)
            rows = cursor.fetchall()

            if not rows:
                cursor.execute("DELETE FROM channel_posts")
                db.commit()
                cursor.execute("SELECT * FROM questions ORDER BY id ASC LIMIT 10")
                rows = cursor.fetchall()

            if not rows:
                logger.warning("Bazada savol yo'q!")
                return 0

            sent = 0
            for number, q in enumerate(rows, start=1):
                payload = {
                    "id": q["id"],
                    "question": q["question"],
                    "options": [q["option_a"], q["option_b"], q["option_c"], q["option_d"]],
                    "correct": q["correct"]
                }
                result = await send_question_to_channel(payload, number)
                if result:
                    message_id, poll_id = result
                    cursor.execute(
                        "INSERT OR REPLACE INTO channel_posts(question_id, message_id, poll_id) VALUES (?, ?, ?)",
                        (q["id"], message_id, poll_id)
                    )
                    db.commit()
                    sent += 1
                await asyncio.sleep(1.5)

            return sent
    except Exception as e:
        logger.error(f"Savollar yuborish xatoligi: {e}")
        return 0


# --- HANDLERLAR ---
@dp.message(Command("start"))
async def start_handler(message: Message):
    try:
        with get_db() as db:
            cursor = db.cursor()

            if not is_admin(message.from_user.id):
                cursor.execute(
                    """INSERT OR REPLACE INTO users(user_id, full_name, username, score) 
                       VALUES (?, ?, ?, COALESCE((SELECT score FROM users WHERE user_id = ?), 0))""",
                    (message.from_user.id, message.from_user.full_name, message.from_user.username,
                     message.from_user.id)
                )
                db.commit()

        if is_admin(message.from_user.id):
            await message.answer("👑 <b>Admin Panelga xush kelibsiz!</b>", reply_markup=admin_menu())
        else:
            await message.answer("Xush kelibsiz! Kanalimizdagi testlarda faol qatnashing.")
    except Exception as e:
        logger.error(f"Start handler xatoligi: {e}")
        await message.answer("Xatolik yuz berdi. Iltimos qayta urinib ko'ring.")


# --- USERS RO'YXATI ---
@dp.callback_query(F.data.startswith("admin_users_list_"))
async def admin_users_list_handler(callback: CallbackQuery):
    try:
        page = int(callback.data.split("_")[-1])
        limit = 10
        offset = (page - 1) * limit

        with get_db() as db:
            cursor = db.cursor()
            cursor.execute(
                "SELECT user_id, full_name, score FROM users ORDER BY score DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            users = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) as total FROM users")
            total = cursor.fetchone()["total"]

        if not users:
            await callback.message.answer("Foydalanuvchilar yo'q.", reply_markup=admin_menu())
            await callback.answer()
            return

        text = f"👥 <b>Users ({offset + 1}-{min(offset + limit, total)} / Jami {total}):</b>\n\n"
        for u in users:
            text += f"<b>{u['full_name']}</b> - 🏆 {u['score']} ball\n"

        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Ortga", callback_data=f"admin_users_list_{page - 1}"))
        if offset + limit < total:
            nav_buttons.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin_users_list_{page + 1}"))

        kb = [nav_buttons] if nav_buttons else []
        kb.append([InlineKeyboardButton(text="🔙 Bosh menyu", callback_data="admin_main_menu")])

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await callback.answer()
    except Exception as e:
        logger.error(f"Users list xatoligi: {e}")
        await callback.answer("Xatolik yuz berdi!")


# --- RATING ---
@dp.callback_query(F.data == "admin_rating")
async def admin_rating_handler(callback: CallbackQuery):
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT full_name, score FROM users ORDER BY score DESC LIMIT 3")
            top_users = cursor.fetchall()

        if not top_users:
            text = "🏆 Hali foydalanuvchilar yo'q."
        else:
            text = "🏆 <b>Top-3 Reyting:</b>\n\n"
            medals = ["🥇", "🥈", "🥉"]
            for i, u in enumerate(top_users):
                text += f"{medals[i]} <b>{u['full_name']}</b> - {u['score']} ball\n"

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Bosh menyu", callback_data="admin_main_menu")]]
        ))
        await callback.answer()
    except Exception as e:
        logger.error(f"Rating xatoligi: {e}")
        await callback.answer("Xatolik yuz berdi!")


# --- BARCHA SAVOLLAR ---
@dp.callback_query(F.data.startswith("admin_all_questions_"))
async def admin_all_questions_handler(callback: CallbackQuery):
    try:
        page = int(callback.data.split("_")[-1])
        limit = 5
        offset = (page - 1) * limit

        with get_db() as db:
            cursor = db.cursor()
            cursor.execute(
                "SELECT * FROM questions ORDER BY id ASC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            questions = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) as total FROM questions")
            total_q = cursor.fetchone()["total"]

        if not questions:
            await callback.message.answer("Bazada savollar mavjud emas.", reply_markup=admin_menu())
            await callback.answer()
            return

        text = f"📜 <b>Savollar ({offset + 1}-{min(offset + limit, total_q)} / Jami {total_q}):</b>\n\n"
        opts = ["A", "B", "C", "D"]
        for q in questions:
            text += f"<b>{q['id']}. {q['question']}</b>\n"
            text += f"A) {q['option_a']}\nB) {q['option_b']}\nC) {q['option_c']}\nD) {q['option_d']}\n"
            text += f"✅ To'g'ri javob: <b>{opts[q['correct']]}</b>\n\n"

        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Ortga", callback_data=f"admin_all_questions_{page - 1}"))
        if offset + limit < total_q:
            nav_buttons.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin_all_questions_{page + 1}"))

        kb = [nav_buttons] if nav_buttons else []
        kb.append([InlineKeyboardButton(text="🔙 Bosh menyu", callback_data="admin_main_menu")])

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await callback.answer()
    except Exception as e:
        logger.error(f"All questions xatoligi: {e}")
        await callback.answer("Xatolik yuz berdi!")


# --- MAIN MENU ---
@dp.callback_query(F.data == "admin_main_menu")
async def admin_main_menu_handler(callback: CallbackQuery):
    try:
        await callback.message.edit_text("👑 <b>Admin Panel:</b>", reply_markup=admin_menu())
        await callback.answer()
    except Exception as e:
        logger.error(f"Main menu xatoligi: {e}")


# --- SAVOLLAR YUBORISH ---
@dp.callback_query(F.data == "admin_send_10")
async def admin_send_10_handler(callback: CallbackQuery):
    try:
        await callback.answer("🚀 Savollar yuborilmoqda...")
        sent = await send_daily_questions()
        await callback.message.answer(f"📢 Kanalga <b>{sent}</b> ta savol yuborildi!", reply_markup=admin_menu())
    except Exception as e:
        logger.error(f"Send 10 xatoligi: {e}")
        await callback.answer("Xatolik yuz berdi!")


# --- BAZANI TOZALASH ---
@dp.callback_query(F.data == "admin_clear_db")
async def admin_clear_db_handler(callback: CallbackQuery):
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("DELETE FROM questions")
            cursor.execute("DELETE FROM channel_posts")
            db.commit()

        await callback.message.answer("🗑 <b>Barcha savollar va tarix tozalandi!</b>", reply_markup=admin_menu())
        await callback.answer()
    except Exception as e:
        logger.error(f"Clear DB xatoligi: {e}")
        await callback.answer("Xatolik yuz berdi!")


# --- QO'LDA SAVOL QO'SHISH ---
@dp.callback_query(F.data == "admin_add_manual")
async def add_manual_start(callback: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(AddQuestion.question)
        await callback.message.answer("📝 <b>Savol matnini kiriting:</b>")
        await callback.answer()
    except Exception as e:
        logger.error(f"Add manual start xatoligi: {e}")


@dp.message(AddQuestion.question)
async def process_q(message: Message, state: FSMContext):
    try:
        await state.update_data(question=message.text)
        await state.set_state(AddQuestion.option_a)
        await message.answer("A) variantini kiriting:")
    except Exception as e:
        logger.error(f"Question handler xatoligi: {e}")


@dp.message(AddQuestion.option_a)
async def process_opt_a(message: Message, state: FSMContext):
    try:
        await state.update_data(option_a=message.text)
        await state.set_state(AddQuestion.option_b)
        await message.answer("B) variantini kiriting:")
    except Exception as e:
        logger.error(f"Option A handler xatoligi: {e}")


@dp.message(AddQuestion.option_b)
async def process_opt_b(message: Message, state: FSMContext):
    try:
        await state.update_data(option_b=message.text)
        await state.set_state(AddQuestion.option_c)
        await message.answer("C) variantini kiriting:")
    except Exception as e:
        logger.error(f"Option B handler xatoligi: {e}")


@dp.message(AddQuestion.option_c)
async def process_opt_c(message: Message, state: FSMContext):
    try:
        await state.update_data(option_c=message.text)
        await state.set_state(AddQuestion.option_d)
        await message.answer("D) variantini kiriting:")
    except Exception as e:
        logger.error(f"Option C handler xatoligi: {e}")


@dp.message(AddQuestion.option_d)
async def process_opt_d(message: Message, state: FSMContext):
    try:
        await state.update_data(option_d=message.text)
        await state.set_state(AddQuestion.correct)
        await message.answer("To'g'ri javob indeksini kiriting (0=A, 1=B, 2=C, 3=D):")
    except Exception as e:
        logger.error(f"Option D handler xatoligi: {e}")


@dp.message(AddQuestion.correct)
async def process_correct(message: Message, state: FSMContext):
    try:
        if not message.text.isdigit() or int(message.text) not in [0, 1, 2, 3]:
            await message.answer("Faqat 0, 1, 2 yoki 3 sonini kiriting!")
            return

        data = await state.get_data()
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute(
                """INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (data['question'], data['option_a'], data['option_b'], data['option_c'],
                 data['option_d'], int(message.text))
            )
            db.commit()

        await state.clear()
        await message.answer("✅ <b>Savol qo'shildi!</b>", reply_markup=admin_menu())
    except Exception as e:
        logger.error(f"Correct answer handler xatoligi: {e}")
        await message.answer("Xatolik yuz berdi!")


# --- PDF YUKLASH ---
@dp.callback_query(F.data == "admin_add_pdf")
async def add_pdf_start(callback: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(UploadPDF.file)
        await callback.message.answer("📄 <b>PDF faylni yuboring:</b>")
        await callback.answer()
    except Exception as e:
        logger.error(f"PDF start xatoligi: {e}")


@dp.message(UploadPDF.file, F.document)
async def process_pdf(message: Message, state: FSMContext):
    if not message.document.file_name.endswith('.pdf'):
        await message.answer("Faqat PDF fayl yuboring!")
        return

    msg = await message.answer("⏳ PDF ketma-ketlik bo'yicha tahlil qilinmoqda...")
    file_path = f"temp_{message.document.file_id}.pdf"

    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, file_path)

        reader = PdfReader(file_path)
        full_text = ""
        for page in reader.pages:
            t = page.extract_text()
            if t:
                full_text += t + "\n"

        pattern = re.compile(
            r"(?:\n|^)\s*\d+[\.\)]\s*(.*?)(?=\n\s*[A-Aa-a][\)\.]\s*|$)"
            r"(?:\n\s*[A-Aa-a][\)\.]\s*(.*?)(?=\n\s*[B-Bb-b][\)\.]\s*|$))?"
            r"(?:\n\s*[B-Bb-b][\)\.]\s*(.*?)(?=\n\s*[C-Cc-c][\)\.]\s*|$))?"
            r"(?:\n\s*[C-Cc-c][\)\.]\s*(.*?)(?=\n\s*[D-Dd-d][\)\.]\s*|$))?"
            r"(?:\n\s*[D-Dd-d][\)\.]\s*(.*?)(?=\n|$))?"
            r"(?:.*?(?:javob|Javob|JAVOB)\s*:\s*([A-Da-d]))?",
            re.DOTALL | re.IGNORECASE
        )

        with get_db() as db:
            cursor = db.cursor()
            count = 0
            letter_map = {"A": 0, "a": 0, "B": 1, "b": 1, "C": 2, "c": 2, "D": 3, "d": 3}

            for match in pattern.finditer(full_text):
                groups = match.groups()
                q_raw = groups[0].strip() if groups[0] else ""
                a_raw = groups[1].strip() if groups[1] else ""
                b_raw = groups[2].strip() if groups[2] else ""
                c_raw = groups[3].strip() if groups[3] else ""
                d_raw = groups[4].strip() if groups[4] else ""
                ans_letter = groups[5] if groups[5] else None

                if len(q_raw) < 5 or re.match(r"^javob\s*:", q_raw, re.IGNORECASE):
                    continue

                opts = [a_raw, b_raw, c_raw, d_raw]

                correct_idx = 0
                if ans_letter and ans_letter in letter_map:
                    correct_idx = letter_map[ans_letter]
                else:
                    for idx, opt in enumerate(opts):
                        if "+" in opt or "*" in opt:
                            correct_idx = idx
                            break

                clean_opts = []
                for opt in opts:
                    opt = re.sub(r"(?:javob|Javob)\s*:\s*[A-Da-d].*", "", opt, flags=re.IGNORECASE)
                    opt = re.sub(r"\(\d+-\d+-modda\)", "", opt, flags=re.IGNORECASE)
                    opt = opt.replace("+", "").replace("*", "").strip()
                    if len(opt) < 3:
                        opt = "Berilmagan"
                    clean_opts.append(opt)

                cursor.execute(
                    """INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (q_raw, clean_opts[0], clean_opts[1], clean_opts[2], clean_opts[3], correct_idx)
                )
                count += 1
                db.commit()

        if os.path.exists(file_path):
            os.remove(file_path)

        await state.clear()
        await msg.edit_text(
            f"✅ <b>PDF muvaffaqiyatli saqlandi!</b>\n{count} ta savol va ularning to'g'ri javoblari bazaga tushdi.",
            reply_markup=admin_menu()
        )
    except Exception as e:
        logger.error(f"PDF parsing xatoligi: {e}")
        if os.path.exists(file_path):
            os.remove(file_path)
        await msg.edit_text(f"❌ PDF parsing xatoligi: {e}", reply_markup=admin_menu())


# --- ISHGA TUSHIRISH ---
async def on_startup(bot: Bot):
    await bot.set_webhook(
        BASE_URL + WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logger.info(f"Webhook o'rnatildi: {BASE_URL + WEBHOOK_PATH}")


def run_webhook():
    """AlwaysData uchun: HTTP server ochadi (IP va PORT ni AlwaysData beradi)"""
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    init_db()
    dp.startup.register(on_startup)

    app = web.Application()

    async def health(request):
        return web.Response(text="Savolchi bot ishlayapti ✅")

    app.router.add_get("/", health)

    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    host = os.getenv("IP", "::")
    port = int(os.getenv("PORT", 8100))
    logger.info(f"Webhook server ishga tushdi: {host}:{port}")
    web.run_app(app, host=host, port=port)


async def run_polling():
    """Kompyuterda sinash uchun (PORT yo'q bo'lsa)"""
    init_db()
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Bot polling rejimida ishga tushdi...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if os.getenv("PORT"):
        run_webhook()  # AlwaysData
    else:
        asyncio.run(run_polling())  # O'z kompyuteringizda