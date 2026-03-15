"""Hosted LensMosaic app for local and Cloud Run deployments.

This service serves the UI, search APIs, item detail APIs, and live WebSocket
endpoints from the same origin.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Literal

import vertexai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents import Agent
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import ToolContext, google_search
from google import genai
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import vectorsearch_v1beta
from google.genai import types
from pydantic import BaseModel
from vertexai.vision_models import Image, MultiModalEmbeddingModel

load_dotenv(Path(__file__).parent / ".env", override=True)

APP_NAME = "lens-mosaic-hosted"
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_VERTEX_AGENT_MODEL = "gemini-live-2.5-flash-native-audio"
DEFAULT_GEMINI_AGENT_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_LIVE_VOICE_NAME = "Schedar"

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
RANKING_CONFIG = (
    f"projects/{PROJECT_ID}/locations/global/rankingConfigs/default_ranking_config"
)
SEARCH_TOP_K = 100
COLLECTION_ID = os.getenv("LENS_MOSAIC_COLLECTION_ID", "mercari3m-collection-mm2")
DEFAULT_IMAGE_MIME_TYPE = "image/jpeg"
RRF_K = 60.0


EmbeddingBackend = Literal["legacy-multimodal", "gemini-embedding-2"]


@dataclass(frozen=True)
class CollectionConfig:
    collection_id: str
    dataset_id: str
    embedding_backend: EmbeddingBackend
    embedding_model: str
    text_vector_field: str
    image_vector_field: str
    output_dimensionality: int | None = None


SUPPORTED_COLLECTIONS: dict[str, CollectionConfig] = {
    "mercari3m-collection-mm2": CollectionConfig(
        collection_id="mercari3m-collection-mm2",
        dataset_id="mercari3m_mm2",
        embedding_backend="gemini-embedding-2",
        embedding_model="gemini-embedding-2-preview",
        text_vector_field="text_emb",
        image_vector_field="image_emb",
        output_dimensionality=768,
    ),
    "mercari3m-collection-multimodal": CollectionConfig(
        collection_id="mercari3m-collection-multimodal",
        dataset_id="mercari3m_multimodal",
        embedding_backend="legacy-multimodal",
        embedding_model="multimodalembedding@001",
        text_vector_field="embedding",
        image_vector_field="embedding",
    ),
}

try:
    ACTIVE_COLLECTION = SUPPORTED_COLLECTIONS[COLLECTION_ID]
except KeyError as exc:
    supported = ", ".join(sorted(SUPPORTED_COLLECTIONS))
    raise RuntimeError(
        "Unsupported LENS_MOSAIC_COLLECTION_ID "
        f"{COLLECTION_ID!r}. Supported values: {supported}"
    ) from exc


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


LIVE_USE_VERTEXAI = _env_flag("GOOGLE_GENAI_USE_VERTEXAI")
LIVE_PROVIDER = "vertex-ai" if LIVE_USE_VERTEXAI else "gemini-api"
AGENT_MODEL = (
    DEFAULT_VERTEX_AGENT_MODEL if LIVE_USE_VERTEXAI else DEFAULT_GEMINI_AGENT_MODEL
)
LIVE_VOICE_NAME = os.getenv("LENS_MOSAIC_LIVE_VOICE_NAME", DEFAULT_LIVE_VOICE_NAME)
LIVE_API_KEY_PRESENT = bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

vertexai.init(project=PROJECT_ID, location=LOCATION)
legacy_mm_model: MultiModalEmbeddingModel | None = None
embedding_client: genai.Client | None = None
if ACTIVE_COLLECTION.embedding_backend == "legacy-multimodal":
    legacy_mm_model = MultiModalEmbeddingModel.from_pretrained(
        ACTIVE_COLLECTION.embedding_model
    )
else:
    embedding_client = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
    )
search_client = vectorsearch_v1beta.DataObjectSearchServiceClient()
data_client = vectorsearch_v1beta.DataObjectServiceClient()
rank_client = discoveryengine.RankServiceClient()


class SearchRequest(BaseModel):
    text: str | None = None
    image_base64: str | None = None


class SearchResult(BaseModel):
    id: str
    name: str
    description: str
    score: float


class RankRequest(BaseModel):
    query: str
    results: list[SearchResult]


class ItemDetails(BaseModel):
    id: str
    name: str
    description: str
    price: str
    url: str
    img_url: str


def _collection_path() -> str:
    return f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION_ID}"


def _search_result_to_dict(result: vectorsearch_v1beta.SearchResult) -> dict | None:
    obj = result.data_object
    if obj is None:
        return None
    item_id = obj.name.split("/")[-1]
    data = obj.data
    if data is None:
        logger.warning("Skipping search result with missing data for item %s", item_id)
        return None
    return {
        "id": item_id,
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "score": result.distance,
    }


def _embed_with_legacy_multimodal(
    text: str | None = None,
    image: bytes | None = None,
) -> list[float]:
    """Generate a legacy multimodal embedding from text or image input."""
    if legacy_mm_model is None:
        raise RuntimeError("Legacy multimodal embedding model is not configured")
    if text is not None:
        emb = legacy_mm_model.get_embeddings(contextual_text=text)
        return emb.text_embedding
    emb = legacy_mm_model.get_embeddings(image=Image(image_bytes=image))
    return emb.image_embedding


def _embed_with_gemini_embedding_2(
    text: str | None = None,
    image: bytes | None = None,
) -> list[float]:
    """Generate a Gemini Embedding 2 vector from text or image input."""
    if embedding_client is None:
        raise RuntimeError("Gemini embedding client is not configured")

    contents: str | types.Part
    if text is not None:
        contents = text
    else:
        contents = types.Part.from_bytes(data=image, mime_type=DEFAULT_IMAGE_MIME_TYPE)

    config = types.EmbedContentConfig(
        output_dimensionality=ACTIVE_COLLECTION.output_dimensionality
    )
    response = embedding_client.models.embed_content(
        model=ACTIVE_COLLECTION.embedding_model,
        contents=contents,
        config=config,
    )
    if not response.embeddings:
        raise RuntimeError("Gemini embedding request returned no embeddings")
    return list(response.embeddings[0].values)


def _generate_query_embedding(
    text: str | None = None,
    image: bytes | None = None,
) -> tuple[str, list[float], float]:
    """Generate the collection-appropriate query embedding and target field."""
    if text is not None:
        vector_field = ACTIVE_COLLECTION.text_vector_field
    elif image is not None:
        vector_field = ACTIVE_COLLECTION.image_vector_field
    else:
        raise ValueError("Either text or image must be provided for embedding")

    started_at = perf_counter()
    if ACTIVE_COLLECTION.embedding_backend == "legacy-multimodal":
        embedding = _embed_with_legacy_multimodal(text=text, image=image)
    else:
        embedding = _embed_with_gemini_embedding_2(text=text, image=image)
    embed_ms = (perf_counter() - started_at) * 1000
    return vector_field, embedding, embed_ms


def _collection_search(
    text: str | None = None,
    image: bytes | None = None,
) -> list[dict]:
    """Search the active collection by text or image."""
    started_at = perf_counter()
    source = "text" if text is not None else "image"
    if ACTIVE_COLLECTION.embedding_backend == "gemini-embedding-2":
        (
            results,
            embed_ms,
            text_search_ms,
            image_search_ms,
            rrf_ms,
            rerank_ms,
        ) = _hybrid_collection_search(text=text, image=image)
        total_ms = (perf_counter() - started_at) * 1000
        logger.info(
            "Search latency: backend=gemini-embedding-2 source=%s embed_ms=%.1f "
            "text_search_ms=%.1f image_search_ms=%.1f rrf_ms=%.1f rerank_ms=%.1f "
            "total_ms=%.1f results=%d",
            source,
            embed_ms,
            text_search_ms,
            image_search_ms,
            rrf_ms,
            rerank_ms,
            total_ms,
            len(results),
        )
        return results
    results, embed_ms, vector_search_ms = _single_vector_search(text=text, image=image)
    total_ms = (perf_counter() - started_at) * 1000
    logger.info(
        "Search latency: backend=legacy-multimodal source=%s embed_ms=%.1f "
        "vector_search_ms=%.1f total_ms=%.1f results=%d",
        source,
        embed_ms,
        vector_search_ms,
        total_ms,
        len(results),
    )
    return results


def _single_vector_search(
    text: str | None = None,
    image: bytes | None = None,
) -> tuple[list[dict], float, float]:
    """Run a single vector search against the active collection."""
    vector_field, embedding, embed_ms = _generate_query_embedding(text=text, image=image)
    search_started_at = perf_counter()
    request = vectorsearch_v1beta.SearchDataObjectsRequest(
        parent=_collection_path(),
        vector_search=vectorsearch_v1beta.VectorSearch(
            search_field=vector_field,
            vector=vectorsearch_v1beta.DenseVector(values=embedding),
            top_k=SEARCH_TOP_K,
            output_fields=vectorsearch_v1beta.OutputFields(
                data_fields=["name", "description"]
            ),
        ),
    )
    response = search_client.search_data_objects(request)
    items: list[dict] = []
    for result in response:
        item = _search_result_to_dict(result)
        if item is not None:
            items.append(item)
    vector_search_ms = (perf_counter() - search_started_at) * 1000
    return items, embed_ms, vector_search_ms


def _vector_search_by_field(
    vector_field: str,
    embedding: list[float],
) -> tuple[list[dict], float]:
    """Run one vector search against a specific field and keep inline data only."""
    started_at = perf_counter()
    request = vectorsearch_v1beta.SearchDataObjectsRequest(
        parent=_collection_path(),
        vector_search=vectorsearch_v1beta.VectorSearch(
            search_field=vector_field,
            vector=vectorsearch_v1beta.DenseVector(values=embedding),
            top_k=SEARCH_TOP_K,
            output_fields=vectorsearch_v1beta.OutputFields(
                data_fields=["name", "description"]
            ),
        ),
    )
    response = search_client.search_data_objects(request)
    items: list[dict] = []
    for result in response:
        item = _search_result_to_dict(result)
        if item is not None:
            items.append(item)
    return items, (perf_counter() - started_at) * 1000


def _rrf_fuse_results(result_sets: list[list[dict]]) -> list[dict]:
    """Fuse ranked result lists with Reciprocal Rank Fusion."""
    fused: dict[str, dict] = {}
    for result_set in result_sets:
        for rank, item in enumerate(result_set, start=1):
            existing = fused.get(item["id"])
            rrf_score = 1.0 / (RRF_K + rank)
            if existing is None:
                fused[item["id"]] = {
                    "id": item["id"],
                    "name": item["name"],
                    "description": item["description"],
                    "score": rrf_score,
                }
                continue
            existing["score"] += rrf_score
            if not existing["name"] and item["name"]:
                existing["name"] = item["name"]
            if not existing["description"] and item["description"]:
                existing["description"] = item["description"]

    return sorted(fused.values(), key=lambda item: item["score"], reverse=True)[
        :SEARCH_TOP_K
    ]


def _hybrid_collection_search(
    text: str | None = None,
    image: bytes | None = None,
) -> tuple[list[dict], float, float, float, float, float]:
    """Search Gemini Embedding 2 collections across text and image vectors via RRF."""
    _, embedding, embed_ms = _generate_query_embedding(text=text, image=image)
    text_results, text_search_ms = _vector_search_by_field(
        ACTIVE_COLLECTION.text_vector_field,
        embedding,
    )
    image_results, image_search_ms = _vector_search_by_field(
        ACTIVE_COLLECTION.image_vector_field,
        embedding,
    )
    rrf_started_at = perf_counter()
    fused_results = _rrf_fuse_results([text_results, image_results])
    rrf_ms = (perf_counter() - rrf_started_at) * 1000
    rerank_started_at = perf_counter()
    ranked_results = _rank_results(text or "", fused_results)
    rerank_ms = (perf_counter() - rerank_started_at) * 1000
    return (
        ranked_results,
        embed_ms,
        text_search_ms,
        image_search_ms,
        rrf_ms,
        rerank_ms,
    )


def _rank_results(query: str, results: list[dict]) -> list[dict]:
    """Re-rank search results using the Vertex AI Ranking API."""
    if not results or not query:
        return results

    records = [
        discoveryengine.RankingRecord(
            id=item["id"],
            title=item["name"],
            content=item.get("description", ""),
        )
        for item in results
    ]
    request = discoveryengine.RankRequest(
        ranking_config=RANKING_CONFIG,
        query=query,
        records=records,
        top_n=len(records),
    )
    response = rank_client.rank(request=request)

    ranked_by_id = {record.id: record.score for record in response.records}
    for item in results:
        item["score"] = ranked_by_id.get(item["id"], 0.0)
    results.sort(key=lambda item: item["score"], reverse=True)
    return results


def _get_item_details(item_id: str) -> dict | None:
    """Fetch item details from the collection by ID."""
    name = f"{_collection_path()}/dataObjects/{item_id}"
    try:
        obj = data_client.get_data_object(
            vectorsearch_v1beta.GetDataObjectRequest(name=name)
        )
    except Exception:
        return None

    return {
        "id": item_id,
        "name": obj.data.get("name", ""),
        "description": obj.data.get("description", ""),
        "price": obj.data.get("price", ""),
        "url": obj.data.get("url", ""),
        "img_url": obj.data.get("img_url", ""),
    }


agent = Agent(
    name="mm_agent",
    model=AGENT_MODEL,
    tools=[google_search],
    instruction="""\
You are a helpful AI shopping assistant.

## Capabilities
- You can see images from the user's camera and hear their voice.
- You can search the web when needed.
- You can find products using the find_items tool.

## Finding Similar Products
- When the user asks to find items similar to what the camera sees:
  1. Tell the user that you will search for the items similar to them.
  For exmaple, "Looks like it's a KEF speaker. Let me find similar items."
  2. Call find_items with descriptive English text queries.
- After find_items returns, read the product names to the user,
  simplified to a few words each. For example: "I found a KEF speaker,
  a bookshelf speaker, and a wireless subwoofer. They are now showing on your screen."

## Recommendations
- The user may ask for recommendations based on what the camera sees or their own
  request. Examples: "find a teapot that fits this cup", "find a birthday present
  for my son", "what goes well with this shirt".
- For these requests:
  1. Tell the user that you will search for the items they requested.
  2. Use google_search to research what products would be a good match for the user's request.
  3. From the search results, generate a few specific product description queries.
  3. Call find_items with those queries.

## Style
- Always respond in the user's language.
- Respond naturally and helpfully.
""",
)


@dataclass
class UserSession:
    user_id: str
    latest_image: bytes | None = None
    similar: list[dict] = field(default_factory=list)
    recommended: list[dict] = field(default_factory=list)
    tile_clients: set[WebSocket] = field(default_factory=set)
    similar_search_enabled: bool = True
    agent_vision_enabled: bool = False
    image_version: int = 0
    search_enqueued: bool = False
    search_running: bool = False
    state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self) -> None:
        should_enqueue = False
        with self.state_lock:
            if (
                self.similar_search_enabled
                and self.latest_image is not None
                and not self.search_running
                and not self.search_enqueued
            ):
                self.search_enqueued = True
                should_enqueue = True
        if should_enqueue:
            SEARCH_REQUEST_QUEUE.put(self.user_id)

    def stop(self) -> None:
        with self.state_lock:
            self.search_enqueued = False

    async def set_camera_modes(
        self, *, similar_search_enabled: bool, agent_vision_enabled: bool
    ) -> None:
        should_enqueue = False
        send_empty_similar = False
        with self.state_lock:
            search_changed = self.similar_search_enabled != similar_search_enabled
            vision_changed = self.agent_vision_enabled != agent_vision_enabled
            if not search_changed and not vision_changed:
                return

            self.similar_search_enabled = similar_search_enabled
            self.agent_vision_enabled = agent_vision_enabled

            if similar_search_enabled:
                if (
                    self.latest_image is not None
                    and not self.search_running
                    and not self.search_enqueued
                ):
                    self.search_enqueued = True
                    should_enqueue = True
            else:
                self.search_enqueued = False
                self.similar = []
                send_empty_similar = True
        logger.info(
            "Camera modes for %s search=%s vision=%s",
            self.user_id,
            similar_search_enabled,
            agent_vision_enabled,
        )
        if should_enqueue:
            SEARCH_REQUEST_QUEUE.put(self.user_id)
        if not send_empty_similar:
            return

        await self.send({"kind": "similar", "items": []})

    def update_image(self, image: bytes) -> None:
        should_enqueue = False
        with self.state_lock:
            self.latest_image = image
            self.image_version += 1
            if (
                self.similar_search_enabled
                and not self.search_running
                and not self.search_enqueued
            ):
                self.search_enqueued = True
                should_enqueue = True
        if should_enqueue:
            SEARCH_REQUEST_QUEUE.put(self.user_id)

    def begin_search(self) -> tuple[bytes, int] | None:
        with self.state_lock:
            self.search_enqueued = False
            if not self.similar_search_enabled or self.latest_image is None:
                return None
            self.search_running = True
            return self.latest_image, self.image_version

    def finish_search(self, processed_version: int) -> bool:
        with self.state_lock:
            self.search_running = False
            if not self.similar_search_enabled or self.latest_image is None:
                return False
            if self.image_version == processed_version or self.search_enqueued:
                return False
            self.search_enqueued = True
            return True

    def should_publish_similar(self) -> bool:
        with self.state_lock:
            return self.similar_search_enabled and self.latest_image is not None

    def should_forward_vision(self) -> bool:
        with self.state_lock:
            return self.agent_vision_enabled

    async def send(self, payload: dict) -> None:
        dead = set()
        for ws in self.tile_clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        self.tile_clients -= dead

    async def snapshot(self, ws: WebSocket) -> None:
        await ws.send_json(
            {
                "kind": "snapshot",
                "similarItems": self.similar,
                "recommendedItems": self.recommended,
            }
        )


SESSIONS: dict[str, UserSession] = {}
SESSION_SERVICE = InMemorySessionService()
RUNNER = Runner(app_name=APP_NAME, agent=agent, session_service=SESSION_SERVICE)
RUN_CONFIG = RunConfig(
    streaming_mode=StreamingMode.BIDI,
    response_modalities=["AUDIO"],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name=LIVE_VOICE_NAME,
            )
        )
    ),
    session_resumption=types.SessionResumptionConfig(),
)
MAIN_LOOP: asyncio.AbstractEventLoop | None = None
SEARCH_REQUEST_QUEUE: queue.Queue[str | None] = queue.Queue()
SEARCH_WORKER: threading.Thread | None = None


def session_for(user_id: str) -> UserSession:
    if user_id not in SESSIONS:
        SESSIONS[user_id] = UserSession(user_id)
    return SESSIONS[user_id]


def cleanup(user_id: str, session: UserSession) -> None:
    if session.tile_clients:
        return
    session.stop()
    SESSIONS.pop(user_id, None)
    logger.info("Cleaned up session for %s", user_id)


def search_text_queries_sync(queries: list[str]) -> list[dict]:
    seen, items = set(), []
    for query in queries:
        for item in _collection_search(text=query):
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
    return _rank_results(" ".join(queries), items)


async def _publish_similar_results(
    user_id: str, processed_version: int, results: list[dict]
) -> None:
    session = SESSIONS.get(user_id)
    if session is None or not session.should_publish_similar():
        return
    session.similar = results
    await session.send({"kind": "similar", "items": results})


def _search_worker_loop() -> None:
    while True:
        user_id = SEARCH_REQUEST_QUEUE.get()
        if user_id is None:
            return

        session = SESSIONS.get(user_id)
        if session is None:
            continue

        search_input = session.begin_search()
        if search_input is None:
            continue

        image, processed_version = search_input
        try:
            results = _collection_search(image=image)
        except Exception as exc:
            logger.error("Search error for %s: %s", user_id, exc, exc_info=True)
        else:
            if MAIN_LOOP is not None:
                asyncio.run_coroutine_threadsafe(
                    _publish_similar_results(user_id, processed_version, results),
                    MAIN_LOOP,
                )

        if session.finish_search(processed_version):
            SEARCH_REQUEST_QUEUE.put(user_id)


def _ensure_search_worker() -> None:
    global SEARCH_WORKER
    if SEARCH_WORKER is not None and SEARCH_WORKER.is_alive():
        return
    SEARCH_WORKER = threading.Thread(
        target=_search_worker_loop,
        name="lens-mosaic-search-worker",
        daemon=True,
    )
    SEARCH_WORKER.start()
    logger.info("Started image search worker thread")


def _stop_search_worker() -> None:
    global SEARCH_WORKER
    if SEARCH_WORKER is None:
        return
    SEARCH_REQUEST_QUEUE.put(None)
    SEARCH_WORKER.join(timeout=2.0)
    SEARCH_WORKER = None
    logger.info("Stopped image search worker thread")


def find_items(queries: list[str], tool_context: ToolContext) -> str:
    """Find product matches from text queries and publish them to the UI."""
    session = session_for(tool_context.session.user_id)
    session.recommended = search_text_queries_sync(queries)
    if MAIN_LOOP:
        asyncio.run_coroutine_threadsafe(
            session.send({"kind": "recommended", "items": session.recommended}),
            MAIN_LOOP,
        )
    names = [item["name"] for item in session.recommended[:3]]
    logger.info("find_items(%s) -> %s items", queries, len(session.recommended))
    return ", ".join(names) if names else "No items found"


agent.tools.append(find_items)


async def ensure_adk_session(user_id: str, session_id: str) -> None:
    if not await SESSION_SERVICE.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    ):
        await SESSION_SERVICE.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )


async def client_to_agent(
    ws: WebSocket, session: UserSession, queue: LiveRequestQueue
) -> None:
    while True:
        message = await ws.receive()
        if "bytes" in message:
            queue.send_realtime(
                types.Blob(mime_type="audio/pcm;rate=16000", data=message["bytes"])
            )
            continue
        if "text" not in message:
            continue

        payload = json.loads(message["text"])
        if payload.get("type") == "config":
            similar_search_enabled = payload.get("similarSearchEnabled")
            agent_vision_enabled = payload.get("agentVisionEnabled")
            if isinstance(similar_search_enabled, bool) and isinstance(
                agent_vision_enabled, bool
            ):
                await session.set_camera_modes(
                    similar_search_enabled=similar_search_enabled,
                    agent_vision_enabled=agent_vision_enabled,
                )
            continue
        if payload.get("type") == "text":
            queue.send_content(types.Content(parts=[types.Part(text=payload["text"])]))
            continue
        if payload.get("type") != "image":
            continue
        if not session.similar_search_enabled and not session.should_forward_vision():
            continue

        image = base64.b64decode(payload["data"])
        session.update_image(image)
        should_forward_to_agent = payload.get("forwardToAgent", True)
        if session.should_forward_vision() and should_forward_to_agent:
            queue.send_realtime(
                types.Blob(mime_type=payload.get("mimeType", "image/jpeg"), data=image)
            )


async def agent_to_client(
    ws: WebSocket, user_id: str, session_id: str, queue: LiveRequestQueue
) -> None:
    async for event in RUNNER.run_live(
        user_id=user_id,
        session_id=session_id,
        live_request_queue=queue,
        run_config=RUN_CONFIG,
    ):
        await ws.send_text(event.model_dump_json(exclude_none=True, by_alias=True))


def is_disconnect_error(exc: RuntimeError) -> bool:
    return "disconnect message has been received" in str(exc)


app = FastAPI(title="LensMosaic Hosted App", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup() -> None:
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    _ensure_search_worker()
    logger.info("Search collection: %s", ACTIVE_COLLECTION.collection_id)
    logger.info("Search dataset: %s", ACTIVE_COLLECTION.dataset_id)
    logger.info("Search embedding backend: %s", ACTIVE_COLLECTION.embedding_backend)
    logger.info("Search embedding model: %s", ACTIVE_COLLECTION.embedding_model)
    logger.info("Live backend provider: %s", LIVE_PROVIDER)
    logger.info("Live backend model: %s", AGENT_MODEL)
    logger.info("Live voice: %s", LIVE_VOICE_NAME)
    if LIVE_USE_VERTEXAI:
        logger.info("Live backend will use Vertex AI credentials from the environment")
    elif not LIVE_API_KEY_PRESENT:
        logger.warning(
            "Gemini API live backend selected, but GOOGLE_API_KEY is missing"
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    global MAIN_LOOP
    _stop_search_worker()
    MAIN_LOOP = None


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/search", response_model=list[SearchResult])
def search_endpoint(req: SearchRequest):
    """Search by text or image."""
    if req.text is None and req.image_base64 is None:
        raise HTTPException(
            status_code=400, detail="Either text or image_base64 must be provided"
        )

    image_bytes = None
    if req.image_base64:
        image_bytes = base64.b64decode(req.image_base64)

    logger.info("Search request: text=%s, has_image=%s", req.text, bool(image_bytes))
    return _collection_search(text=req.text, image=image_bytes)


@app.post("/rank", response_model=list[SearchResult])
def rank_endpoint(req: RankRequest):
    """Re-rank search results."""
    results = [result.model_dump() for result in req.results]
    logger.info("Rank request: query=%s, num_results=%d", req.query, len(results))
    return _rank_results(req.query, results)


@app.get("/item/{item_id}", response_model=ItemDetails)
def get_item(item_id: str):
    """Get item details by ID."""
    logger.info("Item request: item_id=%s", item_id)
    item = _get_item_details(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.get("/api/item/{item_id}", response_model=ItemDetails)
def get_item_for_ui(item_id: str):
    return get_item(item_id)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "project_id": PROJECT_ID,
        "collection_id": COLLECTION_ID,
        "dataset_id": ACTIVE_COLLECTION.dataset_id,
        "embedding_backend": ACTIVE_COLLECTION.embedding_backend,
        "embedding_model": ACTIVE_COLLECTION.embedding_model,
        "live_enabled": True,
        "live_provider": LIVE_PROVIDER,
        "google_genai_use_vertexai": LIVE_USE_VERTEXAI,
        "agent_model": AGENT_MODEL,
        "live_voice": LIVE_VOICE_NAME,
    }


@app.websocket("/ws_image_tile/{user_id}")
async def tile_socket(ws: WebSocket, user_id: str) -> None:
    await ws.accept()
    session = session_for(user_id)
    session.tile_clients.add(ws)
    try:
        await session.snapshot(ws)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        session.tile_clients.discard(ws)
        cleanup(user_id, session)


@app.websocket("/ws/{user_id}/{session_id}")
async def live_socket(ws: WebSocket, user_id: str, session_id: str) -> None:
    await ws.accept()
    await ensure_adk_session(user_id, session_id)

    session = session_for(user_id)
    session.start()
    queue = LiveRequestQueue()

    try:
        await asyncio.gather(
            client_to_agent(ws, session, queue),
            agent_to_client(ws, user_id, session_id, queue),
        )
    except WebSocketDisconnect:
        logger.debug("Client disconnected")
    except RuntimeError as exc:
        if is_disconnect_error(exc):
            logger.debug("Client disconnected")
        else:
            logger.error("Streaming error: %s", exc, exc_info=True)
    except Exception as exc:
        logger.error("Streaming error: %s", exc, exc_info=True)
    finally:
        queue.close()
        cleanup(user_id, session)
