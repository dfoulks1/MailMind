# Development

This page covers the project layout, how to run the test suite, linting,
type checking, and guidelines for contributing.

---

## Project layout

```
mailmind/
├── README.md                    Project overview and quick start
├── pyproject.toml               Package metadata, dependencies, tool config
├── .env.example                 Environment variable template
│
├── mailmind/                    Python package
│   ├── __init__.py              Version string
│   ├── config.py                Settings dataclass
│   ├── models.py                Enums, exceptions, dataclasses
│   ├── oauth.py                 OAuthTokenManager
│   ├── gmail.py                 GmailClient, ThreadParser
│   ├── ollama.py                OllamaClient
│   ├── rag.py                   RagStore, tokenise, chunk_text
│   ├── analysis.py              HeaderAnalyzer, MIMEAnalyzer,
│   │                            TroubleshootAnalyzer, GmailAnalyzer
│   ├── scheduler.py             IngestionScheduler
│   └── service.py               MailMindService, FastAPI app, CLI
│
├── tests/
│   ├── conftest.py              Shared fixtures and RAW_THREAD constant
│   ├── test_config.py           Settings defaults and from_env()
│   ├── test_gmail.py            GmailClient and ThreadParser
│   ├── test_oauth.py            OAuthTokenManager token lifecycle
│   ├── test_ollama.py           OllamaClient HTTP interactions
│   ├── test_rag.py              RagStore, tokenise, chunk_text
│   ├── test_scheduler.py        IngestionScheduler job logic
│   └── test_service.py          FastAPI endpoints
│
├── scripts/
│   └── mailmind-service.sh      systemd / launchd wrapper script
│
└── docs/
    ├── getting-started.md
    ├── configuration.md
    ├── api.md
    ├── architecture.md
    ├── scheduler.md
    ├── rag.md
    ├── ollama-integration.md
    ├── deployment.md
    └── development.md           ← you are here
```

---

## Setting up a development environment

```bash
git clone https://github.com/you/mailmind
cd mailmind

# Install runtime + dev dependencies
uv sync

# Verify the install
python -m pytest tests/ -v
```

No `.env` or OAuth credentials are needed to run the test suite — all
network calls are mocked with `respx` and `unittest.mock`.

---

## Running the tests

```bash
# All tests
python -m pytest tests/ -v

# A single module
python -m pytest tests/test_rag.py -v

# A single test
python -m pytest tests/test_rag.py::TestQuery::test_matching_term -v

# With log output visible
python -m pytest tests/ -v --log-cli-level=DEBUG

# Stop on first failure
python -m pytest tests/ -x
```

### Test coverage by module

| Test file | Module under test | Network mocks |
|-----------|-------------------|---------------|
| `test_config.py` | `config.py` | None (pure env logic) |
| `test_gmail.py` | `gmail.py` | `respx` (Gmail REST API) |
| `test_oauth.py` | `oauth.py` | `respx` (Google token endpoint) |
| `test_ollama.py` | `ollama.py` | `respx` (Ollama HTTP API) |
| `test_rag.py` | `rag.py` | None (in-memory SQLite) |
| `test_scheduler.py` | `scheduler.py` | `AsyncMock` (GmailClient) |
| `test_analysis.py` | `analysis.py` | `AsyncMock` (GmailClient, OllamaClient) |
| `test_service.py` | `service.py` | `MagicMock` / `AsyncMock` (all dependencies) |

### Test fixtures (conftest.py)

All shared fixtures live in `tests/conftest.py`:

| Fixture | Type | Description |
|---------|------|-------------|
| `no_dotenv` | autouse session | Prevents `.env` file loading during tests |
| `settings` | function | `Settings` with in-memory DB, scheduling disabled |
| `raw_thread` | function | Dict matching Gmail REST API thread shape |
| `sample_thread` | function | Parsed `ThreadSummary` from `raw_thread` |
| `sample_message` | function | First `MessageSummary` from `sample_thread` |
| `mock_ollama` | function | `AsyncMock(spec=OllamaClient)` returning `"LLM response."` |
| `mock_tokens` | function | `AsyncMock(spec=OAuthTokenManager)` returning `"fake_access_token"` |
| `mock_gmail` | function | `AsyncMock(spec=GmailClient)` returning `raw_thread` |
| `analyzer` | function | `GmailAnalyzer(settings, mock_gmail, mock_ollama)` |

### Environment isolation

The `no_dotenv` autouse fixture patches `mailmind.service.load_dotenv`
for the entire test session. This means:

- No `.env` file on disk can change test behaviour.
- Real `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` env vars set in your
  shell do not affect tests, because `Settings()` is constructed directly
  in fixtures with known values.
- Tests that explicitly need env var behavior use
  `unittest.mock.patch.dict("os.environ", {...})`.

---

## Linting and type checking

```bash
# Lint and auto-fix imports/style
ruff check mailmind/ tests/ --fix
ruff format mailmind/ tests/

# Type check
mypy mailmind/
```

The project targets `ruff` with `E`, `F`, `I`, `UP` rules and `mypy` in
strict mode. Both are configured in `pyproject.toml`.

---

## Adding a new HTTP endpoint

1. Define a Pydantic request model in `service.py` if the endpoint takes a
   body.

2. Add the route inside `create_app()` — all routes must be inside this
   function so they capture the `settings` closure.

3. Access the service via `app.state.svc`:
   ```python
   @app.post("/my-endpoint", tags=["my-tag"])
   async def my_endpoint(req: MyRequest) -> dict[str, Any]:
       svc: MailMindService = app.state.svc
       # use svc.store, svc.ollama, svc.gmail, etc.
   ```

4. Add tests to `tests/test_service.py`. The `client` fixture injects
   a mock `MailMindService` directly into `app.state.svc`, bypassing the
   lifespan startup. Mock only the dependencies your endpoint actually uses.

---

## Adding a new analysis mode

1. Add a value to `AnalysisMode` in `models.py`.

2. Add the corresponding pass in `GmailAnalyzer._run_analysis()` in
   `analysis.py`.

3. Update the `AnalysisResult` docstring mode table in `models.py`.

4. Add tests to `tests/test_analysis.py`.

---

## Upgrading the RAG backend

The `RagStore` public interface is:

```python
def open(self) -> RagStore: ...
def close(self) -> None: ...
def ingest_email(self, record: dict) -> bool: ...
def query(self, query_text: str, top_k: int) -> list[dict]: ...
def reindex_range(self, since_ts, until_ts, dry_run) -> int: ...
def full_reindex(self, dry_run) -> int: ...
def stats(self) -> dict[str, int]: ...
```

Any class that implements this interface can replace `RagStore` without
changes to `scheduler.py`, `service.py`, or any test outside `test_rag.py`.
See [rag.md](rag.md) for upgrade paths to semantic search.

---

## Commit conventions

```
feat:     new feature
fix:      bug fix
docs:     documentation only
refactor: code change, no behaviour change
test:     adding or fixing tests
chore:    build system, dependencies
```

---

## Dependency management

Runtime dependencies are declared in `pyproject.toml` under `[project]
dependencies`. Dev-only tools (pytest, ruff, mypy, respx) are under
`[dependency-groups] dev`.

```bash
# Add a runtime dependency
uv add httpx

# Add a dev dependency
uv add --dev pytest-cov

# Update all dependencies
uv lock --upgrade
uv sync
```
