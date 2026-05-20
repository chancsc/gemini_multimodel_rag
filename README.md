# Gemini Multimodal RAG Bot (via Telegram)

A Telegram bot that lets you build a personal knowledge base from documents and images, then query it using natural language. Powered by **Google Gemini File Search Store** for retrieval-augmented generation (RAG).

Inspired by [linebot-multimodal-rag](https://github.com/kkdai/linebot-multimodal-rag) — ported from LINE to Telegram.

---

## Features

- **Upload documents** — PDF, DOCX, XLSX, PPTX, TXT, CSV, Markdown, HTML, JSON, YAML, ZIP, images (JPG/PNG), and most code files
- **Per-user isolation** — each user's documents are stored and queried independently
- **Persistent knowledge base** — embeddings stored in Gemini File Search Store with no TTL; documents persist until you delete them
- **Async indexing** — indexing runs in the background; you get a notification when done
- **Source citations** — responses include the source document names
- **Multimodal** — query with text or send a photo to search visually against stored documents

---

## How It Works

```
User sends file
      │
      ▼
Telegram Bot receives file bytes
      │
      ├─── 📥 Save to Database
      │         │
      │         ▼
      │    Upload to Gemini File Search Store
      │    (chunked + embedded with gemini-embedding-2)
      │    Tagged with user_id for isolation
      │    Stored permanently as vector embeddings
      │
      └─── 🔍 Use as Search
                │
                ▼
           One-shot RAG query against stored docs
           (nothing saved)

User sends text query
      │
      ▼
Gemini searches user's stored embeddings
      │
      ▼
Answer grounded in matching document content + source citations
```

---

## Architecture

```
multimodel_rag/
├── app/
│   ├── main.py              # FastAPI app + python-telegram-bot wiring
│   ├── telegram_handler.py  # Telegram event handlers
│   ├── gemini_service.py    # Gemini File Search Store + RAG queries
│   └── session.py           # In-memory session store (5-min TTL)
├── Dockerfile
├── requirements.txt
└── .env.example
```

**Tech stack:**
- [FastAPI](https://fastapi.tiangolo.com/) — webhook receiver
- [python-telegram-bot v20+](https://python-telegram-bot.org/) — Telegram SDK (embedded, no polling)
- [google-genai v1.10+](https://pypi.org/project/google-genai/) — Gemini File Search Store + generation
- [Google Cloud Storage](https://cloud.google.com/storage) — optional: store name persistence (can be replaced with env var)

---

## Setup

### 1. Prerequisites

- Python 3.12+
- A [Telegram bot token](https://core.telegram.org/bots/tutorial) from @BotFather
- A [Gemini API key](https://aistudio.google.com/apikey)
- A public HTTPS URL for the webhook (e.g. [cloudflared tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/))

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_WEBHOOK_URL=https://your-public-domain.com
GEMINI_API_KEY=your-gemini-api-key
GCS_BUCKET=                        # optional GCS bucket name
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_STORE_NAME=                  # paste existing store name to reuse across restarts
```

> **`GEMINI_STORE_NAME`** — On first run, the bot creates a new File Search Store and prints its name in the logs (e.g. `fileSearchStores/telegrambot-xxxx`). Paste that value here so documents survive restarts without needing Google Cloud Storage auth.

### 4. Expose webhook

Using [cloudflared quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/):

```bash
cloudflared tunnel --url http://localhost:8080
```

Copy the generated URL (e.g. `https://xxx.trycloudflare.com`) into `TELEGRAM_WEBHOOK_URL` in `.env`.

### 5. Run

```bash
uvicorn app.main:fastapi_app --port 8080
```

On startup the bot registers its webhook with Telegram and initializes the File Search Store automatically.

---

## Usage

| Action | What to do |
|---|---|
| **Query your knowledge base** | Send any text message |
| **Upload a document** | Send a file → tap **📥 Save to Database** |
| **Quick one-time search** | Send a file → tap **🔍 Use as Search** |
| **Upload a photo** | Send a photo → same two options |

### Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | Usage guide and supported file types |

---

## File Support

| Category | Formats |
|---|---|
| Documents | PDF, DOCX, XLSX, PPTX, RTF |
| Text | TXT, CSV, TSV, Markdown, HTML, JSON, YAML, XML, LaTeX |
| Code | Python, JavaScript, Java, SQL, and most other languages |
| Archives | ZIP |
| Images | JPG, PNG (max 4K×4K) |
| **Not supported** | Audio, Video |

**File size limit:** 20 MB (Telegram bot API constraint). Gemini supports up to 100 MB per document.

---

## Storage & Persistence

| What | Where | TTL |
|---|---|---|
| Vector embeddings | Gemini File Search Store (Google cloud) | None — permanent |
| Raw uploaded files | Gemini temp storage | 48 hours (only used during indexing) |
| File bytes during upload | In-memory session | 5 minutes |
| Store name | `.env` (`GEMINI_STORE_NAME`) or GCS | Until you change it |

---

## Diagnostic Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Bot status and active store name |
| `GET /store/info` | Lists all indexed documents in the store |

```bash
curl http://localhost:8080/health
curl http://localhost:8080/store/info
```

---

## Docker

```bash
docker build -t telegram-rag-bot .
docker run --env-file .env -p 8080:8080 telegram-rag-bot
```

---

## License

MIT
