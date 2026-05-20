import os
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

ptb_app.add_handler(CommandHandler("start",   telegram_handler.handle_start))
ptb_app.add_handler(CommandHandler("help",    telegram_handler.handle_help))
ptb_app.add_handler(CommandHandler("listdoc", telegram_handler.handle_listdoc))
ptb_app.add_handler(CommandHandler("remove",  telegram_handler.handle_remove))
ptb_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_handler.handle_text)
)
ptb_app.add_handler(MessageHandler(filters.PHOTO,        telegram_handler.handle_photo))
ptb_app.add_handler(MessageHandler(filters.Document.ALL, telegram_handler.handle_document))
ptb_app.add_handler(CallbackQueryHandler(telegram_handler.handle_callback_query))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ptb_app.initialize()
    await ptb_app.start()

    await ptb_app.bot.set_my_commands([
        BotCommand("start",   "Welcome message"),
        BotCommand("help",    "Usage guide and supported file types"),
        BotCommand("listdoc", "List all indexed documents"),
        BotCommand("remove",  "Remove a document — /remove <number>"),
    ])

    if TELEGRAM_WEBHOOK_URL:
        webhook_url = f"{TELEGRAM_WEBHOOK_URL.rstrip('/')}/webhook"
        for attempt in range(1, 4):
            try:
                await ptb_app.bot.set_webhook(
                    url=webhook_url,
                    allowed_updates=["message", "callback_query"],
                )
                print(f"[Startup] Telegram webhook set to {webhook_url}")
                break
            except Exception as e:
                print(f"[Startup] Webhook attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    await asyncio.sleep(3)
                else:
                    print("[Startup] Warning: could not register webhook — run healthcheck.sh --fix")
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
    exhausted = {m: t for m, t in gemini._exhausted_until.items()}
    return {
        "status": "ok",
        "model": gemini._get_active_model(),
        "rotation": gemini.ROTATION_MODELS,
        "exhausted": {m: f"resets in {int(t - __import__('time').time())}s" for m, t in exhausted.items()},
        "store": gemini._store_name or "not initialized",
    }


@fastapi_app.get("/store/info")
async def store_info() -> dict:
    loop = asyncio.get_event_loop()
    try:
        store_name = await asyncio.wait_for(
            loop.run_in_executor(None, gemini.get_or_create_store), timeout=10.0
        )
        client = gemini.get_client()
        store = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: client.file_search_stores.get(name=store_name)),
            timeout=10.0,
        )
        documents = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: list(client.file_search_stores.documents.list(parent=store_name)),
            ),
            timeout=15.0,
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
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Gemini API timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@fastapi_app.post("/webhook")
async def webhook(request: Request) -> dict:
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    asyncio.create_task(ptb_app.process_update(update))
    return {"ok": True}
