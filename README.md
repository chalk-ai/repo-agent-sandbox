# Repo agent sandbox

Paste any public GitHub repository and get the [pi](https://pi.dev) coding agent running in a fresh
checkout of it, inside a short-lived, isolated [Chalk](https://chalk.ai) sandbox. The terminal is
drawn by [ghostty-web](https://github.com/coder/ghostty-web), Ghostty's VT parser compiled to
WebAssembly, so pi's full-screen interface works in the browser.

```
browser (ghostty-web) ──WebSocket──▶ server.py (Chalk ScalingGroup)
                                        │  checks the repo on GitHub
                                        ▼
                                  fresh sandbox ── git clone --depth 1 ──▶ github.com
                                        │
                                        └── pi ──▶ Chalk model gateway (key injected at egress)
```

## Files

| File | Role |
| --- | --- |
| `deploy.py` | Builds the server image (with ghostty-web fetched from npm) and deploys it as a `chalkcompute.ScalingGroup`. |
| `server.py` | FastAPI app: `GET /` serves the page, `WS /ws?repo=<owner>/<name>` checks the repo, hands out a sandbox, clones into it and relays pi's PTY. |
| `index.html` | The terminal page. Add `?embed=1` to drop the title when you put it in an iframe, and `?repo=<owner>/<name>` to prefill the box. |

## Run it

You need [`uv`](https://docs.astral.sh/uv/) and a Chalk environment with sandboxes and the model
gateway enabled. Create a Chalk secret named `CHALK_ROUTER_API_KEY` holding a gateway API key (or
pass `--gateway-secret` with another name). Give that key the smallest budget and model list your
gateway allows: anyone who opens the demo spends it.

Credentials come from the SDK’s ambient credentials from `chalk login`. You can also set `CHALK_API_SERVER`,
`CHALK_CLIENT_ID`, `CHALK_CLIENT_SECRET` and `CHALK_ENVIRONMENT_ID` yourself.

```bash
./deploy.py
```

Open the printed `web_url`, paste a repository such as `github.com/pallets/click`, and ask pi about
it. Ctrl+C twice leaves pi for a shell in the checkout; run `pi` to come back.

The deployed server creates sandboxes with its own workload identity (`chalk_identity=True`), so no
client secret is baked into the image.

## How a visitor's sandbox is locked down

- **Repository:** only `github.com/<owner>/<name>`. The server checks GitHub's API first and refuses
  private, missing and oversized (over 250 MB) repositories. The URL that reaches `git` is built from
  the validated owner and name, so input cannot point the clone at another host, a local path or a
  git option.
- **Clone:** shallow, single branch, no tags, no LFS, with a 90-second timeout. The checkout is
  measured afterwards and refused over 400 MB.
- **Kernel:** `KernelPolicy.RESTRICTED` runs the sandbox under gVisor.
- **Network:** a `NetworkPolicy` allowlist of `github.com`, plus the model gateway host added by
  `add_credentials`. Nothing else resolves.
- **Credentials:** the sandbox has no workload identity and no secrets. Its `CHALK_ROUTER_API_KEY`
  variable holds a random placeholder; the sandbox's egress proxy swaps in the real key only on
  requests to the gateway host. Reading the environment from inside the sandbox reveals nothing
  usable anywhere else.
- **Untrusted checkout:** pi starts with `--no-approve --no-extensions --no-mcp --no-skills
  --no-prompt-templates --no-themes --offline`, so nothing project-local in the repository is
  loaded or executed by the agent's harness. pi still reads `AGENTS.md` as context.
- **Size and lifetime:** one CPU, 2 GiB, an 8-minute session, and a hard sandbox `lifetime`, so
  Chalk deletes it even if the server crashes. A sandbox serves exactly one visitor and is
  terminated when the session ends.

## Abuse limits

All of these are environment variables on the ScalingGroup.

| Variable | Default | Meaning |
| --- | --- | --- |
| `REPO_AGENT_MAX_SESSIONS` | `4` | Concurrent sessions. Extra visitors are told to try again. |
| `REPO_AGENT_SESSION_SECONDS` | `480` | Hard cap on one session. |
| `REPO_AGENT_IDLE_SECONDS` | `150` | A session with no keystrokes for this long is ended. |
| `REPO_AGENT_STARTS_PER_MINUTE` | `8` | Sandbox starts across all visitors. |
| `REPO_AGENT_PER_IP_PER_HOUR` | `6` | Starts per client IP, plus one live session per IP. |
| `REPO_AGENT_MAX_REPO_MB` / `REPO_AGENT_MAX_CHECKOUT_MB` | `250` / `400` | Repository size on GitHub, and checkout size on disk. |
| `REPO_AGENT_CLONE_TIMEOUT_SECONDS` | `90` | Clone time limit. |
| `REPO_AGENT_MODELS` | `openai/gpt-5.4-mini,openai/gpt-5.4` | Gateway models; the first is the default, Ctrl+P cycles. |
| `REPO_AGENT_WARM_POOL` | `2` | Started sandboxes kept ready, so a visitor never waits for a cold start. |
| `REPO_AGENT_SANDBOX_CPU` / `REPO_AGENT_SANDBOX_MEMORY` | `1` / `2Gi` | Per-sandbox resources. |

Per-IP limits apply only when the server can see the visitor's public address. Behind a gateway that
forwards only its own private address, every visitor would look identical, so the server falls back
to the global limits.

## WebSocket protocol

| Direction | Message |
| --- | --- |
| client → server | `{"type": "input", "data": "<keystrokes>"}` |
| client → server | `{"type": "resize", "cols": 120, "rows": 40}` |
| server → client | binary frames of raw PTY output |
| server → client | `{"type": "status", "message": "..."}` while the repo is checked and cloned |
| server → client | `{"type": "ready", "expires_in": 480, "repo": "owner/name", "size_mb": 12.3}` |
| server → client | `{"type": "busy" \| "limited" \| "rejected", "message": "..."}`, then close |
| server → client | `{"type": "ended", "reason": "exited" \| "idle" \| "time_limit" \| "error"}`, then close |

Open the socket at `/ws?repo=<owner>/<name>&cols=<n>&rows=<n>` so the PTY starts at the right size.

## Clean up

```bash
uv run --with chalkcompute python -c \
  "from chalkcompute import ScalingGroup; ScalingGroup.from_name('repo-agent-sandbox').delete()"
```

Pooled sandboxes expire on their own lifetime within about 20 minutes.

## License

Apache-2.0. ghostty-web and pi are MIT.

## Local authentication

The SDK reads credentials, API host, and the selected environment from your existing
`chalk login` in `~/.chalk.yml`, including project-scoped and default logins.
For unattended runs, `CHALK_CLIENT_ID` and `CHALK_CLIENT_SECRET` remain supported.
The examples use the current environment selected by your Chalk login.
