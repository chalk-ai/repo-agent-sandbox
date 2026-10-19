#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute==2.13.5"]
# ///
"""Deploy the repo agent sandbox (server.py + index.html) as a Chalk ScalingGroup.

The server creates visitor sandboxes with its own workload identity (`chalk_identity=True`),
so no client secret is baked into the image. Visitor sandboxes get no identity at all; the
only credential they can use is the model gateway key, and they see it only as a placeholder
that the egress proxy swaps in on requests to the gateway.

Before deploying, create a Chalk secret named CHALK_ROUTER_API_KEY in the environment holding a
model gateway API key (or pass --gateway-secret with another secret's name). Give that key the
smallest budget and model list your gateway supports.

Credentials for the deploy itself come from your existing `chalk login` (or the
CHALK_* env vars):
    ./deploy.py
Then open the printed URL and paste a GitHub repository.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GHOSTTY_WEB_VERSION = "0.4.0"
STATIC_DIR = "/app/static"
APP_DIR = "/app/srv"

# ghostty-web ships its WASM inlined in the ESM bundle, so the page needs only dist/*.js.
FETCH_GHOSTTY = (
    'python -c "import io, tarfile, urllib.request; '
    + f"data = urllib.request.urlopen('https://registry.npmjs.org/ghostty-web/-/ghostty-web-{GHOSTTY_WEB_VERSION}.tgz').read(); "
    + "tar = tarfile.open(fileobj=io.BytesIO(data)); "
    + "members = [m for m in tar.getmembers() if m.name.startswith('package/dist/') and m.name.endswith('.js')]; "
    + "[setattr(m, 'name', m.name.split('/')[-1]) for m in members]; "
    + f"tar.extractall('{STATIC_DIR}', members=members)\""
)


def configure_chalk() -> None:
    """Resolve deployment routing settings using the SDK's ambient login."""
    from chalkcompute import ConnectClient

    client = ConnectClient()
    try:
        client._ensure_auth()
        # Only routing settings are needed by the deployed app; credentials stay in the SDK.
        os.environ["CHALK_API_SERVER"] = client._api_server
        os.environ["CHALK_ENVIRONMENT_ID"] = client._env_id
    finally:
        client.close()


def main() -> None:
    configure_chalk()
    from chalkcompute import Image, ScalingGroup

    api_server = os.environ["CHALK_API_SERVER"].rstrip("/")
    image = (
        Image.debian_slim(python_version="3.12")
        .pip_install(["chalkcompute==2.13.5", "fastapi==0.116.1", "uvicorn[standard]==0.35.0"])
        .run_commands(FETCH_GHOSTTY)
        # Together the two files exceed the 32 KiB inline-copy limit, so they ship as a mounted
        # volume. A volume shadows its whole directory, so it gets one of its own.
        .add_local_file(str(HERE / "server.py"), f"{APP_DIR}/server.py", strategy="volume")
        .add_local_file(str(HERE / "index.html"), f"{APP_DIR}/index.html", strategy="volume")
        .workdir("/app")
    )

    sg = ScalingGroup(
        image=image,
        name='repo-agent-sandbox',
        cpu="1",
        memory="2Gi",
        env={
            # Not secrets: which API the server's workload identity talks to, and where the
            # gateway lives. The gateway key stays a Chalk secret the server never reads.
            "CHALK_API_SERVER": api_server,
            "CHALK_ENVIRONMENT_ID": os.environ["CHALK_ENVIRONMENT_ID"],
            "PORT": "8080",
            "REPO_AGENT_STATIC_DIR": STATIC_DIR,
            "REPO_AGENT_GATEWAY_URL": f"{api_server}/v1/router",
            "REPO_AGENT_GATEWAY_SECRET": 'CHALK_ROUTER_API_KEY',
            "REPO_AGENT_MODELS": 'openai/gpt-5.4-mini,openai/gpt-5.4',
            "REPO_AGENT_MAX_SESSIONS": '4',
            "REPO_AGENT_WARM_POOL": '2',
        },
        entrypoint=["python", f"{APP_DIR}/server.py"],
        port=8080,
        chalk_identity=True,
        # Session state lives in the replica (the warm pool and per-IP limits), and each
        # WebSocket must stay on the replica that holds its sandbox, so this stays at one.
        min_replicas=1,
        max_replicas=1,
    )
    t0 = time.monotonic()
    try:
        sg.deploy(build_timeout=1800.0, ready_timeout=1200.0)
    finally:
        print(f"deploy returned after {time.monotonic() - t0:.0f}s")
    print("web_url:", sg.web_url)


if __name__ == "__main__":
    main()
