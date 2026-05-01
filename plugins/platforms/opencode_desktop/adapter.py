"""
OpenCode Desktop Platform Adapter for Hermes Agent.

Runs a WebSocket server that the OpenCode Desktop plugin connects to.
Provides bidirectional, real-time communication between OpenCode Desktop
and the Hermes Agent gateway.

Protocol (JSON over WebSocket):

    OpenCode -> Hermes:
        {"type": "message", "text": "Hello", "session_id": "opt"}
        {"type": "command", "command": "/model", "session_id": "opt"}
        {"type": "interrupt", "session_id": "..."}

    Hermes -> OpenCode:
        {"type": "delta", "text": "chunk", "session_id": "..."}
        {"type": "tool_start", "tool": "...", "label": "...", "session_id": "..."}
        {"type": "tool_complete", "tool": "...", "session_id": "..."}
        {"type": "response_complete", "session_id": "...", "usage": {...}}
        {"type": "error", "message": "...", "session_id": "..."}
        {"type": "typing", "session_id": "..."}
        {"type": "session_created", "session_id": "..."}

Configuration in config.yaml::

    gateway:
      platforms:
        opencode_desktop:
          enabled: true
          extra:
            host: "127.0.0.1"
            port: 18812
            token: ""           # optional auth token

Or via environment variables:
    OPENCODE_DESKTOP_PORT, OPENCODE_DESKTOP_HOST, OPENCODE_DESKTOP_TOKEN
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports from the main repo
# ---------------------------------------------------------------------------

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.session import SessionSource
from gateway.config import PlatformConfig, Platform

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18812
WS_PING_INTERVAL = 15.0  # seconds between WebSocket pings

# Maximum message size for WS receive (1 MB)
MAX_WS_MESSAGE_SIZE = 1_048_576


# ===========================================================================
# WebSocket Client Manager
# ===========================================================================

class _WSClient:
    """Represents a single connected OpenCode Desktop instance."""

    __slots__ = (
        "ws", "session_id", "chat_id", "user_id",
        "connected_at", "last_activity", "agent_task",
        "_lock",
    )

    def __init__(self, ws: "web.WebSocketResponse", chat_id: str, user_id: str):
        self.ws = ws
        self.session_id: Optional[str] = None
        self.chat_id = chat_id
        self.user_id = user_id
        self.connected_at = time.time()
        self.last_activity = time.time()
        self.agent_task: Optional[asyncio.Task] = None


# ===========================================================================
# Adapter
# ===========================================================================

class OpenCodeDesktopAdapter(BasePlatformAdapter):
    """WebSocket-based platform adapter for OpenCode Desktop."""

    def __init__(self, config, **kwargs):
        platform = Platform("opencode_desktop")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        self._host = os.getenv("OPENCODE_DESKTOP_HOST") or extra.get("host", DEFAULT_HOST)
        self._port = int(os.getenv("OPENCODE_DESKTOP_PORT") or extra.get("port", DEFAULT_PORT))
        self._token = os.getenv("OPENCODE_DESKTOP_TOKEN") or extra.get("token", "")

        # Allow all users (OpenCode Desktop is a personal tool)
        self._allowed_users: list = extra.get("allowed_users", [])
        self._allowed_users_lower: set = {u.lower() for u in self._allowed_users if isinstance(u, str)}

        # Runtime state
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._clients: Dict[str, _WSClient] = {}  # chat_id -> _WSClient
        self._server_task: Optional[asyncio.Task] = None

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "OpenCode Desktop"

    # ── Connection Lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        """Start the WebSocket server."""
        if not AIOHTTP_AVAILABLE:
            logger.error("OpenCode Desktop: aiohttp not available")
            self._set_fatal_error(
                "missing_dependency",
                "aiohttp is required for OpenCode Desktop adapter",
                retryable=False,
            )
            return False

        # Prevent two profiles from using the same port
        try:
            from gateway.status import acquire_scoped_lock, release_scoped_lock
            lock_key = f"{self._host}:{self._port}"
            if not acquire_scoped_lock("opencode_desktop", lock_key):
                logger.error(
                    "OpenCode Desktop: %s:%s already in use by another profile",
                    self._host, self._port,
                )
                self._set_fatal_error(
                    "lock_conflict",
                    "Port already in use by another profile",
                    retryable=False,
                )
                return False
            self._lock_key = lock_key
        except ImportError:
            self._lock_key = None

        # Build aiohttp app
        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/health", self._handle_health)
        # Store reference to adapter for middleware
        self._app["opencode_adapter"] = self

        # CORS middleware
        @web.middleware
        async def _cors_middleware(request, handler):
            if request.method == "OPTIONS":
                response = web.Response(status=200)
                response.headers["Access-Control-Allow-Origin"] = "*"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
                return response
            response = await handler(request)
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response

        self._app.middlewares.append(_cors_middleware)

        try:
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
        except OSError as e:
            logger.error("OpenCode Desktop: failed to bind %s:%s — %s", self._host, self._port, e)
            self._set_fatal_error("bind_failed", str(e), retryable=True)
            return False

        self._mark_connected()
        logger.info(
            "OpenCode Desktop: WebSocket server listening on ws://%s:%s/ws",
            self._host, self._port,
        )
        if self._token:
            logger.info("OpenCode Desktop: token auth enabled")
        else:
            logger.info("OpenCode Desktop: no token configured — accept all local connections")
        return True

    async def disconnect(self) -> None:
        """Stop the WebSocket server and close all client connections."""
        if getattr(self, "_lock_key", None):
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("opencode_desktop", self._lock_key)
            except Exception:
                pass

        self._mark_disconnected()

        # Close all WebSocket connections
        for client in list(self._clients.values()):
            try:
                if client.ws and not client.ws.closed:
                    await client.ws.close(
                        code=1001,
                        message=b"Server shutting down",
                    )
            except Exception:
                pass
        self._clients.clear()

        # Stop aiohttp server
        if self._site:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None

        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

        logger.info("OpenCode Desktop: disconnected")

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a connected OpenCode Desktop client."""
        client = self._clients.get(chat_id)
        if client is None:
            return SendResult(success=False, error=f"Client {chat_id} not connected")
        if client.ws.closed:
            return SendResult(success=False, error="WebSocket closed")

        try:
            await client.ws.send_json({
                "type": "delta",
                "text": content,
                "session_id": client.session_id or "",
            })
            return SendResult(success=True, message_id=str(uuid.uuid4()))
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator."""
        client = self._clients.get(chat_id)
        if client is None or client.ws.closed:
            return
        try:
            await client.ws.send_json({
                "type": "typing",
                "session_id": client.session_id or "",
            })
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get info about a connected OpenCode client."""
        client = self._clients.get(chat_id)
        if client is None:
            return {"name": chat_id, "type": "dm", "chat_id": chat_id}
        return {
            "name": f"OpenCode Desktop ({client.user_id})",
            "type": "dm",
            "chat_id": chat_id,
        }

    # ── HTTP Handlers ─────────────────────────────────────────────────────

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — health check for the WebSocket server."""
        return web.json_response({
            "status": "ok",
            "platform": "opencode_desktop",
            "clients_connected": len(self._clients),
            "server": f"ws://{self._host}:{self._port}/ws",
        })

    async def _handle_websocket(self, request: "web.Request") -> "web.WebSocketResponse":
        """GET /ws — WebSocket upgrade handler."""
        # Auth check
        if self._token:
            auth = request.headers.get("Authorization", "")
            token = ""
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
            elif "token" in request.query:
                token = request.query["token"]
            if not token or token != self._token:
                logger.warning("OpenCode Desktop: rejected WS connection — invalid token")
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.send_json({"type": "error", "message": "Invalid token"})
                await ws.close(code=4001, message=b"Unauthorized")
                return ws

        ws = web.WebSocketResponse(
            max_msg_size=MAX_WS_MESSAGE_SIZE,
            heartbeat=WS_PING_INTERVAL,
        )
        await ws.prepare(request)

        # Generate a unique chat_id for this connection
        chat_id = f"opencode-{uuid.uuid4().hex[:12]}"
        user_id = request.remote or "opencode-user"

        client = _WSClient(ws=ws, chat_id=chat_id, user_id=user_id)
        self._clients[chat_id] = client

        logger.info(
            "OpenCode Desktop: client connected — chat_id=%s, remote=%s",
            chat_id, request.remote,
        )

        # Send welcome message with connection info
        await ws.send_json({
            "type": "connected",
            "chat_id": chat_id,
            "server": f"ws://{self._host}:{self._port}/ws",
        })

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_client_message(client, msg.data)
                elif msg.type == web.WSMsgType.CLOSE:
                    break
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(
                        "OpenCode Desktop: WS error for %s: %s",
                        chat_id, ws.exception(),
                    )
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("OpenCode Desktop: WS loop error for %s: %s", chat_id, e)
        finally:
            # Clean up client
            self._clients.pop(chat_id, None)
            # Stop any running agent task for this client
            if client.agent_task and not client.agent_task.done():
                client.agent_task.cancel()
                try:
                    await client.agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("OpenCode Desktop: client disconnected — chat_id=%s", chat_id)

        return ws

    # ── Message Handling ──────────────────────────────────────────────────

    async def _handle_client_message(self, client: _WSClient, raw: str) -> None:
        """Parse and dispatch a JSON message from an OpenCode Desktop client."""
        client.last_activity = time.time()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_error(client, "Invalid JSON")
            return

        msg_type = data.get("type", "")

        if msg_type == "message":
            await self._handle_user_message(client, data)
        elif msg_type == "command":
            await self._handle_command(client, data)
        elif msg_type == "interrupt":
            await self._handle_interrupt(client, data)
        elif msg_type == "ping":
            if client.ws and not client.ws.closed:
                await client.ws.send_json({"type": "pong"})
        else:
            await self._send_error(client, f"Unknown message type: {msg_type}")

    async def _handle_user_message(self, client: _WSClient, data: Dict[str, Any]) -> None:
        """Process a user text message through the Hermes agent."""
        text = data.get("text", "").strip()
        if not text:
            return

        # Use provided session_id or create a new one
        session_id = data.get("session_id")
        if session_id:
            client.session_id = session_id

        # Build session source
        source = self.build_source(
            chat_id=client.chat_id,
            chat_name=f"OpenCode Desktop ({client.user_id})",
            chat_type="dm",
            user_id=client.user_id,
            user_name="OpenCode User",
        )

        # Create a MessageEvent and dispatch to the gateway
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(uuid.uuid4()),
            timestamp=__import__("datetime").datetime.now(),
        )

        await self.handle_message(event)

    async def _handle_command(self, client: _WSClient, data: Dict[str, Any]) -> None:
        """Process a slash command from OpenCode Desktop."""
        command = data.get("command", "").strip()
        if not command:
            return

        # Handle basic commands client-side
        if command == "/ping":
            if client.ws and not client.ws.closed:
                await client.ws.send_json({"type": "pong", "session_id": client.session_id or ""})
            return

        if command == "/session":
            # Return current session info
            if client.ws and not client.ws.closed:
                await client.ws.send_json({
                    "type": "session_info",
                    "session_id": client.session_id,
                    "chat_id": client.chat_id,
                    "user_id": client.user_id,
                })
            return

        # Forward as a regular message (Hermes will process slash commands)
        source = self.build_source(
            chat_id=client.chat_id,
            chat_name=f"OpenCode Desktop ({client.user_id})",
            chat_type="dm",
            user_id=client.user_id,
            user_name="OpenCode User",
        )

        event = MessageEvent(
            text=command,
            message_type=MessageType.COMMAND,
            source=source,
            message_id=str(uuid.uuid4()),
            timestamp=__import__("datetime").datetime.now(),
        )

        await self.handle_message(event)

    async def _handle_interrupt(self, client: _WSClient, data: Dict[str, Any]) -> None:
        """Interrupt a running agent for this client."""
        # The gateway's interrupt mechanism is handled through the base class
        # and the running agent reference.  We signal via the message handler.
        if client.ws and not client.ws.closed:
            await client.ws.send_json({
                "type": "interrupted",
                "session_id": client.session_id or "",
            })
        logger.info("OpenCode Desktop: interrupt requested for %s", client.chat_id)

    async def _send_error(self, client: _WSClient, message: str) -> None:
        """Send an error response to a client."""
        if client.ws and not client.ws.closed:
            try:
                await client.ws.send_json({
                    "type": "error",
                    "message": message,
                    "session_id": client.session_id or "",
                })
            except Exception:
                pass


# ===========================================================================
# Plugin Registration
# ===========================================================================

def check_requirements() -> bool:
    """Check if the OpenCode Desktop adapter can run."""
    return AIOHTTP_AVAILABLE


def validate_config(config) -> bool:
    """Validate that the platform config is sufficient."""
    extra = getattr(config, "extra", {}) or {}
    port = int(os.getenv("OPENCODE_DESKTOP_PORT") or extra.get("port", DEFAULT_PORT))
    return 1024 <= port <= 65535


def is_connected(config) -> bool:
    """Check if the platform is configured to connect."""
    return True  # Always ready — no external API keys needed


def interactive_setup() -> None:
    """Interactive hermes gateway setup flow for OpenCode Desktop."""
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("OpenCode Desktop")
    print_info("Connect Hermes Agent to OpenCode Desktop via WebSocket.")
    print_info("OpenCode Desktop will show a dedicated Hermes chat panel.")
    print()

    existing_port = get_env_value("OPENCODE_DESKTOP_PORT")
    port = prompt(
        f"WebSocket server port (default {DEFAULT_PORT})",
        default=existing_port or str(DEFAULT_PORT),
    )
    try:
        port_int = int(port)
        if 1024 <= port_int <= 65535:
            save_env_value("OPENCODE_DESKTOP_PORT", str(port_int))
        else:
            print_warning(f"Invalid port — using default {DEFAULT_PORT}")
            save_env_value("OPENCODE_DESKTOP_PORT", str(DEFAULT_PORT))
    except ValueError:
        print_warning(f"Invalid port — using default {DEFAULT_PORT}")
        save_env_value("OPENCODE_DESKTOP_PORT", str(DEFAULT_PORT))

    if prompt_yes_no("Set an auth token for security? (recommended for network access)", False):
        token = prompt("Auth token (leave blank for random)", password=True)
        if not token:
            token = uuid.uuid4().hex[:24]
            print_info(f"Generated token: {token}")
        save_env_value("OPENCODE_DESKTOP_TOKEN", token)
    else:
        save_env_value("OPENCODE_DESKTOP_TOKEN", "")
        print_warning("No auth token — any local process can connect")

    # Always allow all users for this personal/local platform
    save_env_value("OPENCODE_DESKTOP_ALLOW_ALL_USERS", "true")

    print()
    print_success("OpenCode Desktop configuration saved")
    print_info("After setup, install the OpenCode plugin:")
    print_info("  1. Create ~/.config/opencode/plugins/hermes-gateway.ts")
    print_info(f"  2. Connect to ws://localhost:{port}/ws")
    print_info("  3. Start OpenCode Desktop — Hermes panel will appear")


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="opencode_desktop",
        label="OpenCode Desktop",
        adapter_factory=lambda cfg: OpenCodeDesktopAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["OPENCODE_DESKTOP_PORT"],
        install_hint="Uses aiohttp (already available in gateway)",
        setup_fn=interactive_setup,
        # Auth: OpenCode Desktop is a personal tool — allow all authenticated users
        # Token-based security is handled at the WebSocket level
        allowed_users_env="",
        allow_all_env="OPENCODE_DESKTOP_ALLOW_ALL_USERS",
        # Display
        emoji="🖥️",
        pii_safe=True,
        allow_update_command=True,
        # LLM guidance
        platform_hint=(
            "You are chatting via OpenCode Desktop — a coding-focused terminal "
            "interface. The user has access to full coding tools (files, terminal, "
            "git, LSP) through OpenCode. You are the conversational/assistant layer "
            "on top. Use markdown formatting. Keep responses helpful but concise. "
            "For coding tasks, you can suggest approaches while OpenCode handles "
            "implementation details."
        ),
    )
