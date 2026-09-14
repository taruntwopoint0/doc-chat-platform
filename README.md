# Document-grounded chatbot platform

Multi-tenant platform where admins upload documents into a workspace and users
later chat against **only** those documents, with citations and a refusal when
the corpus does not cover the question.

**Both milestones are built.** Ingestion (upload → parse → profile → chunk →
contextualise → embed → index) and retrieval (hybrid search → grounded answer
with citations), plus a dashboard at `/` for uploading and asking.

The system is **domain-agnostic**: nothing in the code names a business domain.
The tag vocabulary is derived from whatever documents are uploaded, so the same
code serves ITSM runbooks, HR handbooks, legal contracts or product manuals.

---

## Architecture

Every external dependency sits behind an interface in `app/interfaces/`.
Pipeline code imports the interface; it never imports a vendor SDK.
Implementations are chosen by environment variable at startup, wired in
`app/registry.py` — the only module that names concrete classes.

| Interface | Default | Selected by |
| --- | --- | --- |
| `DocumentParser` | `LiteParser` + `TabularParser` + `TextParser` + `LegacyOfficeParser` | `PARSER` |
| `Embedder` | `GeminiEmbedder` | `EMBEDDER_IMPL` |
| `LLM` | `GeminiLLM` | `LLM_IMPL` |
| `VectorStore` | `PgVectorStore` | `VECTOR_STORE_IMPL` |
| `FileStore` | `PostgresFileStore` | `FILE_STORE_IMPL` |

```
app/
  api/             route handlers, auth dependency, request/response schemas
  interfaces/      abstract base classes + vendor-neutral dataclasses
  implementations/ concrete adapters (Gemini, pgvector, parsers)
  pipeline/        stages: intake, chunking, profiling, embedding, answering
  static/          the dashboard: plain HTML/CSS/JS, no build step
  models/          SQLAlchemy ORM
  workers/         Postgres job queue and the asyncio worker loop
```

### Data model

`workspaces` → `documents` → `chunks`, plus `ingest_jobs` for background work
and `document_files` for the original bytes.

Every content row carries `workspace_id` and **every query filters on it**.
That is the multi-tenancy boundary; `require_workspace` in `app/api/deps.py`
enforces it in one place rather than per-handler.

Indexes on `chunks`: a GIN index on the generated `tsv` column for full-text,
and HNSW on `embedding` for vectors.

**HNSW rather than IVFFlat, deliberately.** IVFFlat trains its centroids when
the index is built, and the migration builds this index on an empty table. An
untrained IVFFlat index does not merely lose recall — it returns *zero rows*.
In this product that renders as "the documents do not cover that": a confident,
wrong refusal, which is the exact failure the design exists to prevent. It was
reproducible on a 7-document corpus — two of four test queries came back wrong
or empty until a `REINDEX`. HNSW has no training step, so it is correct from
the first row and needs no REINDEX as the corpus grows.

The `tsv` column is generated, with the contextual header weighted `A` above
body text at `B`, so a section-title match outranks an incidental body mention.

### Ingestion pipeline

Upload returns a job id immediately; nothing is parsed in the HTTP request.

1. **Intake** — detect MIME from magic bytes, check size, SHA-256 the content,
   store the original, create `documents` + `ingest_jobs` rows, return.
2. **Parse** — to structured `Block`s preserving heading hierarchy; tables
   become markdown. Embedded images get a placeholder with a TODO for vision
   captioning in a later milestone.
3. **Profile** — the LLM is sent the heading tree plus the first ~2000
   characters of each section and returns `{document_type, summary, topics,
   entities}`. Merged into `workspaces.profile` as counted vocabulary.
4. **Chunk** — ~500 tokens with 15% overlap, subject to hard rules (below).
5. **Contextualise** — each chunk gets
   `{document} > {section path} - {description}` in `contextual_header`,
   stored separately from `content` so the UI shows clean text while the
   embedding sees the enriched version.
6. **Embed** — batched, with exponential backoff and jitter on rate limits.
7. **Store** — document tags copied onto each chunk; document marked `ready`.

`ingest_jobs.stage` and `progress_pct` are written at every transition, so
`GET /api/jobs/{id}` is meaningful while a job runs.

### Chunking rules

Enforced in `app/pipeline/chunking.py` and covered by `tests/test_chunking.py`:

- a **numbered procedure is never split mid-sequence** — consecutive
  ascending-numbered items merge into one atomic unit that stays whole even
  when it overshoots the token target;
- a **table always carries its header row**, repeated on every part it spans;
- **PPTX produces exactly one chunk per slide**, speaker notes included;
- no chunk exceeds `EMBED_MAX_INPUT_TOKENS`. This is the one rule that can
  override the others — a chunk too large to embed fails the API call outright.
  When it fires on a procedure, the split lands on a step boundary and the
  continuation is marked, never mid-step.

Bullets that all start "1." are *not* a procedure — they are separate one-step
lists, and merging them would be wrong. Hence the ascending-run check.

### Resumability

Chunks are written **before** any vector exists, with `embedding` NULL. The
embedding stage fills them in batch by batch, committing each one. The work
outstanding is therefore always derivable from the database —
`embedding IS NULL`, served by a partial index — so a worker killed mid-document
resumes from the last committed batch instead of re-embedding the document.
`ingest_jobs.checkpoint` records `chunks_written`, which lets a resumed job skip
parse, profile and chunk entirely.

This matters in practice on Render: the free instance sleeps after 15 idle
minutes, which kills in-flight jobs. They resume on the next boot.

### Accounts and sessions

Only relevant with `AUTH_ENABLED=true`. With it off, every request is an
implicit `local` account — chat history has to belong to a user row, so one
exists even when nobody signs in.

Passwords are hashed with `hashlib.scrypt` — a memory-hard KDF from the
standard library, so there is no bcrypt or argon2 dependency in an image being
kept small. Each password gets its own salt, and the cost parameters are stored
alongside the digest, so raising the work factor later does not invalidate
existing users.

Sessions are opaque random tokens stored **hashed** in `sessions`, not signed
JWTs. Two consequences: a database dump yields no usable sessions, and logout
actually revokes — deleting the row ends the session immediately, which a
self-contained token cannot do.

`ADMIN_USERNAME` / `ADMIN_PASSWORD` create the first account on an empty users
table, once. Without it a fresh deployment could not be signed into at all. It
is skipped as soon as any user exists, so it can never overwrite a real
password.

### Chat history

Kept per `(workspace, user)`. Documents are a shared corpus, but what someone
asked about them is theirs — `test_history_is_private_to_its_user` pins that.
Switching workspace switches conversation, which is what makes "Compliance"
and "Onboarding" feel like separate places rather than one transcript.

**Clear chat** deletes only the conversation. Documents, chunks and the index
survive, because clearing what you asked is not the same as deleting the
corpus and conflating the two would be an unpleasant surprise.

### Versioning

`file_hash` (SHA-256) drives three outcomes, in `app/pipeline/intake.py`:

| Situation | Result |
| --- | --- |
| Same hash, already `ready` | Skipped. No job queued. |
| Same filename, different hash | New version: `version_label` bumped, **old chunks deleted**, re-queued. |
| Otherwise | New document. |

A version reuses the document row, so `documents` holds one row per
(workspace, filename). Stale chunks never survive a re-upload — that is what
`tests/test_versioning.py::test_stale_chunks_never_survive_a_new_version` pins.

---

## Supported file types

| Type | Handling |
| --- | --- |
| PDF | PyMuPDF; outline for headings, font-size heuristics without one |
| DOCX | python-docx; Word heading styles give an exact hierarchy |
| PPTX | python-pptx; one chunk per slide, speaker notes included |
| XLSX / CSV | openpyxl (streaming) / stdlib `csv`; each row as `col: value \| col: value` |
| TXT / MD | read directly |
| HTML | stdlib `html.parser` |
| DOC / PPT / XLS | converted via `soffice --headless --convert-to`, then re-dispatched |

Routing is by MIME type detected from magic bytes, not by extension — a `.txt`
containing `%PDF` is parsed as a PDF. OOXML formats share the ZIP signature, so
the archive directory is inspected to tell DOCX from PPTX from XLSX.

Unsupported uploads are rejected at upload time with a 415 listing what is
accepted.

**Serialising spreadsheet rows as `col: value`** makes a retrieved row
self-describing, which a bare markdown table row is not — and removes any need
to repeat a header across chunks.

### Adding a new format

One class plus one line:

```python
# app/implementations/parsers/rtf_parser.py
class RtfParser(DocumentParser):
    mime_types = ("application/rtf",)

    async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
        ...  # return Blocks; the pipeline handles the rest
```

```python
# app/implementations/parsers/__init__.py, in build_registry()
registry.register(RtfParser())
```

Add the MIME type to `SUPPORTED_MIME_TYPES` in `parsers/mime.py` so uploads are
accepted. `tests/test_parser_registry.py::test_adding_a_format_is_one_class_and_one_registration`
guards that this stays true.

### Adding an embedder or LLM

Implement `Embedder` or `LLM`, then add a factory to the map in
`app/registry.py`:

```python
EMBEDDERS = {"gemini": _gemini_embedder, "voyage": _voyage_embedder}
```

Select it with `EMBEDDER_IMPL=voyage`. If the new embedder has a different
width, set `EMBEDDING_DIMENSIONS` and write a migration to change the
`vector(N)` column — `validate_wiring()` refuses to start if the two disagree,
rather than failing deep inside a background job.

---

## Local setup

### With Docker

```bash
cp .env.example .env    # set GEMINI_API_KEY and ADMIN_TOKEN
docker compose up --build
```

Postgres comes from `pgvector/pgvector:pg16`. Migrations run at container boot.

### Without Docker (no Postgres install needed)

`scripts/dev_db.py` starts a real PostgreSQL 16 with pgvector from the
`pgserver` package, keeping its data in `./.pgdata` so documents survive
restarts. Works on Windows, macOS and Linux with nothing else installed.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip
cp .env.example .env                              # then set GEMINI_API_KEY
python scripts/dev_db.py --write                  # starts Postgres, writes DATABASE_URL
alembic upgrade head
uvicorn app.main:app --reload
```

Run `python scripts/dev_db.py --write` again after a reboot: the port changes
each start, and the script also clears the stale lock an unclean shutdown
leaves behind.

> **Windows:** exclude the project's `.pgdata` folder from real-time antivirus
> scanning. Scanning Postgres's write-ahead log mid-write caused a crash during
> testing (`PANIC: could not write to log file ... Invalid argument`). The data
> recovered, but a database folder should not be scanned while in use.

Back up before anything risky:

```bash
pg_dump -h 127.0.0.1 -p <port> -U postgres -Fc -f backups/docchat.dump postgres
```

### The dashboard

Open `http://localhost:8000/`. Create a workspace with **+ New** — name it for
what it holds, e.g. "Compliance" — drop files in, and watch each one move
through `parsing → profiling → chunking → embedding → ready`. Then ask
questions on the right.

Every claim carries a `[n]` citation you can expand to read the exact passage.
If the documents don't cover a question you get a refusal, styled differently
from an answer so it is never mistaken for one. Conversations are saved per
workspace until **Clear chat**; **Delete** removes a workspace and everything in
it. Saying "hi" describes what the workspace contains.

The UI is plain HTML, CSS and JavaScript served by FastAPI — no build step and
no `node_modules`. Model answers are rendered as markdown by `markdown.js`,
which HTML-escapes everything **before** formatting: answers quote uploaded
documents, and a document is untrusted input.

### Try it without a UI

```bash
python scripts/seed.py --folder ./sample-docs --slug itsm --name "ITSM" --reset
```

`--no-embed` runs parse, profile and chunk but skips the embedding API, so you
can exercise the pipeline with no Gemini key and no cost. Documents stop at
`embedding` rather than `ready`, because a document with no vectors is not
retrievable; a later run finds them via `embedding IS NULL` and finishes.

### Tests

```bash
pytest
```

The suite starts a real PostgreSQL with pgvector in-process via `pgserver`
(a dev dependency) — SQLite cannot stand in, because the schema depends on
pgvector, JSONB, generated tsvector columns and `SELECT ... FOR UPDATE SKIP
LOCKED`. Point it at your own database with `TEST_DATABASE_URL` instead.

---

## API

**Sign-in is off by default** (`AUTH_ENABLED=false`): the dashboard opens
straight onto the workspace and no route asks for a credential. That is the
right setting for a local PoC.

**Turn it on for anything reachable by someone else.** An open instance lets
any visitor upload to your corpus, read every document in it, and spend your
Gemini quota. With `AUTH_ENABLED=true`, two credentials are accepted:

* a **session cookie** from `POST /api/auth/login` — what the dashboard uses;
* the **shared admin token**, as `Authorization: Bearer <token>` or
  `X-Admin-Token: <token>` — for scripts, the seed script and CI, which have no
  browser to hold a cookie.

Uploading, deleting and reindexing then require an admin account; listing
workspaces and chatting do not, so a reader can use a corpus they cannot
change.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/workspaces` | Create a workspace |
| `GET` | `/api/workspaces/{id}` | Workspace, accumulated profile, document counts |
| `POST` | `/api/workspaces/{id}/documents` | Multipart upload → job id |
| `GET` | `/api/workspaces/{id}/documents` | List with status and chunk counts |
| `POST` | `/api/workspaces/{id}/reindex` | Re-run ingestion from stored originals |
| `GET` | `/api/jobs/{id}` | Status, stage, progress, error, resume checkpoint |
| `DELETE` | `/api/documents/{id}` | Delete document, chunks and stored original |
| `POST` | `/api/workspaces/{id}/chat` | Ask a question; cited answer, or a refusal |
| `POST` | `/api/workspaces/{id}/search` | Raw hybrid search, no LLM — for debugging retrieval |
| `GET` | `/api/workspaces` | List workspaces (the dashboard's picker) |
| `GET` | `/api/workspaces/{id}/history` | This user's conversation in this workspace |
| `DELETE` | `/api/workspaces/{id}/history` | Clear it. Documents are untouched |
| `POST` | `/api/auth/login` | Username + password → session cookie |
| `POST` | `/api/auth/logout` | Revoke the session |
| `GET` | `/api/auth/me` | Who the browser is signed in as |
| `POST` | `/api/auth/users` | Add a user (admins only) |
| `GET` | `/` | The dashboard |
| `GET` | `/health` | DB connectivity + pending job count |

```bash
TOKEN=your-admin-token
WS=$(curl -s -X POST localhost:8000/api/workspaces \
  -H "X-Admin-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"ITSM","slug":"itsm"}' | jq -r .id)

JOB=$(curl -s -X POST "localhost:8000/api/workspaces/$WS/documents" \
  -H "X-Admin-Token: $TOKEN" -F "file=@runbook.pdf" | jq -r .job_id)

curl -s "localhost:8000/api/jobs/$JOB" -H "X-Admin-Token: $TOKEN" | jq
```

Auth is a single swappable dependency (`require_admin`). Replacing the shared
token with SSO means rewriting that one function to validate a JWT and return a
`Principal`; no route handler changes, because none of them look at the token.

---

## Deploying to Render (free tier)

`render.yaml` is a Blueprint that creates both the web service and a
PostgreSQL 16 database, free plan throughout.

### Step by step

1. **Push to GitHub.** Create an empty repository on GitHub (no README or
   `.gitignore` — this repo has both), then:
   ```bash
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
   `.env`, `.pgdata/` and `backups/` are git-ignored. They hold your API key and
   your documents, and must never be pushed.
2. **Create the Blueprint.** In Render: **New → Blueprint**, pick the
   repository. Render reads `render.yaml` and proposes a web service and a
   database.
3. **Set the one secret.** Render prompts for `GEMINI_API_KEY` (marked
   `sync: false`, so it is never read from the file). Paste your key.
4. **Apply.** The first build takes several minutes. Migrations run on boot and
   create the `vector` extension.
5. **Check it.** Open `https://<service>.onrender.com/health` and expect
   `"status": "ok"` and `"database": "ok"`. Then open the root URL.

Sign-in is **off** in `render.yaml`: anyone with the URL can read and upload
documents and spend your Gemini quota. To require a login, set
`AUTH_ENABLED=true` and add `ADMIN_PASSWORD` in the dashboard.

### Demo day checklist

- [ ] **5 minutes before:** open `/health`. The free instance sleeps after 15
  idle minutes and takes about a minute to wake; do it before the audience is
  watching. Keep the tab open: the dashboard's health check every 20 seconds
  keeps it awake.
- [ ] **Upload demo documents in advance**, not live. Indexing uses AI calls,
  and on the free Gemini tier (15 requests a minute) a live upload followed by
  immediate questions can hit the limit. Chat then says "the AI service is at
  its request limit — try again in N seconds", which is recoverable but slow on
  stage.
- [ ] **Better: enable billing on the Gemini key.** Paid tier 1 raises the limit
  by orders of magnitude, and `gemini-3.1-flash-lite` costs fractions of a cent
  per question. It removes the single biggest live-demo risk.
- [ ] Ask each planned question once beforehand, so you know the answers and the
  citations they come with.
- [ ] Keep files under 10 MB (the free-tier upload limit).
- [ ] **After the demo:** the URL is public with sign-in off. Suspend the service
  in Render, or rotate the Gemini key, if the link has been shared.

### What the free tier actually means here

These are not incidental; they shaped the defaults in `.env.example`.

- **512 MB RAM, 0.1 CPU.** The whole upload is held in memory to hash and
  store it, so `render.yaml` sets `MAX_UPLOAD_MB=10` (the code default is 50,
  for a normal machine). **Measured peak: ~155 MB** while indexing all eight
  supported file types and answering questions — about 30% of the limit. Keep
  `WORKER_CONCURRENCY=1`; more loops on a tenth of a CPU just contend.
- **The service sleeps after 15 minutes with no inbound traffic.** The worker
  is an asyncio task inside the web process, so it sleeps too. A job running at
  that moment is killed — it is reclaimed and resumed on the next boot, but it
  will not progress while nothing is calling the service. The dashboard polls
  while documents are processing, which keeps the instance awake until they
  finish. Waking from sleep takes about a minute.
- **Gemini free tier: 15 AI requests a minute.** Indexing a document uses
  about two; each question uses one. When the limit is hit, background indexing
  waits as long as Google asks and then continues, and a question returns a
  readable "try again in N seconds" instead of hanging. Enabling billing on the
  key removes the limit in practice.
- **1 GB of database storage, fixed.** Original files live in Postgres
  (`document_files`) so jobs survive restarts and `/reindex` works. Budget
  roughly: each chunk is ~2 KB of text plus a 768-float vector (~3 KB) plus
  index overhead, so ~5 KB per chunk — and the originals sit on top of that.
  A few hundred documents is a comfortable PoC; a real corpus needs a paid
  database or an S3-backed `FileStore`.
- **The free database is deleted 30 days after creation.** Re-upload, or move
  to a paid instance, before then.

### Image size

The image is deliberately lean — no PyTorch, no model downloads. Two decisions
got it there: Docling moved to an optional extra, and pandas was dropped in
favour of openpyxl plus the standard library, which removed pandas and numpy
(112 MB measured) for code that only ever walks rows and formats strings.

**I could not build the image to measure it** — Docker is not installed on the
machine this was built on. Measured instead: the runtime dependency set
installs to **168 MB** of `site-packages` (115 MB excluding `pip` and bytecode).
On top of `python:3.12-slim` (~125 MB), with `--no-compile` skipping ~40 MB of
`.pyc`, that points to roughly **280–300 MB** — at or just under target, but
unverified, and Windows wheel sizes differ from Linux. Check it yourself:

```bash
docker build -t doc-chat-platform . && docker images doc-chat-platform
```

If it comes in over, the next candidates are PyMuPDF (the largest single
dependency) and Pillow, which arrives via python-pptx.

LibreOffice is **not** installed in the image — it is roughly 500 MB, more than
everything else combined. Legacy `.doc` and `.ppt` uploads fail with a message
saying so; `.xls` still works, because the parser falls back to `xlrd` when
LibreOffice is missing. The Dockerfile carries the commented `apt-get` line if
you want them.

### Scanned PDFs

The default parser does no OCR, so a scanned PDF with no text layer yields
nothing and the job fails with a message telling you why. For that corpus:

```bash
pip install -e ".[docling]"   # pulls PyTorch, ~2 GB
PARSER=docling
```

Docling downloads layout and OCR models on first use. On an ephemeral disk it
re-downloads after every restart, so bake them into the image if you go this
route — and expect to need more than 512 MB of RAM.

---

## Notes on the build

Things worth knowing that are not obvious from the code:

- **The worker runs in the API process.** Render's Starter tier is a single
  instance, so a Postgres-backed queue using `FOR UPDATE SKIP LOCKED` beats
  adding Redis and Celery. Moving to Celery later means rewriting
  `app/workers/queue.py` and `worker.py`; nothing else knows how work is
  distributed.
- **Contextual descriptions are per-section by default**, not per-chunk — one
  cheap LLM call describes every section of a document, and chunks in a section
  share it. `CONTEXT_DESCRIPTIONS=chunk` switches to a call per chunk: sharper
  headers, but one request per chunk across a corpus with no size limit.
- **Token counts are estimated, not exact.** Gemini has no offline tokeniser,
  and calling its count-tokens endpoint per candidate chunk would make
  ingestion network-bound for no real gain. The estimator is biased to
  overestimate (3.5 chars/token) so chunks land under the limit rather than
  over. `set_token_counter()` installs an exact one if you have it.
- **Alembic runs through asyncpg**, not a second synchronous driver —
  `psycopg[binary]` bundles its own libpq and would add tens of megabytes for
  nothing.
- **Profiling failures are not fatal.** A document whose profile fails still
  indexes; it just contributes no tags. Refusing to index it would be worse.
- **Hybrid search fuses by rank, not by score.** Cosine distance and
  `ts_rank_cd` are incomparable numbers, so blending them directly needs a
  magic weight that has to be retuned per corpus. Reciprocal rank fusion uses
  only each result's position, needs no tuning, and degrades gracefully when
  one half returns nothing.
- **Refusal is a structured outcome, not a hoped-for sentence.** The model is
  told to emit a `NO_ANSWER` sentinel when the sources do not cover the
  question, and the code checks for it, so `ChatResponse.refused` is reliable
  and the UI can style a refusal differently from an answer.
- **The model names in `.env.example` are the ones the brief specified**, and
  both were confirmed live against the Gemini Developer API:
  `gemini-embedding-2` returns 768-dimensional vectors and
  `gemini-3.1-flash-lite` returns valid JSON for the profiling prompt. They are
  still configuration, not something the code validates, so a project without
  access to them needs different values.
- **Two Gemini Developer API quirks are worth knowing**, because both were
  silent-ish failures found only by making real calls:
  - `EmbedContentConfig.auto_truncate` exists only in Vertex / Enterprise Agent
    Platform mode. Setting it makes the Developer API reject the whole request.
  - `contents=["a", "b"]` matches the SDK's *one Content, many Parts* overload
    and returns a **single** vector for the concatenation. Each text must be
    wrapped in its own `types.Content` to get one vector per input. The
    returned-vector-count check in `GeminiEmbedder` is what caught this, and it
    is worth keeping.
