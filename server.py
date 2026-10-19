"""Clone a public GitHub repository into a locked-down Chalk sandbox and open pi in it.

Each WebSocket on `/ws?repo=<owner>/<name>` gets its own sandbox: gVisor kernel, egress to
github.com only (plus the model gateway, which never sees a real key from inside the
sandbox), a hard lifetime, and no credentials. The server clones the repository there, starts
the pi coding agent in the checkout, and proxies the PTY to the page at `/`, which renders it
with ghostty-web.

Wire protocol on `/ws?repo=<owner>/<name>&cols=<n>&rows=<n>`:
  client -> server  text {"type": "input", "data": "<keystrokes>"}
  client -> server  text {"type": "resize", "cols": <n>, "rows": <n>}
  server -> client  binary frames: raw PTY output
  server -> client  text {"type": "status", "message"}: progress while the sandbox starts
  server -> client  text {"type": "ready", "expires_in": <seconds>, "repo", "size_mb"}
  server -> client  text {"type": "busy" | "limited" | "rejected", "message"}, then close
  server -> client  text {"type": "ended", "reason"}, then close
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import ipaddress
import json
import logging
import os
import re
import shlex
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from chalkcompute import (
    ComputeClass,
    ContainerSecurityPolicy,
    ContainerSession,
    Image,
    KernelPolicy,
    NetworkPolicy,
    Sandbox,
)

# Interactive PTY sessions are exposed through the session client, which is not yet part of
# the public chalkcompute namespace.
from chalkcompute._connect import ConnectClient
from chalkcompute._container import ContainerSessionClient
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

PORT = int(os.environ.get("PORT", "8080"))
HERE = Path(__file__).resolve().parent
STATIC_DIR = Path(os.environ.get("REPO_AGENT_STATIC_DIR", str(HERE / "static")))

MAX_SESSIONS = int(os.environ.get("REPO_AGENT_MAX_SESSIONS", "4"))
SESSION_SECONDS = int(os.environ.get("REPO_AGENT_SESSION_SECONDS", "480"))
IDLE_SECONDS = int(os.environ.get("REPO_AGENT_IDLE_SECONDS", "150"))
PER_IP_PER_HOUR = int(os.environ.get("REPO_AGENT_PER_IP_PER_HOUR", "6"))
STARTS_PER_MINUTE = int(os.environ.get("REPO_AGENT_STARTS_PER_MINUTE", "8"))
WARM_POOL = int(os.environ.get("REPO_AGENT_WARM_POOL", "2"))
POOL_MAX_AGE_SECONDS = int(os.environ.get("REPO_AGENT_POOL_MAX_AGE_SECONDS", "600"))
SANDBOX_CPU = os.environ.get("REPO_AGENT_SANDBOX_CPU", "1")
SANDBOX_MEMORY = os.environ.get("REPO_AGENT_SANDBOX_MEMORY", "2Gi")
# GitHub reports repository size in KiB. The checkout is shallow, so it is usually far
# smaller than this, and the clone is re-measured inside the sandbox anyway.
MAX_REPO_MB = int(os.environ.get("REPO_AGENT_MAX_REPO_MB", "250"))
MAX_CHECKOUT_MB = int(os.environ.get("REPO_AGENT_MAX_CHECKOUT_MB", "400"))
CLONE_TIMEOUT_SECONDS = int(os.environ.get("REPO_AGENT_CLONE_TIMEOUT_SECONDS", "90"))
# The model gateway is an OpenAI-compatible endpoint on the environment's API server.
GATEWAY_URL = os.environ.get("REPO_AGENT_GATEWAY_URL", os.environ["CHALK_API_SERVER"].rstrip("/") + "/v1/router").rstrip("/")
GATEWAY_SECRET = os.environ.get("REPO_AGENT_GATEWAY_SECRET", "CHALK_ROUTER_API_KEY")
MODELS = [m for m in os.environ.get("REPO_AGENT_MODELS", "openai/gpt-5.4-mini,openai/gpt-5.4").split(",") if m]
ENVIRONMENT_ID = os.environ["CHALK_ENVIRONMENT"]
SANDBOX_NAME_PREFIX = "repo-agent-"
SANDBOX_TAG = {"app": "repo-agent-demo"}
MAX_INPUT_BYTES = 16 * 1024
WORKSPACE = "/workspace"

NODE_VERSION = "24.21.0"
PI_VERSION = "1.1.0"
# pi's grep and find tools shell out to rg and fd; Debian names fd `fdfind`. npm install
# scripts are skipped so every sandbox runs the same pinned agent build.
SANDBOX_IMAGE = Image.debian_slim("3.12").run_commands(
    "apt-get update -qq && apt-get install -y -qq --no-install-recommends"
    + " ca-certificates curl git ripgrep fd-find xz-utils less tree"
    + " && ln -s /usr/bin/fdfind /usr/local/bin/fd && rm -rf /var/lib/apt/lists/*",
    f"curl -fsSL https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz"
    + " | tar -xJ -C /usr/local --strip-components=1",
    f"npm install -g --ignore-scripts @earendil-works/pi-coding-agent@{PI_VERSION}",
)

# `owner/name` exactly as GitHub allows them, so nothing but a github.com repository URL is
# ever built from visitor input.
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_NAME = r"[A-Za-z0-9._-]{1,100}"
_REPO_INPUT = re.compile(
    rf"^\s*(?:(?:https?://)?(?:www\.)?github\.com/)?(?P<owner>{_OWNER})/(?P<name>{_NAME}?)(?:\.git)?(?:/.*)?\s*$"
)

PS1 = r"\[\e[38;5;150m\]sandbox\[\e[0m\]:\[\e[38;5;114m\]\w\[\e[0m\]$ "
# pi ignores everything project-local (the checkout is untrusted): no extensions, skills,
# prompt templates, themes or MCP servers from the repository, and no startup network calls.
PI_FLAGS = "--offline --no-approve --no-extensions --no-mcp --no-skills --no-prompt-templates --no-themes"

log = logging.getLogger("repo-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def parse_repo(raw: str) -> tuple[str, str] | None:
    m = _REPO_INPUT.match(raw or "")
    if m is None:
        return None
    owner, name = m.group("owner"), m.group("name")
    name = name.removesuffix(".git")
    if not name or name in (".", ".."):
        return None
    return owner, name


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    size_mb: float
    description: str
    default_branch: str


class RepoRejected(Exception):
    pass


class GitHubChecker:
    """Confirms a repository is public and under the size cap, caching answers for a while.

    Unauthenticated GitHub API calls are rate limited per source address. When that limit is
    hit the check is skipped and the in-sandbox clone timeout and size check still apply.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cache: dict[str, tuple[float, RepoInfo | str]] = {}

    def check(self, owner: str, name: str) -> RepoInfo:
        key = f"{owner}/{name}".lower()
        hit = self.cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < 600:
            if isinstance(hit[1], str):
                raise RepoRejected(hit[1])
            return hit[1]
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{name}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "chalk-repo-agent-demo"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return self._reject(key, f"github.com/{owner}/{name} doesn't exist or isn't public.")
            log.warning("GitHub API returned %s for %s; skipping the pre-check", e.code, key)
            return RepoInfo(f"{owner}/{name}", -1.0, "", "")
        except Exception:
            log.exception("GitHub API check failed for %s; skipping the pre-check", key)
            return RepoInfo(f"{owner}/{name}", -1.0, "", "")
        if data.get("private") or data.get("visibility", "public") != "public":
            return self._reject(key, "Only public repositories can be opened.")
        size_mb = float(data.get("size") or 0) / 1024
        if size_mb > MAX_REPO_MB:
            return self._reject(
                key,
                f"{data.get('full_name', key)} is {size_mb:,.0f} MB; the demo caps repositories at {MAX_REPO_MB} MB.",
            )
        info = RepoInfo(
            full_name=str(data.get("full_name") or f"{owner}/{name}"),
            size_mb=size_mb,
            description=str(data.get("description") or ""),
            default_branch=str(data.get("default_branch") or ""),
        )
        self.cache[key] = (time.monotonic(), info)
        return info

    def _reject(self, key: str, message: str) -> RepoInfo:
        self.cache[key] = (time.monotonic(), message)
        raise RepoRejected(message)


def is_public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


class RateLimiter:
    """Caps sandbox starts globally, and per client IP when the real client IP is visible.

    Some gateways forward only their own private address, which every visitor shares; limiting
    by it would lock everyone out at once, so private addresses get only the global caps.
    """

    def __init__(self, per_hour: int, global_per_minute: int) -> None:
        super().__init__()
        self.per_hour = per_hour
        self.global_per_minute = global_per_minute
        self.starts: dict[str, collections.deque[float]] = collections.defaultdict(collections.deque)
        self.global_starts: collections.deque[float] = collections.deque()
        self.live: set[str] = set()

    @staticmethod
    def _trim(window: collections.deque[float], now: float, horizon: float) -> None:
        while window and now - window[0] > horizon:
            window.popleft()

    def acquire(self, ip: str) -> str | None:
        """Returns a refusal message, or None after recording a start for `ip`."""
        now = time.monotonic()
        self._trim(self.global_starts, now, 60)
        if len(self.global_starts) >= self.global_per_minute:
            return "Lots of people are opening repositories right now. Try again in a minute."
        if is_public(ip):
            if ip in self.live:
                return "You already have a sandbox open in another tab."
            window = self.starts[ip]
            self._trim(window, now, 3600)
            if len(window) >= self.per_hour:
                return "That's a lot of sandboxes for one hour. Try again later."
            window.append(now)
            self.live.add(ip)
        self.global_starts.append(now)
        return None

    def release(self, ip: str) -> None:
        self.live.discard(ip)


state: dict[str, Any] = {}


def pi_models_json() -> str:
    # The key field names an environment variable. Inside the sandbox that variable holds only
    # a placeholder; the egress proxy swaps in the real key on requests to the gateway host.
    return json.dumps(
        {
            "providers": {
                "chalk": {
                    "baseUrl": GATEWAY_URL,
                    "api": "openai-completions",
                    "apiKey": f"${GATEWAY_SECRET}",
                    "headers": {"X-Chalk-Env-Id": ENVIRONMENT_ID},
                    "models": [{"id": m} for m in MODELS],
                }
            }
        }
    )


def shell_rc() -> str:
    first = MODELS[0]
    cycle = ",".join(f"chalk/{m}" for m in MODELS)
    return f"""
PS1={shlex.quote(PS1)}
export PI_TELEMETRY=0 PI_OFFLINE=1
pi() {{ command pi {PI_FLAGS} --model chalk/{first} --models {cycle} "$@"; }}
cd "$(cat /root/.repo-dir 2>/dev/null || echo {WORKSPACE})"
pi
printf '\\n\\033[38;5;150mpi exited.\\033[0m Run \\033[38;5;114mpi\\033[0m to start it again, or explore with \\033[38;5;114mls\\033[0m, \\033[38;5;114mrg\\033[0m, \\033[38;5;114mgit log\\033[0m.\\n\\n'
"""


def make_sandbox() -> Sandbox:
    gateway_host = urlsplit(GATEWAY_URL).hostname
    if not gateway_host:
        raise RuntimeError("REPO_AGENT_GATEWAY_URL is not set")
    # The allowlist is github.com for the clone; add_credentials adds the gateway host and
    # binds the key to it. Nothing else resolves. No chalk_identity: nothing inside the sandbox
    # can call back into Chalk with its own credentials.
    sandbox = Sandbox(
        image=SANDBOX_IMAGE,
        name=f"{SANDBOX_NAME_PREFIX}{uuid.uuid4().hex[:10]}",
        cpu=SANDBOX_CPU,
        memory=SANDBOX_MEMORY,
        lifetime=f"{POOL_MAX_AGE_SECONDS + SESSION_SECONDS + 120}s",
        compute_class=ComputeClass.HOST,
        security_policy=ContainerSecurityPolicy(kernel_policy=KernelPolicy.RESTRICTED),
        network_policy=NetworkPolicy(allowed_hosts=["github.com"]),
        tags=SANDBOX_TAG,
    ).add_credentials(gateway_host, GATEWAY_SECRET)
    sandbox.run()
    setup = (
        f"mkdir -p /root/.pi/agent {WORKSPACE}"
        + f" && printf %s {shlex.quote(pi_models_json())} > /root/.pi/agent/models.json"
        + f" && printf %s {shlex.quote(shell_rc())} > /root/.repo-agent-rc"
    )
    result = sandbox.exec("bash", "-c", setup, timeout_secs=60)
    if result.exit_code != 0:
        terminate_quietly(sandbox)
        raise RuntimeError(f"sandbox setup failed: {result.stdout_text}{result.stderr_text}")
    return sandbox


def clone_into(sandbox: Sandbox, full_name: str) -> float:
    """Shallow-clones the repository and returns the checkout's size in MB."""
    name = full_name.split("/")[-1]
    dest = f"{WORKSPACE}/{name}"
    url = f"https://github.com/{full_name}.git"
    # No hooks run on clone, LFS is not installed, and the URL is built only from a validated
    # owner/name, so the clone cannot be pointed at another host or a local path.
    script = (
        "export GIT_TERMINAL_PROMPT=0 GIT_LFS_SKIP_SMUDGE=1"
        + f" && timeout {CLONE_TIMEOUT_SECONDS} git clone -q --depth 1 --single-branch --no-tags"
        + f" -- {shlex.quote(url)} {shlex.quote(dest)} 2>&1"
        + f" && du -sm {shlex.quote(dest)} | cut -f1"
        + f" && printf %s {shlex.quote(dest)} > /root/.repo-dir"
    )
    result = sandbox.exec("bash", "-c", script, timeout_secs=CLONE_TIMEOUT_SECONDS + 30)
    out = (result.stdout_text + result.stderr_text).strip()
    if result.exit_code != 0:
        if result.exit_code == 124:
            raise RepoRejected(f"Cloning {full_name} took longer than {CLONE_TIMEOUT_SECONDS} s.")
        raise RepoRejected(f"Couldn't clone {full_name}: {out.splitlines()[-1] if out else 'unknown error'}")
    try:
        size_mb = float(out.splitlines()[-1])
    except (ValueError, IndexError):
        size_mb = -1.0
    if size_mb > MAX_CHECKOUT_MB:
        raise RepoRejected(f"The checkout is {size_mb:,.0f} MB; the demo caps it at {MAX_CHECKOUT_MB} MB.")
    return size_mb


def terminate_quietly(sandbox: Sandbox) -> None:
    try:
        sandbox.terminate()
    except Exception:
        log.exception("terminate failed for %s", sandbox.id)


def reap_orphans() -> None:
    """Terminates sandboxes a previous replica left behind; their lifetime would also end them."""
    try:
        for info in Sandbox.list_all():
            if (info.name or "").startswith(SANDBOX_NAME_PREFIX) and info.status in ("pending", "running"):
                terminate_quietly(Sandbox.from_id(info.id))
    except Exception:
        log.exception("orphan reap failed")


class WarmPool:
    """Keeps `size` started sandboxes ready, because a cold start takes anywhere from 2 to 60 s.

    Every sandbox handed out is fresh: a visitor never gets one another visitor has used.
    """

    def __init__(self, size: int) -> None:
        super().__init__()
        self.size = size
        self.ready: collections.deque[tuple[float, Sandbox]] = collections.deque()
        self.filling = 0

    def fill(self) -> None:
        while len(self.ready) + self.filling < self.size:
            self.filling += 1
            asyncio.create_task(self._create())

    async def prime(self) -> None:
        """Starts one sandbox and waits for it, then fills the rest in the background."""
        self.filling += 1
        await self._create()
        self.fill()

    async def _create(self) -> None:
        try:
            t0 = time.monotonic()
            sandbox = await asyncio.to_thread(make_sandbox)
            self.ready.append((time.monotonic(), sandbox))
            log.info("pooled sandbox %s started in %.1fs", sandbox.id, time.monotonic() - t0)
        except Exception:
            log.exception("pooled sandbox start failed")
            await asyncio.sleep(10)
        finally:
            self.filling -= 1

    async def take(self) -> Sandbox:
        while self.ready:
            created, sandbox = self.ready.popleft()
            if time.monotonic() - created < POOL_MAX_AGE_SECONDS:
                self.fill()
                return sandbox
            asyncio.create_task(asyncio.to_thread(terminate_quietly, sandbox))
        self.fill()
        return await asyncio.to_thread(make_sandbox)

    async def recycle_forever(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            while self.ready and now - self.ready[0][0] >= POOL_MAX_AGE_SECONDS:
                _, stale = self.ready.popleft()
                await asyncio.to_thread(terminate_quietly, stale)
            self.fill()

    async def drain(self) -> None:
        while self.ready:
            _, sandbox = self.ready.popleft()
            await asyncio.to_thread(terminate_quietly, sandbox)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await asyncio.to_thread(reap_orphans)
    pool = WarmPool(WARM_POOL)
    # The first sandbox also builds the image; wait for it so the replica only reports ready
    # once a visitor can get a sandbox immediately.
    await pool.prime()
    recycler = asyncio.create_task(pool.recycle_forever())
    state["pool"] = pool
    state["slots"] = asyncio.Semaphore(MAX_SESSIONS)
    state["limiter"] = RateLimiter(PER_IP_PER_HOUR, STARTS_PER_MINUTE)
    state["github"] = GitHubChecker()
    state["active"] = 0
    state["sessions"] = ContainerSessionClient.from_connect(ConnectClient())
    yield
    recycler.cancel()
    await pool.drain()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse((HERE / "index.html").read_text())


# ghostty-web's bundle (its WASM is inlined) plus the empty chunk it imports.
STATIC_FILES = {"ghostty-web.js", "__vite-browser-external-2447137e.js"}


@app.get("/static/{name}")
async def static_file(name: str) -> FileResponse:
    if name not in STATIC_FILES:
        raise HTTPException(status_code=404)
    return FileResponse(
        STATIC_DIR / name,
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {"ok": True, "active": state["active"], "capacity": MAX_SESSIONS, "models": MODELS, "max_repo_mb": MAX_REPO_MB}
    )


def client_ip(websocket: WebSocket) -> str:
    forwarded = websocket.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return websocket.client.host if websocket.client else "unknown"


def clamp_dim(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def banner(info: RepoInfo, size_mb: float, cols: int) -> str:
    size = f"{size_mb:,.0f} MB" if size_mb >= 0 else "size unknown"
    model = MODELS[0]
    if cols >= 76:
        text = f"""
\x1b[38;5;150m  chalk sandbox\x1b[0m  ·  {info.full_name}  ·  {size} checkout  ·  {SESSION_SECONDS // 60} minutes

  gVisor kernel · egress: github.com + Chalk model gateway · no secrets inside
  pi is running on \x1b[38;5;114m{model}\x1b[0m through the gateway. Try asking:
    \x1b[38;5;114mexplain how this project is structured\x1b[0m
    \x1b[38;5;114mfind the entry point and walk me through a request\x1b[0m
    \x1b[38;5;114madd a test for one function and run it\x1b[0m
  Ctrl+C twice leaves pi for a shell; run \x1b[38;5;114mpi\x1b[0m to come back.

"""
    else:
        text = f"""
\x1b[38;5;150m  chalk sandbox\x1b[0m
  {info.full_name}
  {size} · {SESSION_SECONDS // 60} min · github only

"""
    return text.replace("\n", "\r\n")


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    ip = client_ip(websocket)
    limiter: RateLimiter = state["limiter"]
    slots: asyncio.Semaphore = state["slots"]

    async def send(msg: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(msg))

    parsed = parse_repo(websocket.query_params.get("repo", ""))
    if parsed is None:
        await send({"type": "rejected", "message": "Paste a public GitHub repository, like github.com/owner/name."})
        await websocket.close()
        return
    refusal = limiter.acquire(ip)
    if refusal is not None:
        await send({"type": "limited", "message": refusal})
        await websocket.close()
        return
    if slots.locked():
        limiter.release(ip)
        await send({"type": "busy", "message": "Every demo sandbox is in use. Try again in a minute."})
        await websocket.close()
        return

    async with slots:
        state["active"] += 1
        sandbox: Sandbox | None = None
        session: ContainerSession | None = None
        try:
            owner, name = parsed
            await send({"type": "status", "message": f"Checking github.com/{owner}/{name}…"})
            github: GitHubChecker = state["github"]
            info = await asyncio.to_thread(github.check, owner, name)
            t0 = time.monotonic()
            await send({"type": "status", "message": "Starting a fresh sandbox…"})
            pool: WarmPool = state["pool"]
            sandbox = await pool.take()
            await send({"type": "status", "message": f"Cloning {info.full_name}…"})
            size_mb = await asyncio.to_thread(clone_into, sandbox, info.full_name)
            cols = clamp_dim(websocket.query_params.get("cols"), 20, 300, 100)
            rows = clamp_dim(websocket.query_params.get("rows"), 5, 120, 30)
            await send({"type": "status", "message": "Starting pi…"})
            # The banner is display-only, so it goes straight to the browser rather than
            # through the shell, where its quotes would need escaping.
            await websocket.send_bytes(banner(info, size_mb, cols).encode())
            session = await asyncio.to_thread(
                state["sessions"].new_session,
                sandbox.id,
                "/bin/bash",
                "--noprofile",
                "--rcfile",
                "/root/.repo-agent-rc",
                "-i",
                workdir=WORKSPACE,
                env={"TERM": "xterm-256color", "HOME": "/root", "COLORTERM": "truecolor"},
                pty=(cols, rows),
            )
            log.info(
                "session for %s on %s ready in %.1fs (sandbox %s)",
                ip,
                info.full_name,
                time.monotonic() - t0,
                sandbox.id,
            )
            await send(
                {"type": "ready", "expires_in": SESSION_SECONDS, "repo": info.full_name, "size_mb": round(size_mb, 1)}
            )
            reason = await proxy(websocket, session)
            await send({"type": "ended", "reason": reason})
            await websocket.close()
        except RepoRejected as e:
            # The visitor may already be gone; nothing else to clean up here.
            with contextlib.suppress(Exception):
                await send({"type": "rejected", "message": str(e)})
                await websocket.close()
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("session for %s failed", ip)
            with contextlib.suppress(Exception):
                await send({"type": "ended", "reason": "error"})
                await websocket.close(code=1011)
        finally:
            state["active"] -= 1
            limiter.release(ip)
            if sandbox is not None:
                await asyncio.to_thread(terminate_quietly, sandbox)


async def proxy(websocket: WebSocket, session: ContainerSession) -> str:
    """Pumps PTY output to the browser and keystrokes to the PTY; returns why it stopped."""
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[Any] = asyncio.Queue()
    deadline = time.monotonic() + SESSION_SECONDS
    last_input = time.monotonic()

    # The SDK's event stream is a blocking iterator, so it gets its own thread.
    def read_events() -> None:
        try:
            for event in session.events():
                loop.call_soon_threadsafe(events.put_nowait, event)
        except Exception as e:  # noqa: BLE001 - any stream failure ends the session via the queue
            loop.call_soon_threadsafe(events.put_nowait, e)
        loop.call_soon_threadsafe(events.put_nowait, None)

    threading.Thread(target=read_events, daemon=True).start()

    async def pump_output() -> str:
        while True:
            event = await events.get()
            if event is None or isinstance(event, Exception):
                return "exited"
            if isinstance(event, ContainerSession.OutputEvent):
                await websocket.send_bytes(event.data)
            elif isinstance(
                event,
                (
                    ContainerSession.ExitedEvent,
                    ContainerSession.FailedEvent,
                    ContainerSession.TimedOutEvent,
                    ContainerSession.ErrorEvent,
                ),
            ):
                return "exited"

    async def pump_input() -> str:
        nonlocal last_input
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect
            text = msg.get("text")
            if not text or len(text) > MAX_INPUT_BYTES:
                continue
            ctrl = json.loads(text)
            if ctrl.get("type") == "input" and isinstance(ctrl.get("data"), str):
                last_input = time.monotonic()
                await asyncio.to_thread(session.write, ctrl["data"].encode())
            elif ctrl.get("type") == "resize":
                cols = clamp_dim(ctrl.get("cols"), 20, 300, 100)
                rows = clamp_dim(ctrl.get("rows"), 5, 120, 30)
                await asyncio.to_thread(session.resize, cols, rows)

    async def watchdog() -> str:
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now >= deadline:
                return "time_limit"
            if now - last_input >= IDLE_SECONDS:
                return "idle"

    tasks = [asyncio.create_task(c) for c in (pump_output(), pump_input(), watchdog())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return next(iter(done)).result()
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    import uvicorn

    # Pings well under the gateway's 60 s idle timeout keep a quiet terminal connected.
    uvicorn.run(app, host="0.0.0.0", port=PORT, ws_ping_interval=20, ws_ping_timeout=20)
