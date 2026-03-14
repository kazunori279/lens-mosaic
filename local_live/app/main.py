"""FastAPI server for live audio chat plus camera-based shopping search."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
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
from google.genai import types

load_dotenv(Path(__file__).parent / ".env", override=True)

SEARCH_SERVICE_URL = os.getenv("SEARCH_SERVICE_URL", "http://localhost:8001")

APP_NAME = "lens-mosaic"
STATIC_DIR = Path(__file__).parent / "static"
IMAGE_INTERVAL = 1.0
HTTP_CLIENT: httpx.AsyncClient | None = None

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "server.log", mode="w"),
    ],
)
logger = logging.getLogger(__name__)


async def search_api(
    text: str | None = None, image: bytes | None = None
) -> list[dict]:
    """Search via the search service API."""
    payload = {}
    if text:
        payload["text"] = text
    if image:
        payload["image_base64"] = base64.b64encode(image).decode()
    resp = await HTTP_CLIENT.post(f"{SEARCH_SERVICE_URL}/search", json=payload)
    resp.raise_for_status()
    return resp.json()


async def rank_api(query: str, results: list[dict]) -> list[dict]:
    """Re-rank results via the search service API."""
    payload = {"query": query, "results": results}
    resp = await HTTP_CLIENT.post(f"{SEARCH_SERVICE_URL}/rank", json=payload)
    resp.raise_for_status()
    return resp.json()


async def get_item_api(item_id: str) -> dict | None:
    """Get item details via the search service API."""
    resp = await HTTP_CLIENT.get(f"{SEARCH_SERVICE_URL}/item/{item_id}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


# ADK agent used for live voice chat.
agent = Agent(
    name="mm_agent",
    model=os.getenv("AGENT_MODEL", "gemini-live-2.5-flash-native-audio"),
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


# Per-user state: latest image plus the two tile streams.
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


async def search_text_queries(queries: list[str]) -> list[dict]:
    seen, items = set(), []
    for query in queries:
        for item in await search_api(text=query):
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
    return await rank_api(" ".join(queries), items)


async def run_similar_search(session: UserSession) -> None:
    # Turn camera frames into "similar items" in the background.
    while True:
        await session.new_image.wait()
        session.new_image.clear()
        if not session.latest_image:
            continue
        try:
            session.similar = await search_api(image=session.latest_image)
            await session.send({"kind": "similar", "items": session.similar})
        except Exception as exc:
            logger.error("Search error for %s: %s", session.user_id, exc, exc_info=True)


def find_items(queries: list[str], tool_context: ToolContext) -> str:
    """Find product matches from text queries and publish them to the UI.

    Args:
        queries: English search queries describing the items to find.
        tool_context: ADK tool context for the current user session.

    Returns:
        A short comma-separated summary of the top matching item names.
    """
    session = session_for(tool_context.session.user_id)
    if MAIN_LOOP:
        future = asyncio.run_coroutine_threadsafe(
            search_text_queries(queries), MAIN_LOOP
        )
        session.recommended = future.result()
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
    # Forward mic, text, and camera frames to Gemini Live.
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
    return 'disconnect message has been received' in str(exc)


app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup() -> None:
    global MAIN_LOOP, HTTP_CLIENT
    MAIN_LOOP = asyncio.get_running_loop()
    HTTP_CLIENT = httpx.AsyncClient(timeout=60.0)


@app.on_event("shutdown")
async def shutdown() -> None:
    if HTTP_CLIENT:
        await HTTP_CLIENT.aclose()


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "search_service_url": SEARCH_SERVICE_URL}


@app.get("/api/item/{item_id}")
async def item_details(item_id: str):
    item = await get_item_api(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


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
