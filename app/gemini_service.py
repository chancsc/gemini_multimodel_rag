import os
import time
import asyncio
import tempfile
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from google import genai
from google.genai import types
from google.cloud import storage as gcs

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
GEN_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")

STORE_NAME_BLOB = "config/file_search_store_name.txt"
STORE_NAME_ENV  = os.environ.get("GEMINI_STORE_NAME", "")
SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer questions based on the documents in the database. "
    "Always paraphrase and summarize in your own words — do not copy text verbatim from documents. "
    "If the database does not have enough information, say so clearly and give your best general answer. "
    "Respond in the same language the user writes in."
)

_client: Optional[genai.Client] = None
_store_name: str = ""
_executor = ThreadPoolExecutor(max_workers=4)

# Fallback when display_name has no extension. Avoids mimetypes.guess_extension()
# returning oddities like '.jpe' for 'image/jpeg' on Python <3.13.
_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/json": ".json",
    "text/html": ".html",
    "application/x-yaml": ".yaml",
    "text/yaml": ".yaml",
    "application/zip": ".zip",
    "text/rtf": ".rtf",
    "text/tab-separated-values": ".tsv",
}


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# --- File Search Store Management ---

def _load_store_name_from_gcs() -> str:
    if not GCS_BUCKET:
        return ""
    try:
        client = gcs.Client()
        blob = client.bucket(GCS_BUCKET).blob(STORE_NAME_BLOB)
        if blob.exists():
            return blob.download_as_text().strip()
    except Exception as e:
        print(f"[GCS] Load store name error: {e}")
    return ""


def _save_store_name_to_gcs(name: str) -> None:
    if not GCS_BUCKET:
        return
    try:
        client = gcs.Client()
        client.bucket(GCS_BUCKET).blob(STORE_NAME_BLOB).upload_from_string(name)
    except Exception as e:
        print(f"[GCS] Save store name error: {e}")


def get_or_create_store() -> str:
    """Get existing File Search Store name or create a new one. Cached in memory."""
    global _store_name
    if _store_name:
        return _store_name

    if STORE_NAME_ENV:
        _store_name = STORE_NAME_ENV
        print(f"[Store] Using store from env: {_store_name}")
        return _store_name

    stored = _load_store_name_from_gcs()
    if stored:
        _store_name = stored
        print(f"[Store] Loaded existing store: {_store_name}")
        return _store_name

    client = get_client()
    store = client.file_search_stores.create(
        config={
            "display_name": "telegrambot-multimodal-rag",
            "embedding_model": "models/gemini-embedding-2",
        }
    )
    _store_name = store.name
    _save_store_name_to_gcs(_store_name)
    print(f"[Store] Created new store: {_store_name}")
    return _store_name


# --- Document Management ---

def list_documents() -> list[dict]:
    """Return all documents in the store as a list of {name, display_name, state} dicts."""
    client = get_client()
    store_name = get_or_create_store()
    docs = client.file_search_stores.documents.list(parent=store_name)
    return [
        {
            "name": d.name,
            "display_name": getattr(d, "display_name", d.name.split("/")[-1]),
            "state": str(getattr(d, "state", "")),
        }
        for d in docs
    ]


def delete_document(doc_name: str) -> None:
    """Delete a document from the store by its full resource name."""
    get_client().file_search_stores.documents.delete(name=doc_name)


# --- Upload & Index ---

def _upload_and_index_sync(
    file_bytes: bytes,
    mime_type: str,
    display_name: str,
    user_id: str,
    extra_metadata: Optional[list[dict]] = None,
) -> None:
    """Blocking: upload file to File Search Store and poll until indexed.
    user_id is stored as custom_metadata to enable per-user filtering at query time.
    """
    client = get_client()
    store_name = get_or_create_store()

    # Prefer the extension from display_name. mimetypes.guess_extension() on
    # Python <3.13 returns '.jpe' for 'image/jpeg', which the File Search API
    # rejects with "Upload has already been terminated".
    if "." in display_name:
        suffix = "." + display_name.rsplit(".", 1)[-1].lower()
    else:
        suffix = _MIME_TO_EXT.get(mime_type) or mimetypes.guess_extension(mime_type) or ".bin"

    print(f"[BG Store] uploading display_name={display_name!r} mime={mime_type} "
          f"size={len(file_bytes)} tmp_suffix={suffix}")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        config: dict = {"display_name": display_name}
        if extra_metadata:
            config["custom_metadata"] = extra_metadata

        operation = client.file_search_stores.upload_to_file_search_store(
            file_search_store_name=store_name,
            file=tmp_path,
            config=config,
        )

        # Poll until done (max 5 minutes)
        for _ in range(60):
            if operation.done:
                return
            time.sleep(5)
            operation = client.operations.get(operation)

        if not operation.done:
            raise TimeoutError("Indexing timed out after 5 minutes")
    finally:
        os.unlink(tmp_path)


async def upload_and_index(
    file_bytes: bytes,
    mime_type: str,
    display_name: str,
    user_id: str,
    extra_metadata: Optional[list[dict]] = None,
) -> None:
    """Async wrapper for upload_and_index_sync."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor,
        _upload_and_index_sync,
        file_bytes,
        mime_type,
        display_name,
        user_id,
        extra_metadata,
    )


# --- Query ---

def _extract_sources(response) -> str:
    try:
        chunks = response.candidates[0].grounding_metadata.grounding_chunks
        seen, lines = set(), []
        for chunk in chunks:
            ctx = getattr(chunk, "retrieved_context", None)
            name = getattr(ctx, "title", None) or getattr(ctx, "uri", None)
            if name and name not in seen:
                seen.add(name)
                lines.append(f"• {name}")
        if lines:
            return "\n\n📚 Sources:\n" + "\n".join(lines)
    except Exception:
        pass
    return ""


def _query_text_sync(text: str) -> str:
    store_name = get_or_create_store()
    response = get_client().models.generate_content(
        model=GEN_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[
                types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[store_name],
                    )
                )
            ],
        ),
    )
    answer = (response.text or "").strip()
    sources = _extract_sources(response)
    if not answer and not sources:
        return "I couldn't find relevant information in your documents for that query."
    return answer + sources


async def query_with_text(text: str, user_id: str = "") -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _query_text_sync, text)


async def query_with_image(image_bytes: bytes, mime_type: str, user_id: str = "") -> str:
    """RAG query using an image — describe it, then text-search the store."""
    loop = asyncio.get_event_loop()

    def _describe_sync():
        return get_client().models.generate_content(
            model=GEN_MODEL,
            contents=types.Content(
                parts=[
                    types.Part(inline_data=types.Blob(mime_type=mime_type, data=image_bytes)),
                    types.Part(text="Describe all important content in this image in detail, including text, diagrams, objects, and any data."),
                ]
            ),
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )

    desc_response = await loop.run_in_executor(_executor, _describe_sync)
    description = desc_response.text or ""
    return await query_with_text(
        f"Based on this image description, find relevant information in the database:\n\n{description}",
    )
