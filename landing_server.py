import asyncio
import logging
import os

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("proxy")

app = FastAPI()

STREAMLIT_HTTP = "http://localhost:8501"
STREAMLIT_WS   = "ws://localhost:8501"
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))


# ── Landing page ──────────────────────────────────────────────────────────────

@app.get("/")
async def landing():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


# ── Health check (visit /_health to verify Streamlit is up) ──────────────────

@app.get("/_health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{STREAMLIT_HTTP}/tool/_stcore/health")
            return JSONResponse({"streamlit_up": r.status_code == 200, "status": r.status_code})
    except Exception as e:
        return JSONResponse({"streamlit_up": False, "error": str(e)}, status_code=503)


# ── WebSocket proxy → Streamlit ───────────────────────────────────────────────

@app.websocket("/tool/{path:path}")
async def ws_proxy(ws_client: WebSocket, path: str):
    query  = ws_client.scope.get("query_string", b"").decode()
    target = f"{STREAMLIT_WS}/tool/{path}"
    if query:
        target += f"?{query}"

    log.info("WS incoming: %s", target)

    # Capture subprotocols and origin before accepting
    subprotocol_header = ws_client.headers.get("sec-websocket-protocol", "")
    subprotocols = [s.strip() for s in subprotocol_header.split(",") if s.strip()]
    origin = ws_client.headers.get("origin", "http://localhost")

    await ws_client.accept(subprotocol=subprotocols[0] if subprotocols else None)

    try:
        connect_kwargs: dict = {
            "additional_headers": {"Origin": origin},
        }
        if subprotocols:
            connect_kwargs["subprotocols"] = subprotocols

        async with websockets.connect(target, **connect_kwargs) as ws_server:
            log.info("WS connected to backend: %s", target)

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
                except Exception as e:
                    log.warning("fwd_to_server: %s", e)

            async def fwd_to_client():
                try:
                    async for data in ws_server:
                        if isinstance(data, bytes):
                            await ws_client.send_bytes(data)
                        else:
                            await ws_client.send_text(data)
                except Exception as e:
                    log.warning("fwd_to_client: %s", e)

            await asyncio.gather(fwd_to_server(), fwd_to_client())

    except Exception as e:
        log.error("WS proxy failed for %s: %s", target, e)
        try:
            await ws_client.close(code=1011)
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

    # Strip internal localhost from any redirects
    if "location" in out_headers:
        out_headers["location"] = (
            out_headers["location"]
            .replace("http://localhost:8501", "")
            .replace("https://localhost:8501", "")
        )

    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)
