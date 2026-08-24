# ─────────────────────────────────────────────────────────────────────────────
# IRIS backend image — FastAPI (app.py) + the deepagents/LangGraph orchestrator.
#
# BASE: python:3.13-slim, NOT 3.14, even though project_venv on the dev box is
# 3.14. Several pins in requirements.docker.txt only publish manylinux wheels up
# to cp313 (uvloop/httptools via uvicorn[standard], aiohttp, orjson, protobuf,
# psycopg[binary]). On 3.14 pip would fall through to sdists and the build would
# need a full toolchain for packages that are meant to be wheel-only. 3.13 is the
# newest interpreter with complete wheel coverage for this exact pin set.
#
# TWO STAGES: the builder owns pip and the transient compiler; the runtime copies
# only the finished /opt/venv. Nothing that built a wheel survives into the image.
#
# NOTE: app.py calls load_dotenv() at import, but no .env is copied in (see
# .dockerignore) — configuration arrives from the container environment, and
# load_dotenv() is a harmless no-op when the file is absent.
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.13-slim AS builder

# Present ONLY in this stage. The intent is a wheel-only install; this is the
# safety net so a pin that unexpectedly has no cp313 wheel compiles instead of
# failing the build outright. It never reaches the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# A venv rather than --user or system site-packages: the runtime stage then needs
# exactly one COPY to get both the libraries and the console scripts (uvicorn).
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Copied on its own, ahead of the source, so the slow dependency layer is cached
# and only re-resolves when the pin set itself changes.
COPY requirements.docker.txt ./
# --no-cache-dir: pip's wheel/HTTP cache is hundreds of MB of pure dead weight in
# a layer. See requirements.docker.txt's header for why the container installs
# from that file and not requirements.txt (unpinned dump, Windows-only
# python-magic-bin, multi-GB torch/transformers that nothing imports).
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.docker.txt


FROM python:3.13-slim AS runtime

# PYTHONDONTWRITEBYTECODE: the source tree ships root-owned and read-only, so
#   .pyc writes would fail on every import and buy nothing in a one-shot process.
# PYTHONUNBUFFERED: uvicorn/structlog output must reach `docker logs` immediately
#   rather than sit in a stdio buffer that a `docker kill` discards.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

# Non-root. Fixed uid/gid (not auto-assigned) so the named volumes mounted at
# /app/data, /app/workspace and /app/tmp have a predictable owner across rebuilds.
RUN groupadd --system --gid 1001 iris \
 && useradd  --system --uid 1001 --gid iris --home /app iris

WORKDIR /app

# ── Application source ───────────────────────────────────────────────────────
# All 38 root modules, flat. app.py imports IRIS, checkpointer, agent_memory,
# idempotency, recovery, slack_webook, web_api, google_oauth, and those pull in
# the rest (loadenv, subagent_config, PROMPTS/prompt_builder, the tool modules,
# the middleware modules). Copied as a glob rather than named individually so a
# new module cannot be silently left out of the image; __pycache__/*.pyc are
# filtered by .dockerignore.
COPY *.py ./

# Composed system prompts. prompt_builder.py resolves these relative to its own
# file (_ROOT = Path(__file__).parent), reading prompts/iris/{role,
# execution-protocol,delegation-rules}.md, prompts/shared/security-boundaries.md
# and prompts/agents/{aurther,maya,sienna,tavia,grace}.md. A missing file does
# NOT crash — _load_file() substitutes "<!-- MISSING: … -->" — so an omission
# here would ship an agent with silently hollowed-out instructions.
COPY prompts/ ./prompts/

# Progressive-disclosure skills. IRIS.py:164 passes skills=["/skills/"], a
# VIRTUAL path resolved by the FilesystemBackend rooted at this directory, so the
# 8 skills/<name>/SKILL.md files must be present on disk here.
COPY skills/ ./skills/

# The agent's memory sources — IRIS.py:189 memory=["/IRIS.md","/agent.md",
# "/security.md"], again virtual paths under this root. Confirmed to be the only
# three root .md files the code loads; every other root .md (SECURITY_REVIEW.md,
# *_summary.md, *_draft.md, …) is a human report and is excluded.
# --chown because MemoryMiddleware lets the agent EDIT its own memory files
# ("Updated file /agent.md" is a real observed tool result); the rest of the tree
# stays root-owned and unwritable to the app user.
COPY --chown=iris:iris IRIS.md agent.md security.md ./

# ── Writable runtime paths ───────────────────────────────────────────────────
# Pre-created and chowned because two of them are mkdir'd at IMPORT time, which
# would fail for a non-root user against a root-owned /app:
#   workspace/        agent_memory.py:70-71 (also web_api.py:87's upload target,
#                     workspace/uploads)
#   tmp/              web_search.py:178-179 (save_research_brief destination)
#   data/             not used by the code as-is — see the ENV note below
RUN mkdir -p /app/workspace/uploads /app/tmp /app/data \
 && chown -R iris:iris /app/workspace /app/tmp /app/data

# The SQLite defaults are RELATIVE — checkpointer.py:159/:265 "iris_checkpoints
# .sqlite" and agent_memory.py:189 "iris_store.sqlite" — which would resolve into
# the root-owned /app (write fails, saver silently degrades to MemorySaver) and
# would live in the container layer instead of a volume. Redirect both into
# /app/data, which docker-compose.yml backs with a named volume.
# CAUTION: compose `env_file:` OVERRIDES image ENV, and the host .env does set
# IRIS_CHECKPOINT_DB_PATH — so docker-compose.yml restates both under
# `environment:` (which outranks env_file) to keep a Windows path from leaking in.
ENV IRIS_CHECKPOINT_DB_PATH=/app/data/iris_checkpoints.sqlite \
    IRIS_STORE_DB_PATH=/app/data/iris_store.sqlite

# Matches app.py:178-179's own env contract (os.getenv("HOST"/"PORT")), so the
# container and `python app.py` are configured identically. 0.0.0.0 rather than
# 127.0.0.1: a loopback bind is unreachable from outside the container.
ENV HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000

USER iris

# Probes app.py:133's /health — the only health route in the app (web_api.py and
# google_oauth.py add none). Shell form, with the port read from the environment
# INSIDE python, so $PORT is honoured without any sh-quoting games.
# /health always answers 200 by design (app.py:139-141: a degraded Redis or an
# in-memory checkpointer fallback are reported, not raised), so this is a liveness
# probe — process alive and the event loop serving — not a readiness gate.
# start-period is long because the lifespan builds the whole agent graph
# (acreate_iris_agent) before the first request is served.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health', timeout=4)"

# `app:app` is app.py:104's module-level `app = FastAPI(title="IRIS Service",
# lifespan=lifespan)` — the async lifespan is what builds the durable async
# checkpointer in-loop, so this entry point (not a sync one) is mandatory.
# Shell form + `exec` so uvicorn REPLACES sh as PID 1: SIGTERM from `docker stop`
# then reaches uvicorn, which unwinds the lifespan's finally block
# (close_async_checkpointer / close_async_store) instead of being SIGKILLed with
# live SQLite connections. --reload is deliberately absent; the async saver binds
# to the loop it was built in and a reloader restart does not re-enter it cleanly.
CMD exec uvicorn app:app --host "$HOST" --port "$PORT"
