import asyncio
import mimetypes
import traceback
from io import BytesIO
import os

from telegram import Update, ReactionTypeEmoji, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from app.session import session_store
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
# UI helpers
# ---------------------------------------------------------------------------

def _replace_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Replace",    callback_data="dup=replace"),
        InlineKeyboardButton("➕ Keep Both",  callback_data="dup=keep"),
        InlineKeyboardButton("❌ Cancel",     callback_data="dup=cancel"),
    ]])


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
    old_doc_name: str = "",      # if set, delete this before indexing
) -> None:
    print(f"[BG Store] Task started: {display_name!r} ({len(file_bytes)} bytes)")
    try:
        if old_doc_name:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, gemini.delete_document, old_doc_name)
            print(f"[BG Store] Deleted old doc: {old_doc_name!r}")

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

async def handle_listdoc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loop = asyncio.get_event_loop()
    try:
        docs = await loop.run_in_executor(None, gemini.list_documents)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to list documents: {str(e)[:120]}")
        return

    if not docs:
        await update.message.reply_text("📭 No documents in the database yet.")
        return

    lines = ["📚 Documents in database:\n"]
    for i, doc in enumerate(docs, 1):
        state = "✅" if "ACTIVE" in doc["state"] else "⏳"
        lines.append(f"{i}. {state} {doc['display_name']}")
    lines.append("\nUse /remove <number> to delete a document.")
    await update.message.reply_text("\n".join(lines))


async def handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /remove <number>\nGet the number from /listdoc")
        return

    index = int(args[0]) - 1
    loop = asyncio.get_event_loop()
    try:
        docs = await loop.run_in_executor(None, gemini.list_documents)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch documents: {str(e)[:120]}")
        return

    if index < 0 or index >= len(docs):
        await update.message.reply_text(f"⚠️ Invalid number. Use /listdoc to see valid numbers (1–{len(docs)}).")
        return

    doc = docs[index]
    try:
        await loop.run_in_executor(None, gemini.delete_document, doc["name"])
        await update.message.reply_text(f"🗑️ Deleted: {doc['display_name']}")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to delete: {str(e)[:120]}")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to the Multimodal RAG bot!\n\n"
        "Send me a text message to query your knowledge base.\n"
        "Send a photo or document to index it automatically.\n\n"
        "Commands:\n"
        "/start   — Show this message\n"
        "/help    — Show help\n"
        "/listdoc — List all indexed documents\n"
        "/remove <number> — Delete a document"
    )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 How to use:\n"
        "• Send a document or photo → automatically indexed to your database\n"
        "• Send a text message → RAG query against all stored documents\n\n"
        "Commands:\n"
        "/listdoc — List all indexed documents\n"
        "/remove <number> — Delete a document by its list number\n\n"
        "Supported file types: PDF, DOCX, XLSX, PPTX, images (JPG/PNG), "
        "TXT, CSV, Markdown, HTML, JSON, YAML, ZIP, and most code files.\n"
        f"File size limit: {TELEGRAM_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB (Telegram bot constraint)."
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Send typing action every 4 s until cancelled."""
    while True:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(4)


QUERY_TIMEOUT_SECONDS = 120


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if not text:
        return
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    try:
        await update.message.set_reaction([ReactionTypeEmoji(emoji="👀")])
    except Exception:
        pass  # reactions are best-effort

    typing_task = asyncio.create_task(_keep_typing(context, chat_id))
    try:
        answer = await asyncio.wait_for(
            gemini.query_with_text(text, user_id),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        answer = f"⏱️ Query timed out after {QUERY_TIMEOUT_SECONDS}s. Please try a shorter question or try again later."
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            answer = "⚠️ Gemini API quota exceeded for today. Please try again tomorrow or enable billing on your Google AI project."
        else:
            answer = f"❌ Query failed: {err[:120]}"
    finally:
        typing_task.cancel()

    await update.message.reply_text(answer)


async def _receive_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_bytes: bytes,
    mime_type: str,
    display_name: str,
    gcs_path: str,
) -> None:
    """Shared logic after file bytes are downloaded: duplicate check → index or prompt."""
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    loop = asyncio.get_event_loop()

    # Duplicate check
    existing = await loop.run_in_executor(None, gemini.find_document_by_name, display_name)

    await update.message.set_reaction([ReactionTypeEmoji(emoji="👀")])

    if existing:
        # Store file in session so the callback handler can retrieve it
        session_store.set(user_id, "file_bytes",    file_bytes)
        session_store.set(user_id, "mime_type",     mime_type)
        session_store.set(user_id, "display_name",  display_name)
        session_store.set(user_id, "chat_id",       chat_id)
        session_store.set(user_id, "old_doc_name",  existing["name"])
        await update.message.reply_text(
            f"⚠️ *{display_name}* is already in your database.\nWhat would you like to do?",
            parse_mode="Markdown",
            reply_markup=_replace_keyboard(),
        )
    else:
        _try_save_to_gcs(file_bytes, gcs_path, mime_type)
        await update.message.reply_text(f"⏳ Received {display_name} — indexing will start soon.")
        asyncio.create_task(
            _bg_store_and_notify(context, chat_id, user_id, file_bytes, mime_type, display_name)
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    photo = update.message.photo[-1]

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
        await _receive_file(
            update, context,
            file_bytes=buf.getvalue(),
            mime_type="image/jpeg",
            display_name=f"photo_{photo.file_id}.jpg",
            gcs_path=f"uploads/{user_id}/{photo.file_id}.jpg",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to process photo: {str(e)[:120]}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
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
        await _receive_file(
            update, context,
            file_bytes=buf.getvalue(),
            mime_type=mime_type,
            display_name=filename,
            gcs_path=f"uploads/{user_id}/{doc.file_id}.{ext}",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to process file: {str(e)[:120]}")


# ---------------------------------------------------------------------------
# Callback query handler (duplicate resolution only)
# ---------------------------------------------------------------------------

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    action  = query.data

    file_bytes   = session_store.get(user_id, "file_bytes")
    mime_type    = session_store.get(user_id, "mime_type")
    display_name = session_store.get(user_id, "display_name")
    chat_id      = session_store.get(user_id, "chat_id") or update.effective_chat.id
    old_doc_name = session_store.get(user_id, "old_doc_name") or ""

    if file_bytes is None:
        await query.edit_message_text("⚠️ Session expired (5 min). Please re-upload the file.")
        return

    session_store.clear(user_id)

    if action == "dup=cancel":
        await query.edit_message_text("❌ Upload cancelled.")

    elif action == "dup=keep":
        await query.edit_message_text(f"⏳ Indexing additional copy of {display_name}...")
        asyncio.create_task(
            _bg_store_and_notify(context, chat_id, user_id, file_bytes, mime_type, display_name)
        )

    elif action == "dup=replace":
        await query.edit_message_text(f"⏳ Replacing {display_name}...")
        asyncio.create_task(
            _bg_store_and_notify(
                context, chat_id, user_id, file_bytes, mime_type, display_name,
                old_doc_name=old_doc_name,
            )
        )
