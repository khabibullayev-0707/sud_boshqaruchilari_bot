"""
Savolchi bot — sud boshqaruvchilari uchun test boti (aiogram 3)

Ishga tushirish:
  * AlwaysData (server): PORT o'zgaruvchisi bor -> webhook rejimi
  * O'z kompyuteringiz:  PORT yo'q            -> polling rejimi
    (Diqqat: kompyuterda ishga tushirsangiz, serverdagi webhook o'chadi.
     Keyin AlwaysData'da saytni Restart qilsangiz, qayta tiklanadi.)

Kerakli o'zgaruvchilar (Environment):
  BOT_TOKEN=...   (majburiy)
"""

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pypdf import PdfReader

# ============================================================
# SOZLAMALAR
# ============================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _load_dotenv():
    """Kompyuterda .env faylidan BOT_TOKEN ni o'qish (serverda Environment ishlatiladi)"""
    folder = os.path.dirname(os.path.abspath(__file__))
    # .env topilmasa, Windows ko'pincha saqlaydigan nomlarni ham tekshiramiz
    for name in (".env", ".env.txt", "env", "env.txt"):
        env_path = os.path.join(folder, name)
        if os.path.isfile(env_path):
            break
    else:
        print(f"[.env] Fayl topilmadi. Qidirilgan papka: {folder}")
        return
    with open(env_path, encoding="utf-8-sig") as f:   # utf-8-sig: Windows BOM belgisini olib tashlaydi
        for line in f:
            line = line.strip().lstrip("\ufeff")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if not os.environ.get(k):          # bo'sh bo'lsa ham to'ldiramiz
                    os.environ[k] = v
    if not os.environ.get("BOT_TOKEN") or "bu_yerga" in os.environ.get("BOT_TOKEN", ""):
        print(f"[.env] {env_path} topildi, lekin ichida haqiqiy BOT_TOKEN yo'q. Faylni ochib tokenni yozing.")


_load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN or "bu_yerga" in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi! Serverda Environment'ga, kompyuterda .env faylga BOT_TOKEN=... yozing")

CHANNEL_ID = "@sud_boshqaruvchilar"
ADMIN_IDS = [8061103270, 7198606055, 8653619219, 8863967708]

BASE_URL = os.getenv("BASE_URL", "https://muhammadbilolkhon.alwaysdata.net")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "savolchi_secret_2026")

DB_PATH = "quiz_bot.db"
DB_TIMEOUT = 10.0

LETTERS = ["A", "B", "V", "G"]      # Variant harflari (PDF'dagidek)
POLL_Q_LIMIT = 300                  # Telegram quiz savoli limiti
POLL_OPT_LIMIT = 100                # Telegram quiz varianti limiti
MSG_LIMIT = 4000                    # Telegram xabar limiti (4096 dan biroz kam)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# Admin filtrlari: admin tugmalari va holatlari faqat adminlar uchun ishlaydi
IsAdminMsg = F.from_user.id.in_(ADMIN_IDS)
IsAdminCb = F.from_user.id.in_(ADMIN_IDS)


# ============================================================
# DATABASE
# ============================================================
@contextmanager
def get_db():
    """Ulanishni ochadi va ish tugagach albatta yopadi"""
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database xatoligi: {e}")
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                full_name  TEXT,
                username   TEXT,
                score      INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                question   TEXT NOT NULL,
                option_a   TEXT NOT NULL,
                option_b   TEXT NOT NULL,
                option_c   TEXT NOT NULL,
                option_d   TEXT NOT NULL,
                correct    INTEGER CHECK (correct IN (0, 1, 2, 3)),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS channel_posts (
                question_id INTEGER PRIMARY KEY,
                message_id  INTEGER,
                poll_id     TEXT,
                sent_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
    logger.info("Database boshlandi!")


# ============================================================
# FSM HOLATLARI
# ============================================================
class AddQuestion(StatesGroup):
    question = State()
    option_a = State()
    option_b = State()
    option_c = State()
    option_d = State()
    correct = State()


class UploadPDF(StatesGroup):
    file = State()


# ============================================================
# YORDAMCHI FUNKSIYALAR
# ============================================================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Users ro'yxati", callback_data="admin_users_list_1"),
         InlineKeyboardButton(text="🏆 Top-3 Reyting", callback_data="admin_rating")],
        [InlineKeyboardButton(text="📜 Barcha savollar", callback_data="admin_all_questions_1"),
         InlineKeyboardButton(text="➕ Qo'lda savol qo'shish", callback_data="admin_add_manual")],
        [InlineKeyboardButton(text="📄 PDF savol yuklash", callback_data="admin_add_pdf"),
         InlineKeyboardButton(text="📢 10 ta savol yuborish", callback_data="admin_send_10")],
        [InlineKeyboardButton(text="🗑 Bazani tozalash", callback_data="admin_clear_db")],
    ])


def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bosh menyu", callback_data="admin_main_menu")]
    ])


async def safe_answer(callback: CallbackQuery, text: str = None):
    """Tugma bosilishiga javob (eskirgan bo'lsa ham xato bermaydi)"""
    try:
        await callback.answer(text)
    except Exception:
        pass


async def safe_edit(callback: CallbackQuery, text: str, markup=None):
    """Xabarni tahrirlaydi, bo'lmasa yangisini yuboradi"""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await callback.message.answer(text, reply_markup=markup)


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ============================================================
# KANALGA SAVOL YUBORISH
# ============================================================
async def send_question_to_channel(payload: dict, number: int):
    """
    Savolni quiz ko'rinishida yuboradi.
    Savol yoki variantlar Telegram limitidan uzun bo'lsa — avval to'liq matn
    alohida xabar qilib yuboriladi, quiz'da esa qisqartirilgan ko'rinish bo'ladi.
    """
    q_text = payload["question"]
    opts = payload["options"]
    correct = payload["correct"]

    too_long = len(f"{number}. {q_text}") > POLL_Q_LIMIT or any(
        len(f"{LETTERS[i]}) {o}") > POLL_OPT_LIMIT for i, o in enumerate(opts))

    try:
        if too_long:
            full = f"<b>{number}. {escape(q_text)}</b>\n\n"
            for i, o in enumerate(opts):
                full += f"<b>{LETTERS[i]})</b> {escape(o)}\n"
            await bot.send_message(CHANNEL_ID, full[:MSG_LIMIT])
            poll_question = shorten(f"{number}. {q_text}", POLL_Q_LIMIT - 20) + " (yuqorida)"
        else:
            poll_question = f"{number}. {q_text}"

        poll_options = [shorten(f"{LETTERS[i]}) {o}", POLL_OPT_LIMIT) for i, o in enumerate(opts)]

        poll_msg = await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=poll_question,
            options=poll_options,
            type="quiz",
            correct_option_id=correct,
            explanation=f"To'g'ri javob: {LETTERS[correct]}",
            is_anonymous=True,
        )
        return poll_msg.message_id, poll_msg.poll.id
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await send_question_to_channel(payload, number)
    except Exception as e:
        logger.error(f"Poll yuborish xatoligi (Savol ID {payload.get('id')}): {e}")
        return None


async def send_daily_questions():
    """Hali yuborilmagan 10 ta savolni kanalga yuboradi"""
    try:
        with get_db() as db:
            rows = db.execute("""
                SELECT q.* FROM questions q
                LEFT JOIN channel_posts cp ON cp.question_id = q.id
                WHERE cp.question_id IS NULL
                ORDER BY q.id ASC LIMIT 10
            """).fetchall()

            if not rows:  # Hammasi yuborilgan bo'lsa — boshidan boshlaymiz
                db.execute("DELETE FROM channel_posts")
                rows = db.execute("SELECT * FROM questions ORDER BY id ASC LIMIT 10").fetchall()

        if not rows:
            logger.warning("Bazada savol yo'q!")
            return 0

        sent = 0
        for number, q in enumerate(rows, start=1):
            payload = {
                "id": q["id"],
                "question": q["question"],
                "options": [q["option_a"], q["option_b"], q["option_c"], q["option_d"]],
                "correct": q["correct"],
            }
            result = await send_question_to_channel(payload, number)
            if result:
                message_id, poll_id = result
                with get_db() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO channel_posts(question_id, message_id, poll_id) VALUES (?, ?, ?)",
                        (q["id"], message_id, poll_id),
                    )
                sent += 1
            await asyncio.sleep(1.5)
        return sent
    except Exception as e:
        logger.error(f"Savollar yuborish xatoligi: {e}")
        return 0


# ============================================================
# PDF PARSER (qatorma-qator)
# ============================================================
Q_RE = re.compile(r'^\s*(\d{1,4})\s*[\.\)]\s*(.+)$')
OPT_RE = re.compile(r'^\s*([ABVGDАБВГДabvgd])\s*[\)\.]\s*(.*)$')
ANS_RE = re.compile(r'^\s*T?o.?g.?ri\s+javob\s*:?\s*(.*)$', re.IGNORECASE)
LETTER_INDEX = {"A": 0, "B": 1, "V": 2, "G": 3, "C": 2, "D": 3,
                "А": 0, "Б": 1, "В": 2, "Г": 3, "Д": 4}


def clean(s: str) -> str:
    s = s.replace('ѐ', 'yo')
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'\(\s*\d+(?:-\d+)*-?\s*modda(?:lar)?\s*\)', '', s, flags=re.I).strip()
    return s


def parse_questions(text: str):
    """Qaytaradi: (savollar_ro'yxati, muammolar_ro'yxati)"""
    lines = [l for l in text.split('\n')
             if l.strip()
             and not re.fullmatch(r'\s*\d+\s*', l)                       # sahifa raqami
             and not re.search(r'TESTLAR\s*-\s*SAVOLLAR', l, re.I)]      # sarlavha
    blocks, cur = [], None
    for l in lines:
        m = Q_RE.match(l)
        if m and (cur is None or cur['state'] in ('ans', 'opt')) and not OPT_RE.match(l):
            if cur:
                blocks.append(cur)
            cur = {'num': int(m.group(1)), 'q': m.group(2), 'opts': [], 'ans': None, 'state': 'q'}
            continue
        if cur is None:
            continue
        a = ANS_RE.match(l)
        if a:
            lm = re.match(r'([ABVGDАБВГДabvgd])\b', a.group(1).strip())
            cur['ans'] = LETTER_INDEX.get(lm.group(1).upper()) if lm else None
            cur['state'] = 'ans'
            continue
        o = OPT_RE.match(l)
        if o and cur['state'] in ('q', 'opt'):
            cur['opts'].append(o.group(2))
            cur['state'] = 'opt'
            continue
        if cur['state'] == 'q':          # ko'p qatorli savol
            cur['q'] += ' ' + l
        elif cur['state'] == 'opt':      # ko'p qatorli variant
            cur['opts'][-1] += ' ' + l
    if cur:
        blocks.append(cur)

    result, problems = [], []
    for b in blocks:
        opts = [clean(o) for o in b['opts']]
        q = clean(b['q'])
        if len(opts) < 2:
            problems.append(f"{b['num']}-savol: variantlar topilmadi")
            continue
        if b['ans'] is None or b['ans'] >= len(opts) or b['ans'] > 3:
            problems.append(f"{b['num']}-savol: to'g'ri javob ko'rsatilmagan yoki noto'g'ri")
            continue
        opts = (opts + ["Berilmagan"] * 4)[:4]
        result.append({'num': b['num'], 'question': q, 'options': opts, 'correct': b['ans']})
    return result, problems


def read_pdf_text(path: str) -> str:
    reader = PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


# ============================================================
# UMUMIY HANDLERLAR
# ============================================================
@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    try:
        if is_admin(message.from_user.id):
            await message.answer("👑 <b>Admin Panelga xush kelibsiz!</b>", reply_markup=admin_menu())
            return
        with get_db() as db:
            db.execute(
                """INSERT INTO users(user_id, full_name, username) VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET full_name = excluded.full_name,
                                                      username  = excluded.username""",
                (message.from_user.id, message.from_user.full_name, message.from_user.username),
            )
        await message.answer("Xush kelibsiz! Kanalimizdagi testlarda faol qatnashing.")
    except Exception as e:
        logger.error(f"Start handler xatoligi: {e}")
        await message.answer("Xatolik yuz berdi. Iltimos qayta urinib ko'ring.")


@dp.message(Command("cancel"), IsAdminMsg)
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Bekor qilindi.", reply_markup=admin_menu())


# ============================================================
# ADMIN: MENYU, USERS, REYTING
# ============================================================
@dp.callback_query(F.data == "admin_main_menu", IsAdminCb)
async def admin_main_menu_handler(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    await safe_edit(callback, "👑 <b>Admin Panel:</b>", admin_menu())


@dp.callback_query(F.data.startswith("admin_users_list_"), IsAdminCb)
async def admin_users_list_handler(callback: CallbackQuery):
    await safe_answer(callback)
    try:
        page = max(1, int(callback.data.split("_")[-1]))
        limit = 10
        offset = (page - 1) * limit
        with get_db() as db:
            users = db.execute(
                "SELECT user_id, full_name, score FROM users ORDER BY score DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = db.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]

        if not users:
            await safe_edit(callback, "Foydalanuvchilar yo'q.", back_menu())
            return

        text = f"👥 <b>Users ({offset + 1}-{min(offset + limit, total)} / Jami {total}):</b>\n\n"
        for u in users:
            text += f"<b>{escape(u['full_name'] or 'Nomsiz')}</b> — 🏆 {u['score']} ball\n"

        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="⬅️ Ortga", callback_data=f"admin_users_list_{page - 1}"))
        if offset + limit < total:
            nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin_users_list_{page + 1}"))
        kb = ([nav] if nav else []) + [[InlineKeyboardButton(text="🔙 Bosh menyu", callback_data="admin_main_menu")]]
        await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        logger.error(f"Users list xatoligi: {e}")
        await callback.message.answer("❌ Xatolik yuz berdi.", reply_markup=admin_menu())


@dp.callback_query(F.data == "admin_rating", IsAdminCb)
async def admin_rating_handler(callback: CallbackQuery):
    await safe_answer(callback)
    try:
        with get_db() as db:
            top = db.execute("SELECT full_name, score FROM users ORDER BY score DESC LIMIT 3").fetchall()
        if not top:
            text = "🏆 Hali foydalanuvchilar yo'q."
        else:
            medals = ["🥇", "🥈", "🥉"]
            text = "🏆 <b>Top-3 Reyting:</b>\n\n" + "".join(
                f"{medals[i]} <b>{escape(u['full_name'] or 'Nomsiz')}</b> — {u['score']} ball\n"
                for i, u in enumerate(top))
        await safe_edit(callback, text, back_menu())
    except Exception as e:
        logger.error(f"Rating xatoligi: {e}")
        await callback.message.answer("❌ Xatolik yuz berdi.", reply_markup=admin_menu())


# ============================================================
# ADMIN: BARCHA SAVOLLAR
# ============================================================
@dp.callback_query(F.data.startswith("admin_all_questions_"), IsAdminCb)
async def admin_all_questions_handler(callback: CallbackQuery):
    await safe_answer(callback)   # darhol javob — "query is too old" chiqmaydi
    try:
        page = max(1, int(callback.data.split("_")[-1]))
        limit = 3
        offset = (page - 1) * limit
        with get_db() as db:
            questions = db.execute(
                "SELECT * FROM questions ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            total_q = db.execute("SELECT COUNT(*) AS total FROM questions").fetchone()["total"]

        if not questions:
            await safe_edit(callback, "Bazada savollar mavjud emas.", back_menu())
            return

        text = f"📜 <b>Savollar ({offset + 1}-{min(offset + limit, total_q)} / Jami {total_q}):</b>\n\n"
        for q in questions:
            # Avval qisqartiramiz, keyin escape qilamiz — HTML teglar buzilmaydi
            opts = [q['option_a'], q['option_b'], q['option_c'], q['option_d']]
            block = f"<b>{q['id']}. {escape(shorten(q['question'], 500))}</b>\n"
            block += "".join(f"{LETTERS[i]}) {escape(shorten(opts[i], 150))}\n" for i in range(4))
            block += f"✅ To'g'ri javob: <b>{LETTERS[q['correct']]}</b>\n\n"
            text += block

        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="⬅️ Ortga", callback_data=f"admin_all_questions_{page - 1}"))
        if offset + limit < total_q:
            nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin_all_questions_{page + 1}"))
        kb = ([nav] if nav else []) + [[InlineKeyboardButton(text="🔙 Bosh menyu", callback_data="admin_main_menu")]]
        await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        logger.error(f"All questions xatoligi: {e}")
        await callback.message.answer("❌ Savollarni ko'rsatishda xatolik yuz berdi.", reply_markup=admin_menu())


# ============================================================
# ADMIN: KANALGA YUBORISH
# ============================================================
@dp.callback_query(F.data == "admin_send_10", IsAdminCb)
async def admin_send_10_handler(callback: CallbackQuery):
    await safe_answer(callback, "🚀 Savollar yuborilmoqda...")
    sent = await send_daily_questions()
    await callback.message.answer(f"📢 Kanalga <b>{sent}</b> ta savol yuborildi!", reply_markup=admin_menu())


# ============================================================
# ADMIN: BAZANI TOZALASH (tasdiqlash bilan)
# ============================================================
@dp.callback_query(F.data == "admin_clear_db", IsAdminCb)
async def admin_clear_db_ask(callback: CallbackQuery):
    await safe_answer(callback)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, o'chirilsin", callback_data="admin_clear_db_yes"),
         InlineKeyboardButton(text="❌ Yo'q", callback_data="admin_main_menu")]
    ])
    await safe_edit(callback, "⚠️ <b>Barcha savollar va yuborish tarixi o'chiriladi. Ishonchingiz komilmi?</b>", kb)


@dp.callback_query(F.data == "admin_clear_db_yes", IsAdminCb)
async def admin_clear_db_handler(callback: CallbackQuery):
    await safe_answer(callback)
    try:
        with get_db() as db:
            db.execute("DELETE FROM questions")
            db.execute("DELETE FROM channel_posts")
            db.execute("DELETE FROM sqlite_sequence WHERE name = 'questions'")  # ID yana 1 dan boshlanadi
        await safe_edit(callback, "🗑 <b>Barcha savollar va tarix tozalandi!</b>", admin_menu())
    except Exception as e:
        logger.error(f"Clear DB xatoligi: {e}")
        await callback.message.answer("❌ Xatolik yuz berdi.", reply_markup=admin_menu())


# ============================================================
# ADMIN: QO'LDA SAVOL QO'SHISH
# ============================================================
@dp.callback_query(F.data == "admin_add_manual", IsAdminCb)
async def add_manual_start(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(AddQuestion.question)
    await callback.message.answer("📝 <b>Savol matnini kiriting:</b>\n(Bekor qilish: /cancel)")


async def _next_step(message: Message, state: FSMContext, key: str, next_state, prompt: str):
    if not message.text:
        await message.answer("Iltimos, matn yuboring.")
        return
    await state.update_data(**{key: message.text.strip()})
    await state.set_state(next_state)
    await message.answer(prompt)


@dp.message(AddQuestion.question, IsAdminMsg)
async def process_q(message: Message, state: FSMContext):
    await _next_step(message, state, "question", AddQuestion.option_a, "A) variantini kiriting:")


@dp.message(AddQuestion.option_a, IsAdminMsg)
async def process_opt_a(message: Message, state: FSMContext):
    await _next_step(message, state, "option_a", AddQuestion.option_b, "B) variantini kiriting:")


@dp.message(AddQuestion.option_b, IsAdminMsg)
async def process_opt_b(message: Message, state: FSMContext):
    await _next_step(message, state, "option_b", AddQuestion.option_c, "V) variantini kiriting:")


@dp.message(AddQuestion.option_c, IsAdminMsg)
async def process_opt_c(message: Message, state: FSMContext):
    await _next_step(message, state, "option_c", AddQuestion.option_d, "G) variantini kiriting:")


@dp.message(AddQuestion.option_d, IsAdminMsg)
async def process_opt_d(message: Message, state: FSMContext):
    await _next_step(message, state, "option_d", AddQuestion.correct,
                     "To'g'ri javob harfini kiriting: <b>A, B, V yoki G</b>")


@dp.message(AddQuestion.correct, IsAdminMsg)
async def process_correct(message: Message, state: FSMContext):
    ans = (message.text or "").strip().upper()
    mapping = {"A": 0, "B": 1, "V": 2, "G": 3, "0": 0, "1": 1, "2": 2, "3": 3}
    if ans not in mapping:
        await message.answer("Faqat <b>A, B, V</b> yoki <b>G</b> harfini kiriting!")
        return
    data = await state.get_data()
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (data['question'], data['option_a'], data['option_b'], data['option_c'],
                 data['option_d'], mapping[ans]),
            )
        await state.clear()
        await message.answer("✅ <b>Savol qo'shildi!</b>", reply_markup=admin_menu())
    except Exception as e:
        logger.error(f"Savol qo'shish xatoligi: {e}")
        await message.answer("❌ Xatolik yuz berdi!", reply_markup=admin_menu())


# ============================================================
# ADMIN: PDF YUKLASH
# ============================================================
@dp.callback_query(F.data == "admin_add_pdf", IsAdminCb)
async def add_pdf_start(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(UploadPDF.file)
    await callback.message.answer("📄 <b>PDF faylni yuboring:</b>\n(Bekor qilish: /cancel)")


@dp.message(UploadPDF.file, F.document, IsAdminMsg)
async def process_pdf(message: Message, state: FSMContext):
    name = (message.document.file_name or "").lower()
    if not name.endswith(".pdf"):
        await message.answer("Faqat PDF fayl yuboring!")
        return

    msg = await message.answer("⏳ PDF tahlil qilinmoqda...")
    file_path = f"temp_{message.document.file_unique_id}.pdf"
    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, file_path)

        # Og'ir ishni alohida oqimda bajaramiz — bot bu vaqtda qotib qolmaydi
        full_text = await asyncio.to_thread(read_pdf_text, file_path)
        questions, problems = await asyncio.to_thread(parse_questions, full_text)

        added, duplicates = 0, 0
        with get_db() as db:
            existing = {r["question"] for r in db.execute("SELECT question FROM questions")}
            for q in questions:
                if q['question'] in existing:      # bir PDF ikki marta yuklansa, takrorlanmaydi
                    duplicates += 1
                    continue
                o = q['options']
                db.execute(
                    """INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (q['question'], o[0], o[1], o[2], o[3], q['correct']),
                )
                existing.add(q['question'])
                added += 1

        await state.clear()
        text = f"✅ <b>PDF saqlandi!</b>\n\n📥 Yangi qo'shildi: <b>{added}</b> ta"
        if duplicates:
            text += f"\n♻️ Bazada bor edi (o'tkazildi): <b>{duplicates}</b> ta"
        if problems:
            text += f"\n⚠️ O'qib bo'lmadi: <b>{len(problems)}</b> ta\n\n" + "\n".join(problems[:20])
        await msg.edit_text(text[:MSG_LIMIT], reply_markup=admin_menu())
    except Exception as e:
        logger.error(f"PDF parsing xatoligi: {e}")
        await msg.edit_text(f"❌ PDF o'qishda xatolik: {escape(str(e))}", reply_markup=admin_menu())
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


@dp.message(UploadPDF.file, IsAdminMsg)
async def process_pdf_wrong(message: Message):
    await message.answer("📄 Iltimos, <b>PDF fayl</b> yuboring yoki /cancel bosing.")


# ============================================================
# ISHGA TUSHIRISH
# ============================================================
async def on_startup(bot: Bot):
    await bot.set_webhook(
        BASE_URL + WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logger.info(f"Webhook o'rnatildi: {BASE_URL + WEBHOOK_PATH}")


def run_webhook():
    """AlwaysData uchun: HTTP server (IP va PORT ni AlwaysData beradi)"""
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
    """Kompyuterda sinash uchun"""
    init_db()
    await bot.delete_webhook(drop_pending_updates=False)
    logger.warning("Polling rejimi: serverdagi webhook o'chirildi! Tugatgach AlwaysData'da Restart qiling.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if os.getenv("PORT"):
        run_webhook()                 # AlwaysData (server)
    else:
        asyncio.run(run_polling())    # O'z kompyuteringiz
