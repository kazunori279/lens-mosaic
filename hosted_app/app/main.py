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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, perf_counter, sleep

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

load_dotenv(Path(__file__).parent / ".env", override=True)

APP_NAME = "lens-mosaic-hosted"
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_VERTEX_AGENT_MODEL = "gemini-live-2.5-flash-native-audio"
DEFAULT_GEMINI_AGENT_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
RANKING_CONFIG = (
    f"projects/{PROJECT_ID}/locations/global/rankingConfigs/default_ranking_config"
)
SEARCH_TOP_K = 100
MAX_TILE_ITEMS = 64
COLLECTION_ID = os.getenv("LENS_MOSAIC_COLLECTION_ID", "mercari3m-collection-mm2")
DEFAULT_IMAGE_MIME_TYPE = "image/jpeg"
TEXT_QUERY_HYBRID_WEIGHTS = [1.35, 0.65]
IMAGE_QUERY_HYBRID_WEIGHTS = [0.65, 1.35]
EMBEDDING_MAX_RETRIES = 3
EMBEDDING_RETRY_BASE_DELAY_SECONDS = 0.5
EMBEDDING_MAX_RPM_ENV = "LENS_MOSAIC_GEMINI_EMBEDDING_MAX_RPM"
SIMILAR_SEARCH_WORKER_ENV = "LENS_MOSAIC_SIMILAR_SEARCH_WORKERS"


@dataclass(frozen=True)
class CollectionConfig:
    collection_id: str
    embedding_model: str
    text_vector_field: str
    image_vector_field: str
    output_dimensionality: int | None = None


SUPPORTED_COLLECTIONS: dict[str, CollectionConfig] = {
    "mercari3m-collection-mm2": CollectionConfig(
        collection_id="mercari3m-collection-mm2",
        embedding_model="gemini-embedding-2-preview",
        text_vector_field="text_emb",
        image_vector_field="image_emb",
        output_dimensionality=768,
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


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {value!r}") from exc


LIVE_USE_VERTEXAI = _env_flag("GOOGLE_GENAI_USE_VERTEXAI")
LIVE_PROVIDER = "vertex-ai" if LIVE_USE_VERTEXAI else "gemini-api"
AGENT_MODEL = (
    DEFAULT_VERTEX_AGENT_MODEL if LIVE_USE_VERTEXAI else DEFAULT_GEMINI_AGENT_MODEL
)
LIVE_API_KEY_PRESENT = bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
TEST_ENDPOINTS_ENABLED = _env_flag("LENS_MOSAIC_ENABLE_TEST_ENDPOINTS")
EMBEDDING_MAX_REQUESTS_PER_MINUTE = _env_int(EMBEDDING_MAX_RPM_ENV, default=1500)
SIMILAR_SEARCH_WORKER_COUNT = max(1, _env_int(SIMILAR_SEARCH_WORKER_ENV, default=100))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _ignore_normal_live_close(record: logging.LogRecord) -> bool:
    exc = record.exc_info[1] if record.exc_info else None
    return not (
        isinstance(exc, genai.errors.APIError) and exc.code == 1000
    )


logging.getLogger(
    "google_adk.google.adk.flows.llm_flows.base_llm_flow"
).addFilter(_ignore_normal_live_close)


class EmbeddingRateLimitExceeded(RuntimeError):
    """Raised when the app-side Gemini embedding RPM budget has been exhausted."""


@dataclass
class RollingWindowRateLimiter:
    max_requests: int
    window_seconds: float = 60.0
    timestamps: deque[float] = field(default_factory=deque, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self) -> tuple[bool, int]:
        if self.max_requests <= 0:
            return True, 0

        now = monotonic()
        cutoff = now - self.window_seconds
        with self.lock:
            while self.timestamps and self.timestamps[0] <= cutoff:
                self.timestamps.popleft()
            current = len(self.timestamps)
            if current >= self.max_requests:
                return False, current
            self.timestamps.append(now)
            return True, current + 1

    def current_count(self) -> int:
        if self.max_requests <= 0:
            return 0

        now = monotonic()
        cutoff = now - self.window_seconds
        with self.lock:
            while self.timestamps and self.timestamps[0] <= cutoff:
                self.timestamps.popleft()
            return len(self.timestamps)


EMBEDDING_RATE_LIMITER = RollingWindowRateLimiter(
    max_requests=EMBEDDING_MAX_REQUESTS_PER_MINUTE
)

vertexai.init(project=PROJECT_ID, location=LOCATION)
embedding_client = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION,
)
search_client = vectorsearch_v1beta.DataObjectSearchServiceClient()
data_client = vectorsearch_v1beta.DataObjectServiceClient()
rank_client = discoveryengine.RankServiceClient()


class SearchRequest(BaseModel):
    queries: list[str]
    ranking_query: str


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


class FindItemsTestRequest(BaseModel):
    user_id: str
    session_id: str
    queries: list[str]
    ranking_query: str
    publish: bool = True


class SimilarSearchTestRequest(BaseModel):
    user_id: str
    session_id: str
    image_b64: str


class FindItemsTestResponse(BaseModel):
    user_id: str
    session_id: str
    item_ids: list[str]
    item_names: list[str]
    latency_ms: float


def _collection_path() -> str:
    return f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION_ID}"


def _search_result_to_dict(result: vectorsearch_v1beta.SearchResult) -> dict | None:
    obj = result.data_object
    if obj is None:
        return None
    item_id = obj.data_object_id or obj.name.split("/")[-1]
    data = obj.data
    if data is None:
        details = _get_item_details(item_id)
        if details is None:
            logger.warning("Skipping search result with missing data for item %s", item_id)
            return None
        data = details
    return {
        "id": item_id,
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "score": result.distance,
    }


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
    for attempt in range(EMBEDDING_MAX_RETRIES + 1):
        allowed, current_rpm = EMBEDDING_RATE_LIMITER.reserve()
        if not allowed:
            raise EmbeddingRateLimitExceeded(
                "Gemini embedding RPM budget exceeded: "
                f"{current_rpm}/{EMBEDDING_MAX_REQUESTS_PER_MINUTE} requests "
                "in the last 60 seconds"
            )
        try:
            response = embedding_client.models.embed_content(
                model=ACTIVE_COLLECTION.embedding_model,
                contents=contents,
                config=config,
            )
            if not response.embeddings:
                raise RuntimeError("Gemini embedding request returned no embeddings")
            return list(response.embeddings[0].values)
        except genai.errors.APIError as exc:
            if exc.status != "RESOURCE_EXHAUSTED" or attempt >= EMBEDDING_MAX_RETRIES:
                raise
            delay_seconds = EMBEDDING_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "Embedding request hit RESOURCE_EXHAUSTED; retrying in %.1fs "
                "(attempt %d/%d)",
                delay_seconds,
                attempt + 1,
                EMBEDDING_MAX_RETRIES,
            )
            sleep(delay_seconds)

    raise RuntimeError("Embedding retry loop exited unexpectedly")


def _generate_query_embedding(
    text: str | None = None,
    image: bytes | None = None,
) -> tuple[list[float], float]:
    """Generate the Gemini embedding query vector."""
    if text is None and image is None:
        raise ValueError("Either text or image must be provided for embedding")

    started_at = perf_counter()
    embedding = _embed_with_gemini_embedding_2(text=text, image=image)
    embed_ms = (perf_counter() - started_at) * 1000
    return embedding, embed_ms


def _collection_search(
    text: str | None = None,
    image: bytes | None = None,
    rerank: bool = True,
) -> list[dict]:
    """Search the active Gemini Embedding collection by text or image."""
    started_at = perf_counter()
    source = "text" if text is not None else "image"
    results, embed_ms, batch_search_ms, rerank_ms = (
        _hybrid_collection_search(text=text, image=image, rerank=rerank)
    )
    total_ms = (perf_counter() - started_at) * 1000
    logger.info(
        "Search latency: model=%s source=%s rerank=%s embed_ms=%.1f "
        "batch_search_ms=%.1f rerank_ms=%.1f total_ms=%.1f results=%d",
        ACTIVE_COLLECTION.embedding_model,
        source,
        rerank,
        embed_ms,
        batch_search_ms,
        rerank_ms,
        total_ms,
        len(results),
    )
    return results


def _image_similarity_search(image: bytes) -> list[dict]:
    """Search the active collection by image similarity only."""
    started_at = perf_counter()
    results, embed_ms, search_ms = _image_similarity_collection_search(image=image)
    total_ms = (perf_counter() - started_at) * 1000
    logger.info(
        "Search latency: model=%s source=image-similarity rerank=%s embed_ms=%.1f "
        "search_ms=%.1f total_ms=%.1f results=%d",
        ACTIVE_COLLECTION.embedding_model,
        False,
        embed_ms,
        search_ms,
        total_ms,
        len(results),
    )
    return results


def _hybrid_collection_search(
    text: str | None = None,
    image: bytes | None = None,
    rerank: bool = True,
) -> tuple[list[dict], float, float, float]:
    """Search Gemini Embedding 2 collections via VS2 batch search with built-in RRF."""
    embedding, embed_ms = _generate_query_embedding(text=text, image=image)
    weights = TEXT_QUERY_HYBRID_WEIGHTS if text is not None else IMAGE_QUERY_HYBRID_WEIGHTS
    batch_started_at = perf_counter()
    request = vectorsearch_v1beta.BatchSearchDataObjectsRequest(
        parent=_collection_path(),
        searches=[
            vectorsearch_v1beta.Search(
                vector_search=vectorsearch_v1beta.VectorSearch(
                    search_field=ACTIVE_COLLECTION.text_vector_field,
                    vector=vectorsearch_v1beta.DenseVector(values=embedding),
                    top_k=SEARCH_TOP_K,
                    output_fields=vectorsearch_v1beta.OutputFields(
                        data_fields=["name", "description"]
                    ),
                )
            ),
            vectorsearch_v1beta.Search(
                vector_search=vectorsearch_v1beta.VectorSearch(
                    search_field=ACTIVE_COLLECTION.image_vector_field,
                    vector=vectorsearch_v1beta.DenseVector(values=embedding),
                    top_k=SEARCH_TOP_K,
                    output_fields=vectorsearch_v1beta.OutputFields(
                        data_fields=["name", "description"]
                    ),
                )
            ),
        ],
        combine=vectorsearch_v1beta.BatchSearchDataObjectsRequest.CombineResultsOptions(
            ranker=vectorsearch_v1beta.Ranker(
                rrf=vectorsearch_v1beta.ReciprocalRankFusion(
                    weights=weights
                )
            ),
            output_fields=vectorsearch_v1beta.OutputFields(
                data_fields=["name", "description"]
            ),
            top_k=SEARCH_TOP_K,
        ),
    )
    response = search_client.batch_search_data_objects(request)
    batch_search_ms = (perf_counter() - batch_started_at) * 1000
    fused_response = response.results[0].results if response.results else []
    fused_results: list[dict] = []
    for result in fused_response:
        item = _search_result_to_dict(result)
        if item is not None:
            fused_results.append(item)
    if rerank:
        rerank_started_at = perf_counter()
        ranked_results = _rank_results(text or "", fused_results)
        rerank_ms = (perf_counter() - rerank_started_at) * 1000
    else:
        ranked_results = fused_results
        rerank_ms = 0.0
    return ranked_results, embed_ms, batch_search_ms, rerank_ms


def _image_similarity_collection_search(image: bytes) -> tuple[list[dict], float, float]:
    """Search Gemini Embedding 2 collections with the image embedding field only."""
    embedding, embed_ms = _generate_query_embedding(image=image)
    search_started_at = perf_counter()
    request = vectorsearch_v1beta.SearchDataObjectsRequest(
        parent=_collection_path(),
        vector_search=vectorsearch_v1beta.VectorSearch(
            search_field=ACTIVE_COLLECTION.image_vector_field,
            vector=vectorsearch_v1beta.DenseVector(values=embedding),
            top_k=SEARCH_TOP_K,
            output_fields=vectorsearch_v1beta.OutputFields(
                data_fields=["name", "description"]
            ),
        ),
    )
    response = search_client.search_data_objects(request)
    search_ms = (perf_counter() - search_started_at) * 1000
    results: list[dict] = []
    for result in response.results:
        item = _search_result_to_dict(result)
        if item is not None:
            results.append(item)
    return results, embed_ms, search_ms


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
- You can find products using the find_items tool.
- Always respond in the user's language.

## Finding Similar Products
- When the user asks to find items similar to what the camera sees:
  1. Do not ask the user a follow-up question before searching.
  2. Tell the user that you will search for the items similar to them.
  For exmaple, "Looks like it's a KEF speaker. Let me find similar items."
  3. Call find_items with descriptive English text queries and a short
  English ranking_query that describes the items the user wants to see.
- After find_items returns, read the product names to the user,
  simplified to a few words each. For example: "I found a KEF speaker,
  a bookshelf speaker, and a wireless subwoofer. They are now showing on your screen."

## Recommendations
- The user may ask for recommendations based on what the camera sees or their own
  request. Examples: "find a teapot that fits this cup", "find a birthday present
  for my son", "what goes well with this shirt".
- For these requests:
  1. Do not ask the user a follow-up question before searching.
  2. Tell the user that you will search for the items they requested.
  3. Use google_search to research what products would be a good match for the user's request.
  4. From the search results, generate 5 product description queries.
  5. Call find_items with those queries and a short English ranking_query
  that describes the desired items.
- After find_items returns, read the product names to the user,
  simplified to a few words each. For example: "I found a KEF speaker,
  a bookshelf speaker, and a wireless subwoofer. They are now showing on your screen."
""",
)


@dataclass
class SessionState:
    session_id: str
    user_id: str | None = None
    latest_image: bytes | None = None
    similar: list[dict] = field(default_factory=list)
    recommended: list[dict] = field(default_factory=list)
    tile_client: WebSocket | None = None
    image_version: int = 0
    search_enqueued: bool = False
    search_running: bool = False
    state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self) -> None:
        should_enqueue = False
        with self.state_lock:
            if (
                self.latest_image is not None
                and not self.search_running
                and not self.search_enqueued
            ):
                self.search_enqueued = True
                should_enqueue = True
        if should_enqueue:
            SEARCH_REQUEST_QUEUE.put(self.session_id)

    def stop(self) -> None:
        with self.state_lock:
            self.search_enqueued = False

    def update_image(self, image: bytes) -> None:
        should_enqueue = False
        with self.state_lock:
            self.latest_image = image
            self.image_version += 1
            if not self.search_running and not self.search_enqueued:
                self.search_enqueued = True
                should_enqueue = True
        if should_enqueue:
            SEARCH_REQUEST_QUEUE.put(self.session_id)

    def begin_search(self) -> tuple[bytes, int] | None:
        with self.state_lock:
            self.search_enqueued = False
            if self.latest_image is None:
                return None
            self.search_running = True
            return self.latest_image, self.image_version

    def finish_search(self, processed_version: int) -> bool:
        with self.state_lock:
            self.search_running = False
            if self.latest_image is None:
                return False
            if self.image_version == processed_version or self.search_enqueued:
                return False
            self.search_enqueued = True
            return True

    def should_publish_similar(self) -> bool:
        with self.state_lock:
            return self.latest_image is not None

    async def send(self, payload: dict) -> None:
        ws = self.tile_client
        if ws is None:
            return
        try:
            await ws.send_json(payload)
        except Exception:
            if self.tile_client is ws:
                self.tile_client = None

    async def snapshot(self, ws: WebSocket) -> None:
        await ws.send_json(
            {
                "kind": "snapshot",
                "sessionId": self.session_id,
                "userId": self.user_id,
                "similarItems": self.similar,
                "recommendedItems": self.recommended,
            }
        )


SESSION_STATES: dict[str, SessionState] = {}
SESSION_SERVICE = InMemorySessionService()
RUNNER = Runner(app_name=APP_NAME, agent=agent, session_service=SESSION_SERVICE)
RUN_CONFIG = RunConfig(
    streaming_mode=StreamingMode.BIDI,
    response_modalities=["AUDIO"],
    session_resumption=types.SessionResumptionConfig(),
)
MAIN_LOOP: asyncio.AbstractEventLoop | None = None
SEARCH_REQUEST_QUEUE: queue.Queue[str | None] = queue.Queue()
SEARCH_WORKERS: list[threading.Thread] = []


def session_state_for(
    session_id: str, user_id: str | None = None
) -> SessionState:
    state = SESSION_STATES.get(session_id)
    if state is None:
        state = SessionState(session_id=session_id, user_id=user_id)
        SESSION_STATES[session_id] = state
        return state
    if user_id is not None:
        state.user_id = user_id
    return state


def cleanup(session_id: str, session: SessionState) -> None:
    if session.tile_client is not None or session.user_id is not None:
        return
    session.stop()
    SESSION_STATES.pop(session_id, None)
    logger.info("Cleaned up session state for %s", session_id)


def search_text_queries_sync(queries: list[str], ranking_query: str) -> list[dict]:
    seen, items = set(), []
    for query in queries:
        for item in _collection_search(text=query, rerank=False):
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
    return _rank_results(ranking_query.strip(), items)


async def _publish_similar_results(
    session_id: str, processed_version: int, results: list[dict]
) -> None:
    session = SESSION_STATES.get(session_id)
    if session is None or not session.should_publish_similar():
        return
    session.similar = results[:MAX_TILE_ITEMS]
    await session.send(
        {
            "kind": "similar",
            "sessionId": session.session_id,
            "userId": session.user_id,
            "items": session.similar,
        }
    )


async def _publish_recommended_results(session: SessionState) -> None:
    await session.send(
        {
            "kind": "recommended",
            "sessionId": session.session_id,
            "userId": session.user_id,
            "items": session.recommended,
        }
    )


def _search_worker_loop(worker_id: int) -> None:
    while True:
        session_id = SEARCH_REQUEST_QUEUE.get()
        if session_id is None:
            logger.info("Similar search worker %d received shutdown signal", worker_id)
            return

        session = SESSION_STATES.get(session_id)
        if session is None:
            continue

        search_input = session.begin_search()
        if search_input is None:
            continue

        image, processed_version = search_input
        try:
            results = _image_similarity_search(image)
        except EmbeddingRateLimitExceeded as exc:
            results = list(session.similar)
            logger.warning(
                "Similar search worker %d reused %d cached items for %s because %s",
                worker_id,
                len(results),
                session_id,
                exc,
            )
            if MAIN_LOOP is not None:
                asyncio.run_coroutine_threadsafe(
                    _publish_similar_results(session_id, processed_version, results),
                    MAIN_LOOP,
                )
        except Exception as exc:
            logger.error(
                "Similar search worker %d error for %s: %s",
                worker_id,
                session_id,
                exc,
                exc_info=True,
            )
        else:
            if MAIN_LOOP is not None:
                asyncio.run_coroutine_threadsafe(
                    _publish_similar_results(session_id, processed_version, results),
                    MAIN_LOOP,
                )

        if session.finish_search(processed_version):
            SEARCH_REQUEST_QUEUE.put(session_id)


def _ensure_search_workers() -> None:
    global SEARCH_WORKERS
    SEARCH_WORKERS = [worker for worker in SEARCH_WORKERS if worker.is_alive()]
    if len(SEARCH_WORKERS) >= SIMILAR_SEARCH_WORKER_COUNT:
        return
    start_index = len(SEARCH_WORKERS)
    for worker_index in range(start_index, SIMILAR_SEARCH_WORKER_COUNT):
        worker = threading.Thread(
            target=_search_worker_loop,
            args=(worker_index,),
            name=f"lens-mosaic-search-worker-{worker_index}",
            daemon=True,
        )
        worker.start()
        SEARCH_WORKERS.append(worker)
    logger.info(
        "Started %d similar search worker threads",
        len(SEARCH_WORKERS),
    )


def _stop_search_workers() -> None:
    global SEARCH_WORKERS
    if not SEARCH_WORKERS:
        return
    workers = SEARCH_WORKERS
    SEARCH_WORKERS = []
    for _ in workers:
        SEARCH_REQUEST_QUEUE.put(None)
    for worker in workers:
        worker.join(timeout=2.0)
    logger.info("Stopped %d similar search worker threads", len(workers))


def _run_find_items_for_session(
    session_id: str,
    user_id: str | None,
    queries: list[str],
    ranking_query: str,
    publish: bool = True,
) -> tuple[list[dict], float]:
    session = session_state_for(session_id, user_id)
    started_at = perf_counter()
    reused_cached_results = False
    try:
        session.recommended = search_text_queries_sync(queries, ranking_query)[
            :MAX_TILE_ITEMS
        ]
    except EmbeddingRateLimitExceeded as exc:
        reused_cached_results = True
        logger.warning(
            "find_items session_id=%s user_id=%s reused %d cached items because %s",
            session_id,
            user_id,
            len(session.recommended),
            exc,
        )
    latency_ms = (perf_counter() - started_at) * 1000
    if publish and MAIN_LOOP:
        asyncio.run_coroutine_threadsafe(
            _publish_recommended_results(session),
            MAIN_LOOP,
        )
    logger.info(
        "find_items session_id=%s user_id=%s ranking_query=%r queries=%s "
        "items=%d latency_ms=%.1f publish=%s reused_cached=%s",
        session_id,
        user_id,
        ranking_query,
        queries,
        len(session.recommended),
        latency_ms,
        publish,
        reused_cached_results,
    )
    return session.recommended, latency_ms


def find_items(
    queries: list[str], ranking_query: str, tool_context: ToolContext
) -> str:
    """Find shopping items that match one or more product description queries.

    Use this tool when you want to show the user product candidates on screen.
    Provide a list of descriptive English product-search queries. The tool
    searches and publishes the matched items to the UI, then uses ranking_query
    for the final Ranking API rerank across all merged candidates.

    Args:
        queries: One or more descriptive English product-search queries.
        ranking_query: A short English description used for final reranking.
        tool_context: ADK tool context for the current user session.

    Returns:
        A comma-separated string of top matched item names, or "No items found".
    """
    recommended, _ = _run_find_items_for_session(
        session_id=tool_context.session.id,
        user_id=tool_context.session.user_id,
        queries=queries,
        ranking_query=ranking_query,
        publish=True,
    )
    names = [item["name"] for item in recommended[:3]]
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
    ws: WebSocket, session: SessionState, queue: LiveRequestQueue
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
        if payload.get("type") == "text":
            queue.send_content(types.Content(parts=[types.Part(text=payload["text"])]))
            continue
        if payload.get("type") != "image":
            continue

        image = base64.b64decode(payload["data"])
        session.update_image(image)
        should_forward_to_agent = payload.get("forwardToAgent", True)
        if should_forward_to_agent:
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


def is_disconnect_error(exc: Exception) -> bool:
    if isinstance(exc, RuntimeError):
        return "disconnect message has been received" in str(exc)
    if isinstance(exc, genai.errors.APIError):
        return exc.code == 1000
    return False


app = FastAPI(title="LensMosaic Hosted App", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup() -> None:
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    _ensure_search_workers()
    logger.info("Search collection: %s", ACTIVE_COLLECTION.collection_id)
    logger.info("Search embedding model: %s", ACTIVE_COLLECTION.embedding_model)
    logger.info("Embedding max RPM: %d", EMBEDDING_MAX_REQUESTS_PER_MINUTE)
    logger.info("Similar search workers: %d", SIMILAR_SEARCH_WORKER_COUNT)
    logger.info("Live backend provider: %s", LIVE_PROVIDER)
    logger.info("Live backend model: %s", AGENT_MODEL)
    if LIVE_USE_VERTEXAI:
        logger.info("Live backend will use Vertex AI credentials from the environment")
    elif not LIVE_API_KEY_PRESENT:
        logger.warning(
            "Gemini API live backend selected, but GOOGLE_API_KEY is missing"
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    global MAIN_LOOP
    _stop_search_workers()
    MAIN_LOOP = None


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/search", response_model=list[SearchResult])
def search_endpoint(req: SearchRequest):
    """Search with multiple recall queries and a final ranking query rerank."""
    queries = [query.strip() for query in req.queries if query.strip()]
    ranking_query = req.ranking_query.strip()
    if not queries:
        raise HTTPException(
            status_code=400, detail="queries must include at least one non-empty string"
        )
    if not ranking_query:
        raise HTTPException(
            status_code=400, detail="ranking_query must be a non-empty string"
        )
    logger.info("Search request: ranking_query=%r, queries=%s", ranking_query, queries)
    try:
        return search_text_queries_sync(queries, ranking_query)
    except EmbeddingRateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@app.post("/rank", response_model=list[SearchResult])
def rank_endpoint(req: RankRequest):
    """Re-rank search results."""
    results = [result.model_dump() for result in req.results]
    logger.info("Rank request: query=%s, num_results=%d", req.query, len(results))
    return _rank_results(req.query, results)


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
        "embedding_model": ACTIVE_COLLECTION.embedding_model,
        "live_enabled": True,
        "live_provider": LIVE_PROVIDER,
        "google_genai_use_vertexai": LIVE_USE_VERTEXAI,
        "agent_model": AGENT_MODEL,
        "embedding_max_rpm": EMBEDDING_MAX_REQUESTS_PER_MINUTE,
        "embedding_requests_last_minute": EMBEDDING_RATE_LIMITER.current_count(),
        "test_endpoints_enabled": TEST_ENDPOINTS_ENABLED,
    }


@app.post("/test/find_items", response_model=FindItemsTestResponse)
def test_find_items_endpoint(req: FindItemsTestRequest):
    if not TEST_ENDPOINTS_ENABLED:
        raise HTTPException(status_code=404, detail="Test endpoints are disabled")
    queries = [query.strip() for query in req.queries if query.strip()]
    ranking_query = req.ranking_query.strip()
    if not queries:
        raise HTTPException(
            status_code=400, detail="queries must include at least one non-empty string"
        )
    if not ranking_query:
        raise HTTPException(
            status_code=400, detail="ranking_query must be a non-empty string"
        )
    items, latency_ms = _run_find_items_for_session(
        session_id=req.session_id,
        user_id=req.user_id,
        queries=queries,
        ranking_query=ranking_query,
        publish=req.publish,
    )
    return FindItemsTestResponse(
        user_id=req.user_id,
        session_id=req.session_id,
        item_ids=[item["id"] for item in items],
        item_names=[item["name"] for item in items[:3]],
        latency_ms=latency_ms,
    )


@app.post("/test/similar")
def test_similar_endpoint(req: SimilarSearchTestRequest):
    if not TEST_ENDPOINTS_ENABLED:
        raise HTTPException(status_code=404, detail="Test endpoints are disabled")
    try:
        image = base64.b64decode(req.image_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="image_b64 must be valid base64") from exc
    session = session_state_for(req.session_id, req.user_id)
    session.update_image(image)
    return {
        "status": "accepted",
        "user_id": req.user_id,
        "session_id": req.session_id,
    }


@app.websocket("/ws_image_tile/{session_id}")
async def tile_socket(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    session = session_state_for(session_id)
    session.tile_client = ws
    try:
        await session.snapshot(ws)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if session.tile_client is ws:
            session.tile_client = None
        cleanup(session_id, session)


@app.websocket("/ws/{user_id}/{session_id}")
async def live_socket(ws: WebSocket, user_id: str, session_id: str) -> None:
    await ws.accept()
    await ensure_adk_session(user_id, session_id)

    session = session_state_for(session_id, user_id)
    session.start()
    queue = LiveRequestQueue()

    try:
        await asyncio.gather(
            client_to_agent(ws, session, queue),
            agent_to_client(ws, user_id, session_id, queue),
        )
    except WebSocketDisconnect:
        logger.debug("Client disconnected")
    except Exception as exc:
        if is_disconnect_error(exc):
            logger.debug("Client disconnected")
        else:
            logger.error("Streaming error: %s", exc, exc_info=True)
    finally:
        queue.close()
        session.user_id = None
        cleanup(session_id, session)
