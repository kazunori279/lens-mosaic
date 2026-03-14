"""Hosted LensMosaic app for Cloud Run.

This service supports two browser flows from the same deployment:

- Demo mode: static UI + public search API + hosted live API
- Local-live mode: static UI + public search API, while the browser connects
  to a reader's local live API server via a runtime-configured origin
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

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
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import vectorsearch_v1beta
from google.genai import types
from pydantic import BaseModel
from vertexai.vision_models import Image, MultiModalEmbeddingModel

load_dotenv(Path(__file__).parent / ".env", override=True)

APP_NAME = "lens-mosaic-hosted"
STATIC_DIR = Path(__file__).parent / "static"
IMAGE_INTERVAL = 1.0
DEFAULT_VERTEX_AGENT_MODEL = "gemini-live-2.5-flash-native-audio"
DEFAULT_GEMINI_AGENT_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "your-gcp-project-id")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
COLLECTION_ID = os.getenv("VECTOR_COLLECTION_ID", "your-vector-search-collection")
VECTOR_FIELD = os.getenv("VECTOR_FIELD", "embedding")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "multimodalembedding@001")
RANKING_CONFIG = os.getenv(
    "RANKING_CONFIG",
    f"projects/{PROJECT_ID}/locations/global/rankingConfigs/default_ranking_config",
)
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "100"))


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


LIVE_USE_VERTEXAI = _env_flag("GOOGLE_GENAI_USE_VERTEXAI")
LIVE_PROVIDER = "vertex-ai" if LIVE_USE_VERTEXAI else "gemini-api"
DEFAULT_AGENT_MODEL = (
    DEFAULT_VERTEX_AGENT_MODEL if LIVE_USE_VERTEXAI else DEFAULT_GEMINI_AGENT_MODEL
)
RAW_AGENT_MODEL = os.getenv("AGENT_MODEL")
if RAW_AGENT_MODEL in {
    None,
    "",
    DEFAULT_VERTEX_AGENT_MODEL,
    DEFAULT_GEMINI_AGENT_MODEL,
}:
    AGENT_MODEL = DEFAULT_AGENT_MODEL
    AGENT_MODEL_SOURCE = "provider-default"
else:
    AGENT_MODEL = RAW_AGENT_MODEL
    AGENT_MODEL_SOURCE = "env"
LIVE_API_KEY_PRESENT = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

vertexai.init(project=PROJECT_ID, location=LOCATION)
mm_model = MultiModalEmbeddingModel.from_pretrained(EMBEDDING_MODEL)
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


def _generate_multimodal_embedding(
    text: str | None = None,
    image: bytes | None = None,
) -> list[float]:
    """Generate an embedding from text or image input."""
    if text is not None:
        emb = mm_model.get_embeddings(contextual_text=text)
        return emb.text_embedding
    emb = mm_model.get_embeddings(image=Image(image_bytes=image))
    return emb.image_embedding


def _multimodal_search(
    text: str | None = None,
    image: bytes | None = None,
) -> list[dict]:
    """Search the multimodal collection by text or image."""
    embedding = _generate_multimodal_embedding(text=text, image=image)
    request = vectorsearch_v1beta.SearchDataObjectsRequest(
        parent=_collection_path(),
        vector_search=vectorsearch_v1beta.VectorSearch(
            search_field=VECTOR_FIELD,
            vector=vectorsearch_v1beta.DenseVector(values=embedding),
            top_k=SEARCH_TOP_K,
            output_fields=vectorsearch_v1beta.OutputFields(
                data_fields=["name", "description"]
            ),
        ),
    )
    response = search_client.search_data_objects(request)

    results = []
    for result in response:
        obj = result.data_object
        item_id = obj.name.split("/")[-1]
        results.append(
            {
                "id": item_id,
                "name": obj.data.get("name", ""),
                "description": obj.data.get("description", ""),
                "score": result.distance,
            }
        )
    return results


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
    new_image: asyncio.Event = field(default_factory=asyncio.Event)
    search_task: asyncio.Task | None = None

    def start(self) -> None:
        if self.search_task is None or self.search_task.done():
            self.search_task = asyncio.create_task(run_similar_search(self))

    def stop(self) -> None:
        if self.search_task:
            self.search_task.cancel()
            self.search_task = None

    def update_image(self, image: bytes) -> None:
        self.latest_image = image
        self.new_image.set()

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
    session_resumption=types.SessionResumptionConfig(),
)
MAIN_LOOP: asyncio.AbstractEventLoop | None = None


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
        for item in _multimodal_search(text=query):
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
    return _rank_results(" ".join(queries), items)


async def run_similar_search(session: UserSession) -> None:
    while True:
        await session.new_image.wait()
        session.new_image.clear()
        if not session.latest_image:
            continue
        try:
            session.similar = _multimodal_search(image=session.latest_image)
            await session.send({"kind": "similar", "items": session.similar})
        except Exception as exc:
            logger.error("Search error for %s: %s", session.user_id, exc, exc_info=True)


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
    last_image = 0.0
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
        now = asyncio.get_running_loop().time()
        if now - last_image < IMAGE_INTERVAL:
            continue
        last_image = now
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
    logger.info("Live backend provider: %s", LIVE_PROVIDER)
    logger.info("Live backend model: %s", AGENT_MODEL)
    logger.info("Live backend model source: %s", AGENT_MODEL_SOURCE)
    if RAW_AGENT_MODEL and RAW_AGENT_MODEL != AGENT_MODEL:
        logger.info(
            "Normalized AGENT_MODEL=%s to provider default %s",
            RAW_AGENT_MODEL,
            AGENT_MODEL,
        )
    if LIVE_USE_VERTEXAI:
        logger.info("Live backend will use Vertex AI credentials from the environment")
    elif not LIVE_API_KEY_PRESENT:
        logger.warning(
            "Gemini API live backend selected, but GEMINI_API_KEY/GOOGLE_API_KEY is missing"
        )


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
    return _multimodal_search(text=req.text, image=image_bytes)


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
        "live_enabled": True,
        "live_provider": LIVE_PROVIDER,
        "google_genai_use_vertexai": LIVE_USE_VERTEXAI,
        "agent_model": AGENT_MODEL,
        "agent_model_source": AGENT_MODEL_SOURCE,
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
