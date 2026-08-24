# Docker packaging — IRIS

Three containers: `backend` (FastAPI + the LangGraph/deepagents orchestrator),
`ui` (Next.js standalone), `redis` (idempotency dedup).

| file | what it builds |
| --- | --- |
| `Dockerfile` | backend image, `python:3.13-slim`, two-stage, runs as uid 1001 (`iris`) |
| `ui/Dockerfile` | UI image, `node:20-slim`, two-stage, runs as uid 1001 (`nextjs`) |
| `docker-compose.yml` | all three + named volumes |
| `requirements.docker.txt` | the backend's Linux-safe pinned dependency set (NOT `requirements.txt`) |
| `.dockerignore`, `ui/.dockerignore`, `ui/.gcloudignore` | keep secrets and ~1 GB of state out of the build contexts |

## Run the stack

```bash
docker compose config --quiet     # validate (--quiet: `config` prints resolved SECRETS)
docker compose build
docker compose up -d
docker compose logs -f backend
```

UI on <http://localhost:8080>, backend on <http://localhost:8000>,
`GET /health` on either (the UI proxies `/health` to the backend).

```bash
docker compose down       # keeps the volumes
docker compose down -v    # DESTROYS checkpoints, memories, uploads
```

### Individual images

```bash
docker build -t iris-backend .
docker run --rm -p 8000:8000 --env-file .env iris-backend

# NEXT_PUBLIC_API_URL must be a BUILD arg, not a runtime env — see gotcha (a)
docker build -t iris-ui --build-arg NEXT_PUBLIC_API_URL= ./ui
docker run --rm -p 8080:8080 -e BACKEND_ORIGIN=http://host.docker.internal:8000 iris-ui
```

## Required host environment (names only)

Compose reads `./.env` automatically, both for `env_file:` on the backend and for
`${VAR}` interpolation. Values never appear in `docker-compose.yml`.

**Hard requirement**

- `BACKEND_JWT_SECRET` — the one shared HS256 secret. Compose wires it to the
  backend as `BACKEND_JWT_SECRET` (verified in `auth.py`) **and** to the UI as
  `NEXTAUTH_SECRET` (used to sign in the NextAuth `session` callback). They are
  the same value by construction; if they diverge every authenticated request
  401s. Startup fails loudly if it is unset.

**Models** (`loadenv.py`) — `ORCHESTRATOR_NAME`, `ORCHESTRATOR_MODEL_NAME`,
`ORCHESTRATOR_MODEL_API_KEY`, and the same triple for
`ATTIO_SUBAGENT_*`, `JIRA_SUBAGENT_*`, `SLACK_SUBAGENT_*`, `TAVILY_SUBAGENT_*`,
`GOOGLE_WORKSPACE_SUBAGENT_*`.

**Integrations** — `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_TEAM_ID`,
`SLACK_APPROVAL_CHANNEL`, `ATTIO_ACCESS_TOKEN`, `JIRA_URL`, `JIRA_USERNAME`,
`JIRA_API_TOKEN`, `JIRA_DEFAULT_PROJECT`, `TAVILY_API_KEY`, `RESEND_API_KEY`,
`RESEND_FROM_EMAIL`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `SUPABASE_URL`,
`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`,
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`.

**Optional** — `BACKEND_JWT_AUDIENCE` (default `iris-backend`), `WEB_UI_ORIGIN`,
`NEXTAUTH_URL` (default `http://localhost:8080`), `LOG_LEVEL`,
`NEXT_PUBLIC_API_URL` (build arg; empty = same-origin), `IRIS_RECURSION_LIMIT`,
`IRIS_CHECKPOINT_BACKEND`, `LANGCHAIN_*`.

Set by compose, do not override: `REDIS_URL`, `IRIS_CHECKPOINT_DB_PATH`,
`IRIS_STORE_DB_PATH`, `BACKEND_ORIGIN`, `HOST`, `PORT`, `HOSTNAME`.

Name mismatch worth knowing: the UI's NextAuth Google provider reads
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, while `.env` names the same OAuth
client `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.
`docker-compose.yml` maps one onto the other.

## Build-time gotchas

**(a) `NEXT_PUBLIC_*` is baked at BUILD time, not runtime.** Next inlines every
`NEXT_PUBLIC_*` value into the client bundle during `npm run build`, so it must
be passed as a `--build-arg` (`ui/Dockerfile` declares
`ARG NEXT_PUBLIC_API_URL`; compose passes it under `build.args`). Setting it in
`environment:` or `docker run -e` has **no effect** — the string is already
compiled into the JS. Changing it requires a rebuild, not a restart. Empty is the
right value for this stack: it makes the client use relative URLs, which
`next.config.ts`'s server-side rewrites then proxy to `BACKEND_ORIGIN` (that one
*is* a runtime env, read by the Next server).

**(b) The UI build needs network egress to Google Fonts.**
`ui/src/app/layout.tsx` imports `Plus_Jakarta_Sans` from `next/font/google`, and
`next/font` downloads and self-hosts the font files at build time. `npm run build`
therefore reaches out to `fonts.googleapis.com` / `fonts.gstatic.com`, and an
air-gapped or egress-blocked builder fails the build (not the runtime). Options:
allow those two hosts through the build network, or vendor the font locally with
`next/font/local` — a code change, out of scope here.

Related: `ui/.dockerignore` now excludes `.env*`, so the build no longer picks up
`ui/.env`'s `NEXT_PUBLIC_API_URL` implicitly. Pass it as a build arg (compose
already does). This exclusion is the point — `COPY . .` was otherwise baking
`NEXTAUTH_SECRET` and the Google client secret into an image layer.

**(c) `jose` was added to `ui/package.json` without regenerating the lockfile.**
`route.ts` imports it directly but it was only present as a hoisted transitive of
`next-auth`. `npm ci --dry-run` accepts the current lockfile (`node_modules/jose`
is pinned there at 4.15.9), but if a clean `npm ci` ever reports the two files
out of sync, run `npm install --package-lock-only` in `ui/` to add the root
dependency entry.

**(d) Backend base is 3.13, dev venv is 3.14.** Deliberate — see the header
comment in `Dockerfile`. Pinned versions in `requirements.docker.txt` were
resolved against the 3.14 dev venv; they install on 3.13 from cp313 wheels.

## Where the state lives

Four named volumes. `docker compose down` keeps them; only `down -v` deletes them.

| volume | mount | contents |
| --- | --- | --- |
| `iris-data` | `/app/data` | `iris_checkpoints.sqlite` (per-thread LangGraph state, HITL pauses) + `iris_store.sqlite` (per-user memories). Paths forced by `IRIS_CHECKPOINT_DB_PATH` / `IRIS_STORE_DB_PATH`. |
| `iris-uploads` | `/app/workspace` | `workspace/uploads` — files from `POST /api/upload` |
| `iris-briefs` | `/app/tmp` | markdown written by `save_research_brief` |
| `redis-data` | `/data` | idempotency dedup snapshots (6h TTL keys — losing this loses dedup, nothing else) |

If `SUPABASE_DB_URL` (or `IRIS_CHECKPOINT_DB_URL` / `IRIS_STORE_DB_URL`) is a
Postgres DSN, both the checkpointer and the store prefer Postgres and the SQLite
files in `iris-data` go unused — the state then lives in Postgres, not in a
volume. `IRIS_CHECKPOINT_BACKEND=memory` forces the in-process saver and loses
all durability.

Not persisted, by design: the Google OAuth token cache (`google_token.*` is
excluded from the image and from any volume — reconnect through `/google/*`, or
supply `GOOGLE_REFRESH_TOKEN`).
