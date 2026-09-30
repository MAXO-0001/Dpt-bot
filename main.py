import asyncio
import html
import logging
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DPT_JAR = Path(os.environ.get("DPT_JAR", "/opt/dpt/dpt.jar"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "users.sqlite3"
JOBS_DIR = DATA_DIR / "jobs"
DPT_VERSION = os.environ.get("DPT_VERSION", "2.19.0")
MAX_INPUT_MB = 20  # Telegram cloud Bot API getFile limit
MAX_OUTPUT_MB = 50  # Telegram sendDocument limit
MAX_DPT_SECONDS = int(os.environ.get("MAX_DPT_SECONDS", "7200"))

BRAND = "MAXO DPT"
CAPTION = "CREATE BY MAXO\n@Pv_MAXO"
OUTPUT_PREFIX = "✧ 𝐂𝐑𝐄𝐀𝐓𝐄 𝐁𝐘 𝐌𝐀𝐗𝐎 ✧"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("maxo-dpt")

DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL)")
DB.commit()
DB_LOCK = asyncio.Lock()
JOB_LOCK = asyncio.Lock()
ENABLED = True


def db_touch(chat_id: int):
    now = int(time.time())
    DB.execute(
        "INSERT INTO users(chat_id,first_seen,last_seen) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET last_seen=excluded.last_seen",
        (chat_id, now, now),
    )
    DB.commit()


def db_users():
    return [r[0] for r in DB.execute("SELECT chat_id FROM users ORDER BY first_seen").fetchall()]


def menu(is_admin=False):
    rows = [[KeyboardButton("🔴 آپلود فایل", style="danger")]]
    if is_admin:
        rows.append([KeyboardButton("🔴 مدیریت", style="danger")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="فقط APK ارسال کنید")


def admin_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔴 روشن/خاموش", style="danger"), KeyboardButton("🔴 کاربران", style="danger")],
        [KeyboardButton("🔴 پیام همگانی", style="danger")],
        [KeyboardButton("🔴 بازگشت", style="danger")],
    ], resize_keyboard=True, is_persistent=True)


def clean_name(name: str) -> str:
    name = Path(name or "app.apk").name
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return safe[:180] or "app.apk"


def find_output(out_dir: Path):
    files = [p for p in out_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".apk"]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime_ns)


def dpt_cmd(input_file: Path, output_dir: Path):
    return ["java", "-jar", str(DPT_JAR), "-f", str(input_file), "-o", str(output_dir)]


async def run_dpt(input_file: Path, output_dir: Path):
    if not DPT_JAR.exists():
        raise RuntimeError("DPT Shell is not installed in the container.")
    output_dir.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *dpt_cmd(input_file, output_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    start = time.monotonic()
    chunks = []
    # Do not report a false timeout while DPT is still working. We only fail after
    # the process has actually exceeded the configured hard limit.
    while True:
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
            if chunk:
                chunks.append(chunk.decode("utf-8", errors="replace"))
            if proc.returncode is not None:
                break
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                break
            if time.monotonic() - start > MAX_DPT_SECONDS:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"DPT exceeded {MAX_DPT_SECONDS // 60} minutes without finishing.")
    code = await proc.wait()
    output = "".join(chunks)
    if code != 0:
        raise RuntimeError(f"DPT exited with code {code}.\n{output[-2500:]}")
    result = find_output(output_dir)
    if not result:
        raise RuntimeError("DPT finished successfully, but no output APK was found.")
    return result


async def safe_status(message, text):
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


async def process_document(update, context, document):
    global ENABLED
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not ENABLED and user.id != ADMIN_ID:
        await message.reply_text("🔴 <b>سرویس موقتاً خاموش است.</b>", parse_mode=ParseMode.HTML, reply_markup=menu(user.id == ADMIN_ID))
        return
    name = clean_name(document.file_name)
    if not name.lower().endswith(".apk"):
        await message.reply_text("❌ <b>فقط فایل APK قبول می‌شود.</b>\nZIP و 7Z پشتیبانی نمی‌شوند.", parse_mode=ParseMode.HTML, reply_markup=menu(user.id == ADMIN_ID))
        return
    if document.file_size and document.file_size > MAX_INPUT_MB * 1024 * 1024:
        await message.reply_text(f"❌ <b>حجم APK بیشتر از {MAX_INPUT_MB}MB است.</b>", parse_mode=ParseMode.HTML, reply_markup=menu(user.id == ADMIN_ID))
        return

    job = JOBS_DIR / f"{user.id}_{uuid.uuid4().hex}"
    inp = job / "input.apk"
    out = job / "out"
    job.mkdir(parents=True, exist_ok=True)
    status = await message.reply_text("⏳ <b>MAXO DPT</b>\n\nفایل دریافت شد.\nدر حال آماده‌سازی...", parse_mode=ParseMode.HTML)
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(custom_path=str(inp))
        if inp.stat().st_size > MAX_INPUT_MB * 1024 * 1024:
            raise RuntimeError(f"حجم فایل بیشتر از {MAX_INPUT_MB}MB است.")
        await safe_status(status, "⚙️ <b>MAXO DPT</b>\n\nدر حال اجرای DPT Shell...\nاین مرحله ممکن است زمان‌بر باشد.")
        async with JOB_LOCK:
            result = await run_dpt(inp, out)
        size = result.stat().st_size
        if size > MAX_OUTPUT_MB * 1024 * 1024:
            raise RuntimeError(f"حجم خروجی بیشتر از {MAX_OUTPUT_MB}MB است و Telegram آن را ارسال نمی‌کند.")
        final_name = OUTPUT_PREFIX + ".apk"
        await safe_status(status, "✅ <b>DPT با موفقیت انجام شد.</b>\n\nدر حال ارسال خروجی...")
        with result.open("rb") as f:
            await message.reply_document(document=f, filename=final_name, caption=CAPTION, parse_mode=ParseMode.HTML, reply_markup=menu(user.id == ADMIN_ID))
        await status.delete()
    except Exception as e:
        log.exception("job failed")
        await safe_status(status, f"❌ <b>پردازش ناموفق بود.</b>\n\n<code>{html.escape(str(e)[:1800])}</code>")
        if ADMIN_ID:
            try:
                await context.bot.send_message(ADMIN_ID, f"⚠️ <b>MAXO DPT ERROR</b>\n\nUser: <code>{user.id}</code>\nFile: <code>{html.escape(name)}</code>\n\n<code>{html.escape(str(e)[:2500])}</code>", parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
    finally:
        shutil.rmtree(job, ignore_errors=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u: return
    async with DB_LOCK:
        db_touch(u.id)
    await update.message.reply_text(
        "╭────────────────────╮\n"
        "        <b>MAXO DPT</b>\n"
        "╰────────────────────╯\n\n"
        "<b>محافظت حرفه‌ای APK با DPT Shell</b>\n\n"
        "فقط فایل <b>APK</b> را ارسال کنید.\n"
        "ZIP و 7Z پشتیبانی نمی‌شوند.\n\n"
        "برای شروع، روی دکمه <b>آپلود فایل</b> بزنید یا یک APK را ریپلای کرده و <code>/dpt</code> را ارسال کنید.",
        parse_mode=ParseMode.HTML, reply_markup=menu(u.id == ADMIN_ID))


async def dpt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u: return
    async with DB_LOCK: db_touch(u.id)
    r = update.message.reply_to_message
    if not r or not r.document:
        await update.message.reply_text("↩️ <b>/dpt</b> را روی پیام فایل APK ریپلای کنید.", parse_mode=ParseMode.HTML)
        return
    await process_document(update, context, r.document)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("🔴 <b>MAXO ADMIN</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ENABLED
    u = update.effective_user
    if not u: return
    async with DB_LOCK: db_touch(u.id)
    t = update.message.text or ""
    if t == "🔴 آپلود فایل":
        await update.message.reply_text("📦 <b>فایل APK را ارسال کنید.</b>\n\nحداکثر حجم ورودی در Bot API ابری: 20MB.", parse_mode=ParseMode.HTML, reply_markup=menu(u.id == ADMIN_ID))
    elif t == "🔴 مدیریت" and u.id == ADMIN_ID:
        await update.message.reply_text("🔴 <b>پنل مدیریت MAXO</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif t == "🔴 روشن/خاموش" and u.id == ADMIN_ID:
        ENABLED = not ENABLED
        await update.message.reply_text(f"وضعیت سرویس: <b>{'روشن' if ENABLED else 'خاموش'}</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif t == "🔴 کاربران" and u.id == ADMIN_ID:
        users = db_users()
        await update.message.reply_text(f"👥 <b>تعداد کاربران:</b> {len(users)}\n\n" + ("\n".join(f"<code>{x}</code>" for x in users) if users else "کاربری ثبت نشده است."), parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif t == "🔴 پیام همگانی" and u.id == ADMIN_ID:
        context.user_data["broadcast"] = True
        await update.message.reply_text("📢 پیام همگانی را در پیام بعدی ارسال کنید.", reply_markup=admin_menu())
    elif t == "🔴 بازگشت" and u.id == ADMIN_ID:
        await update.message.reply_text("بازگشت به منوی اصلی.", reply_markup=menu(True))
    elif context.user_data.get("broadcast") and u.id == ADMIN_ID:
        context.user_data["broadcast"] = False
        users = db_users()
        ok = 0
        for chat_id in users:
            try:
                await context.bot.copy_message(chat_id, u.id, update.message.message_id)
                ok += 1
                await asyncio.sleep(0.04)
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after) + 0.1)
            except TelegramError:
                pass
        await update.message.reply_text(f"📢 ارسال انجام شد.\nموفق: <b>{ok}</b> از <b>{len(users)}</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u: return
    async with DB_LOCK: db_touch(u.id)
    await process_document(update, context, update.message.document)


def main():
    if not TOKEN or ADMIN_ID == 0:
        raise SystemExit("BOT_TOKEN and ADMIN_ID must be set in Railway Variables.")
    app = Application.builder().token(TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("dpt", dpt))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("MAXO DPT started | DPT=%s | version=%s", DPT_JAR, DPT_VERSION)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
