import os
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

import app.gemini_service as gemini
from app import telegram_handler

TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_URL = os.environ.get("TELEGRAM_WEBHOOK_URL", "")

# Build PTB Application as a module-level singleton.
# updater(None) disables PTB's built-in HTTP server so FastAPI owns the I/O layer.
ptb_app = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .updater(None)
    .build()
)

ptb_app.add_handler(CommandHandler("start", telegram_handler.handle_start))
ptb_app.add_handler(CommandHandler("help",  telegram_handler.handle_help))
ptb_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_handler.handle_text)
)
ptb_app.add_handler(MessageHandler(filters.PHOTO,        telegram_handler.handle_photo))
ptb_app.add_handler(MessageHandler(filters.Document.ALL, telegram_handler.handle_document))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ptb_app.initialize()
    await ptb_app.start()

    if TELEGRAM_WEBHOOK_URL:
        webhook_url = f"{TELEGRAM_WEBHOOK_URL.rstrip('/')}/webhook"
        await ptb_app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message"],
        )
        print(f"[Startup] Telegram webhook set to {webhook_url}")
    else:
        print("[Startup] TELEGRAM_WEBHOOK_URL not set — webhook not registered")

    loop = asyncio.get_event_loop()
    try:
        store = await loop.run_in_executor(None, gemini.get_or_create_store)
        print(f"[Startup] File Search Store ready: {store}")
    except Exception as e:
        print(f"[Startup] Warning: could not initialize store: {e}")

    yield

    await ptb_app.bot.delete_webhook()
    await ptb_app.stop()
    await ptb_app.shutdown()


fastapi_app = FastAPI(title="Telegram Bot Multimodal RAG", lifespan=lifespan)


@fastapi_app.get("/health")
async def health() -> dict:
    return {"status": "ok", "store": gemini._store_name or "not initialized"}


@fastapi_app.get("/store/info")
async def store_info() -> dict:
    loop = asyncio.get_event_loop()
    try:
        store_name = await loop.run_in_executor(None, gemini.get_or_create_store)
        client = gemini.get_client()
        store = await loop.run_in_executor(
            None, lambda: client.file_search_stores.get(name=store_name)
        )
        documents = await loop.run_in_executor(
            None,
            lambda: list(
                client.file_search_stores.documents.list(parent=store_name)
            ),
        )
        return {
            "store_name": store_name,
            "display_name": getattr(store, "display_name", ""),
            "document_count": len(documents),
            "documents": [
                {
                    "name": getattr(d, "name", ""),
                    "display_name": getattr(d, "display_name", ""),
                    "state": str(getattr(d, "state", "")),
                }
                for d in documents
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@fastapi_app.post("/webhook")
async def webhook(request: Request) -> dict:
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"ok": True}
