import asyncio
import os

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, Response

app = FastAPI()

STREAMLIT_HTTP = "http://localhost:8501"
STREAMLIT_WS   = "ws://localhost:8501"
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))


# ── Landing page ──────────────────────────────────────────────────────────────

@app.get("/")
async def landing():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


# ── WebSocket proxy → Streamlit ───────────────────────────────────────────────

@app.websocket("/tool/{path:path}")
async def ws_proxy(ws_client: WebSocket, path: str):
    await ws_client.accept()

    query  = ws_client.scope.get("query_string", b"").decode()
    target = f"{STREAMLIT_WS}/tool/{path}"
    if query:
        target += f"?{query}"

    try:
        async with websockets.connect(target) as ws_server:

            async def fwd_to_server():
                try:
                    while True:
                        msg = await ws_client.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes"):
                            await ws_server.send(msg["bytes"])
                        elif msg.get("text"):
                            await ws_server.send(msg["text"])
                except Exception:
                    pass

            async def fwd_to_client():
                try:
                    async for data in ws_server:
                        if isinstance(data, bytes):
                            await ws_client.send_bytes(data)
                        else:
                            await ws_client.send_text(data)
                except Exception:
                    pass

            await asyncio.gather(fwd_to_server(), fwd_to_client())
    except Exception:
        pass


# ── HTTP proxy → Streamlit ────────────────────────────────────────────────────

_SKIP_REQ  = {"host", "connection", "upgrade", "transfer-encoding", "content-encoding"}
_SKIP_RESP = {"transfer-encoding", "connection", "content-encoding"}


@app.api_route(
    "/tool{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
)
async def http_proxy(path: str, request: Request):
    url = f"{STREAMLIT_HTTP}/tool{path}"

    # Tell Streamlit its real public host so it doesn't embed localhost in redirects
    public_host = request.headers.get("host", "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQ}
    headers["X-Forwarded-Host"]  = public_host
    headers["X-Forwarded-Proto"] = "https"
    headers["X-Forwarded-For"]   = request.client.host if request.client else ""

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=await request.body(),
            params=dict(request.query_params),
            follow_redirects=False,
        )

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _SKIP_RESP}

    # Rewrite any localhost:8501 in redirect Location headers so the browser
    # never sees the internal address
    if "location" in out_headers:
        out_headers["location"] = out_headers["location"].replace(
            "http://localhost:8501", ""
        ).replace("https://localhost:8501", "")

    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)
