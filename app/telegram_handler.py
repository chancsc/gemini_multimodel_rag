import asyncio
import mimetypes
import traceback
from io import BytesIO
import os

from telegram import Update, ReactionTypeEmoji
from telegram.ext import ContextTypes

import app.gemini_service as gemini

GCS_BUCKET = os.environ.get("GCS_BUCKET", "")

TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Telegram bot download limit


# ---------------------------------------------------------------------------
# GCS helpers (optional — silently skipped if credentials are unavailable)
# ---------------------------------------------------------------------------

def _try_save_to_gcs(data: bytes, path: str, content_type: str) -> None:
    if not GCS_BUCKET:
        return
    try:
        from google.cloud import storage as gcs
        client = gcs.Client()
        client.bucket(GCS_BUCKET).blob(path).upload_from_string(data, content_type=content_type)
        print(f"[GCS] Saved {path}")
    except Exception as e:
        print(f"[GCS] Save skipped: {e}")


# ---------------------------------------------------------------------------
# Background indexing task
# ---------------------------------------------------------------------------

async def _bg_store_and_notify(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: str,
    file_bytes: bytes,
    mime_type: str,
    display_name: str,
) -> None:
    print(f"[BG Store] Task started: {display_name!r} ({len(file_bytes)} bytes)")
    try:
        await gemini.upload_and_index(file_bytes, mime_type, display_name, user_id)
        print(f"[BG Store] Indexed successfully: {display_name!r}")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Successfully saved to your database!\n📄 {display_name}",
        )
    except Exception as e:
        print(f"[BG Store] Error: {e}\n{traceback.format_exc()}")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Failed to save: {str(e)[:120]}",
        )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to the Multimodal RAG bot!\n\n"
        "Send me a text message to query your personal knowledge base.\n"
        "Send a photo or document to save or search it.\n\n"
        "Commands:\n"
        "/start — Show this message\n"
        "/help  — Show help"
    )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 How to use:\n"
        "• Text → RAG query against your stored documents\n"
        "• Photo → Choose: save to DB or search DB with image\n"
        "• Document → Choose: save to DB or search DB\n\n"
        "Supported file types: PDF, DOCX, XLSX, PPTX, images (JPG/PNG), "
        "TXT, CSV, Markdown, HTML, JSON, YAML, ZIP, and most code files.\n"
        f"File size limit: {TELEGRAM_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB (Telegram bot constraint)."
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if not text:
        return
    user_id = str(update.effective_user.id)
    try:
        answer = await gemini.query_with_text(text, user_id)
    except Exception as e:
        answer = f"❌ Query failed: {str(e)[:120]}"
    await update.message.reply_text(answer)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]  # highest resolution

    if photo.file_size and photo.file_size > TELEGRAM_MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"⚠️ Photo is too large ({photo.file_size // 1024 // 1024} MB). "
            f"Limit is {TELEGRAM_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB."
        )
        return

    try:
        tg_file = await photo.get_file()
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        image_bytes = buf.getvalue()

        display_name = f"photo_{photo.file_id}.jpg"
        _try_save_to_gcs(image_bytes, f"uploads/{user_id}/{photo.file_id}.jpg", "image/jpeg")

        await update.message.set_reaction([ReactionTypeEmoji(emoji="👀")])
        await update.message.reply_text("⏳ Received your photo — indexing will start soon.")
        asyncio.create_task(
            _bg_store_and_notify(context, chat_id, user_id, image_bytes, "image/jpeg", display_name)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to process photo: {str(e)[:120]}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    doc = update.message.document

    filename  = doc.file_name or f"file_{doc.file_id}"
    mime_type = doc.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    ext       = filename.rsplit(".", 1)[-1] if "." in filename else "bin"

    if doc.file_size and doc.file_size > TELEGRAM_MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"⚠️ File \"{filename}\" is too large ({doc.file_size // 1024 // 1024} MB). "
            f"Limit is {TELEGRAM_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB."
        )
        return

    if any(mime_type.startswith(u) for u in ("audio/", "video/")):
        await update.message.reply_text(
            "⚠️ Audio and video files are not supported.\n"
            "Supported formats: PDF, images, TXT, CSV, Markdown."
        )
        return

    try:
        tg_file = await doc.get_file()
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        file_bytes = buf.getvalue()

        _try_save_to_gcs(file_bytes, f"uploads/{user_id}/{doc.file_id}.{ext}", mime_type)

        await update.message.set_reaction([ReactionTypeEmoji(emoji="👀")])
        await update.message.reply_text(f"⏳ Received {filename} — indexing will start soon.")
        asyncio.create_task(
            _bg_store_and_notify(context, chat_id, user_id, file_bytes, mime_type, filename)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to process file: {str(e)[:120]}")


