"""Minimal LensMosaic live server for blog readers."""

from __future__ import annotations

import asyncio, base64, json, os, ssl
import urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import certifi
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import ToolContext
from google.genai import types

import vertexai

# App configuration and external service setup.
APP_NAME = "lens-mosaic-blog-sample"
AGENT_MODEL = "gemini-live-2.5-flash-native-audio"
MAX_TILE_ITEMS = 64
ENV_FILE = Path(__file__).with_name(".env")
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=True)
# The blog sample reuses the deployed hosted app for UI and catalog APIs.
HOSTED_URL = os.getenv("LENS_MOSAIC_HOSTED_URL", "").rstrip("/")

vertexai.init(
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
    location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
)


# Per-live-session state shared across websocket handlers.
@dataclass
class SessionState:
    session_id: str
    user_id: str | None = None
    recommended: list[dict] = field(default_factory=list)
    tile_clients: set[WebSocket] = field(default_factory=set)
    live_clients: int = 0


SESSION_STATES: dict[str, SessionState] = {}
SESSION_SERVICE = InMemorySessionService()
MAIN_LOOP: asyncio.AbstractEventLoop | None = None
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


# Session lifecycle helpers.
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


def cleanup_session(session_id: str) -> None:
    session = SESSION_STATES.get(session_id)
    if session and session.live_clients == 0 and not session.tile_clients:
        SESSION_STATES.pop(session_id, None)


# Upstream proxy helpers for the hosted UI and APIs.
def fetch_upstream(
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    query: list[tuple[str, str]] | None = None,
) -> tuple[int, str, bytes]:
    # Keep local sample routes thin by forwarding most HTTP work upstream.
    if not HOSTED_URL:
        raise RuntimeError("Set LENS_MOSAIC_HOSTED_URL to your deployed hosted app URL")
    url = f"{HOSTED_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = {"Content-Type": content_type} if content_type else {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


async def proxy_upstream(
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    query: list[tuple[str, str]] | None = None,
) -> Response:
    status, media_type, data = await asyncio.to_thread(
        fetch_upstream,
        path,
        method=method,
        body=body,
        content_type=content_type,
        query=query,
    )
    return Response(content=data, status_code=status, media_type=media_type)


# Tile update helpers used by the local recommendation tool.
async def broadcast_recommended(session_id: str, items: list[dict]) -> None:
    session = SESSION_STATES.get(session_id)
    if not session:
        return
    # Drop any tile sockets that disappeared between broadcasts.
    dead = set()
    for ws in session.tile_clients:
        try:
            await ws.send_json({"kind": "recommended", "items": items})
        except Exception:
            dead.add(ws)
    session.tile_clients -= dead


# Tool and agent definitions for the local live assistant.
def find_items(queries: list[str], user_request: str, tool_context: ToolContext) -> str:
    """Find shopping items that match one or more product description queries.

    Use this tool when you want to show the user product candidates on screen.
    Provide a list of descriptive English product-search queries The tool searches and
    publishes the matched items to the UI, and uses the user_request for the final 
    Ranking API rerank across all merged candidates. 

    Args:
        queries: One or more descriptive English product-search queries.
        user_request: The user's intent for finding items.
        tool_context: ADK tool context for the current user session.

    Returns:
        A comma-separated string of top matched item names, or "No items found".
    """
    # Merge search hits across a few descriptive queries before updating the tiles.
    seen: dict[str, dict] = {}
    for query in queries[:4]:
        status, _, body = fetch_upstream(
            "/search",
            method="POST",
            body=json.dumps({"text": query}).encode(),
            content_type="application/json",
        )
        if status >= 400:
            continue
        for item in json.loads(body.decode()):
            seen.setdefault(item["id"], item)
    items = sorted(seen.values(), key=lambda item: item.get("score", 0.0), reverse=True)
    session = session_state_for(tool_context.session.id, tool_context.session.user_id)
    session.recommended = items[:MAX_TILE_ITEMS]
    if MAIN_LOOP:
        # Tool calls run off the main loop, so schedule the tile push back onto it.
        asyncio.run_coroutine_threadsafe(
            broadcast_recommended(session.session_id, session.recommended),
            MAIN_LOOP,
        )
    names = [item.get("name", "") for item in session.recommended[:3] if item.get("name")]
    return ", ".join(names) if names else "No items found"


agent = Agent(
    name="blog_sample_agent",
    model=AGENT_MODEL,
    tools=[find_items],
    instruction="""
        You are a helpful AI shopping assistant. Always respond in the user's language.
        Capabilities:
        - You can hear the user's voice, read their text, and see camera images.
        - Use find_items to show product candidates on screen.
        Similar-item requests:
        - Do not ask a follow-up question before searching.
        - Briefly say you will search for similar items.
        - Call find_items with a couple of descriptive English queries.
        - Pass the user intent as user_request.
        Recommendations or matching products:
        - Do not ask a follow-up question before searching.
        - Infer the shopping goal from the user intent and camera context.
        - Call find_items with 5 descriptive English queries.
        - Pass the user intent as user_request.
        After find_items returns:
        - Mention a few item names in simple language.""",
)
RUNNER = Runner(app_name=APP_NAME, agent=agent, session_service=SESSION_SERVICE)
RUN_CONFIG = RunConfig(
    streaming_mode=StreamingMode.BIDI,
    response_modalities=["AUDIO"],
    session_resumption=types.SessionResumptionConfig(),
)
app = FastAPI(title="LensMosaic Blog Sample", version="0.1.0")


# Live websocket communication between the browser and ADK.
async def ensure_adk_session(user_id: str, session_id: str) -> None:
    if not await SESSION_SERVICE.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id):
        await SESSION_SERVICE.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)


async def client_to_agent(ws: WebSocket, queue: LiveRequestQueue) -> None:
    while True:
        message = await ws.receive()
        if message.get("bytes") is not None:
            # Raw websocket bytes are microphone audio chunks from the browser.
            queue.send_realtime(
                types.Blob(mime_type="audio/pcm;rate=16000", data=message["bytes"])
            )
            continue
        if message.get("text") is None:
            continue
        payload = json.loads(message["text"])
        if payload.get("type") == "text":
            queue.send_content(types.Content(parts=[types.Part(text=payload["text"])]))
            continue
        if payload.get("type") != "image":
            continue
        if payload.get("forwardToAgent", True):
            # Camera frames are base64 encoded on the client before they reach us.
            queue.send_realtime(
                types.Blob(
                    mime_type=payload.get("mimeType", "image/jpeg"),
                    data=base64.b64decode(payload["data"]),
                )
            )


async def agent_to_client(user_id: str, session_id: str, ws: WebSocket, queue: LiveRequestQueue) -> None:
    # Stream ADK events straight back to the browser without reshaping them.
    async for event in RUNNER.run_live(
        user_id=user_id,
        session_id=session_id,
        live_request_queue=queue,
        run_config=RUN_CONFIG,
    ):
        await ws.send_text(event.model_dump_json(exclude_none=True, by_alias=True))


def is_disconnect_error(exc: RuntimeError) -> bool:
    return 'disconnect message has been received' in str(exc)


# FastAPI app lifecycle and proxied HTTP routes.
@app.on_event("startup")
async def startup() -> None:
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()


@app.get("/")
async def root() -> Response:
    return await proxy_upstream("/")


@app.get("/static/{path:path}")
async def static_proxy(path: str, request: Request) -> Response:
    return await proxy_upstream(f"/static/{path}", query=list(request.query_params.multi_items()))


@app.post("/search")
async def search_proxy(request: Request) -> Response:
    body = await request.body()
    return await proxy_upstream("/search", method="POST", body=body, content_type=request.headers.get("content-type"))


@app.get("/api/item/{item_id}")
async def item_proxy(item_id: str) -> Response:
    return await proxy_upstream(f"/api/item/{item_id}")


# FastAPI websocket endpoints for recommendation tiles and live chat.
@app.websocket("/ws_image_tile/{session_id}")
async def tile_socket(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    session = session_state_for(session_id)
    session.tile_clients.add(ws)
    try:
        # New tile clients receive the latest recommendation snapshot immediately.
        await ws.send_json({"kind": "snapshot", "similarItems": [], "recommendedItems": session.recommended})
        while True:
            await ws.receive()
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        if not is_disconnect_error(exc):
            raise
    finally:
        session.tile_clients.discard(ws)
        cleanup_session(session_id)


@app.websocket("/ws/{user_id}/{session_id}")
async def live_socket(ws: WebSocket, user_id: str, session_id: str) -> None:
    await ws.accept()
    await ensure_adk_session(user_id, session_id)
    session = session_state_for(session_id, user_id)
    session.live_clients += 1
    # One queue feeds the ADK runner while both websocket tasks stay in sync.
    queue = LiveRequestQueue()
    try:
        await asyncio.gather(
            client_to_agent(ws, queue),
            agent_to_client(user_id, session_id, ws, queue),
        )
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        if not is_disconnect_error(exc):
            raise
    finally:
        queue.close()
        session.live_clients -= 1
        cleanup_session(session_id)
