# Invite-only Multi-user Podcast Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task by task. Execute inline in this session as requested; do not use subagents.

**Goal:** Add invite-only accounts with private workspaces and per-account usage caps to the online Podcast Article service, while fixing the confirmed KB/settings path failures.

**Architecture:** Add a SQLite platform store for identities, invites, sessions, jobs, quotas and usage. Resolve every request to a server-side account context and pass its `WorkspacePaths` through storage, jobs and integrations. Keep one global billable worker, isolate its results by owner, then coordinate a separate Portfolio Hub Caddy change to remove shared Basic Auth only after Flask protection is verified.

**Tech Stack:** Python 3.11 / Flask, SQLite, `argon2-cffi` for Argon2id password hashing, `cryptography` for encrypted per-user integration secrets, existing vanilla JavaScript UI, Caddy 2.

**Spec:** [docs/superpowers/specs/2026-09-23-podcast-article-invite-only-multitenant-auth-design.md](../specs/2026-09-23-podcast-article-invite-only-multitenant-auth-design.md)

## Global Constraints

- Registration is invite-only; each member has an independent workspace.
- Resolve account IDs from server-side sessions; never trust account IDs supplied by clients.
- Store the service's DashScope, DeepSeek and OSS credentials only in `/etc/podcast-article/server.env`.
- Encrypt per-user Notion/TTS secrets at rest; MCP configuration and calls are admin-only.
- Set ASR monthly limits in audio seconds and LLM limits in CNY; quota months use Asia/Shanghai calendar months.
- Treat LLM CNY usage as an estimate from provider-reported tokens and a versioned price table; it is not an invoice guarantee.
- Require one global ASR/article pipeline at a time; queue jobs fairly across accounts.
- Keep the existing OSS namespace assigned to Kevin; do not delete or bulk-move OSS objects.
- The admin role manages identities, invites, quotas and aggregate usage; it has no member-content browsing API. OS/root operators still have filesystem access, so this is application-layer isolation.
- Keep Caddy HTTPS, reverse proxy, SSE flushing and request-size limits; remove Basic Auth only after every Flask route is protected.
- Run locally first; no push or deployment without a separate release approval.
- Do not treat the previous GHCR build as a deployment: ECS image pull was canceled after about 14 MB in 6 minutes. A release gate must verify image delivery, deployment and live auth separately.

## Review Focus

1. An expired, revoked or already-consumed invitation must never create an account; test in `tests/test_platform_store.py`.
2. A member must not read another member's article, audio, export, SSE, memory or OSS key by changing a slug or ID; test in `tests/test_workspace_isolation.py`.
3. Concurrent queue submissions and LLM usage updates must not overspend or charge one member for another's usage; test in `tests/test_quotas.py`.
4. Missing/expired sessions, disabled accounts, missing CSRF tokens and unsafe `next` URLs must fail closed; test in `tests/test_auth_api.py`.
5. Wrong encryption keys and interrupted legacy migration must preserve ciphertext and source data; test in `tests/test_integrations.py` and `tests/test_migration.py`.

---

## File Map

### Podcast Article repository

- Create `podcast_article/workspace.py`: immutable paths for one account's files.
- Create `podcast_article/platform_store.py`: platform SQLite schema and transactional account, invite, session, job and quota operations.
- Create `podcast_article/auth.py`: password hashing, session token handling, CSRF and login throttling.
- Create `podcast_article/quota.py`: preflight reservations, usage settlement and per-account monthly limits.
- Create `podcast_article/admin.py`: bootstrap, invite, account and legacy-migration CLI commands.
- Modify `webapp.py`: public auth routes, default-protected routes, per-request account context, ownership checks and owner-scoped process state.
- Modify `podcast_article/library.py`, `settings.py`, `feeds.py`, `kb.py`, `search.py`, `export.py`, `queue.py`, `usage.py`, `pipeline.py`, `object_storage.py`, `mcp_config.py`, `publish.py`, `tts.py`, `deepdive.py` and `library_ask.py`: accept workspace paths or account-scoped services instead of relying on process-wide user data.
- Create `podcast_article/integration_secrets.py`: encrypt/decrypt user-specific Notion/TTS credentials using `PA_USER_SECRETS_KEY`.
- Create `web/login.html`, `web/login.js` and `web/login.css`; modify `web/index.html`, `web/app.js` and `web/app.css` for session state, logout and quota display.
- Modify `deploy/podcast-article.service`, `deploy/Caddyfile` and `deploy/README.md` for the platform data root and cutover procedure.
- Modify `tests/conftest.py`, `tests/ui/run.sh` and `tests/ui/harness.js`; create `tests/ui/session_seed.py` and focused auth, workspace, quota, migration and UI tests.

### Portfolio Hub repository

- Modify `Caddyfile`, `compose.yaml`, `.github/workflows/deploy.yml` and `deploy/README.md` to remove the Basic Auth hash dependency while retaining TLS, proxying and SSE behavior.

## Task 1: Repair test isolation and make existing model tests credential-free

**Files:** `tests/ui/run.sh`, `tests/test_install_scripts.py`, `tests/test_cli_kb.py`, `tests/test_mcp_tools.py`

**Interfaces:** No product interfaces change. UI test processes receive `PA_KB_FILE` pointing inside their own temporary directory.

- [ ] **Step 1: Confirm the two current model tests fail without a key.**

Run:

```bash
env -u DEEPSEEK_API_KEY uv run pytest \
  tests/test_cli_kb.py::test_ask_uses_library_and_cites \
  tests/test_mcp_tools.py::test_ask_library_uses_only_library_and_cites_sources -q
```

Expected: the existing tests fail while constructing `summarize._client()` before reaching the mocked `_chat`.

- [ ] **Step 2: Make both test doubles replace the client constructor.**

In each test, add this before invoking the CLI or MCP function:

```python
monkeypatch.setattr(summarize, "_client", lambda: object())
monkeypatch.setattr(summarize, "_chat", fake_chat)
```

- [ ] **Step 3: Run those tests with the key unset.**

Run the command from Step 1. Expected: `2 passed`; no real OpenAI-compatible client or network request is created.

- [ ] **Step 4: Add a failing test for UI-runner KB isolation.**

In `tests/test_install_scripts.py`, assert the runner exports `PA_KB_FILE="$TMP/kb.sqlite"` and passes `PA_KB_FILE="$PA_KB_FILE"` into the child server environment.

```python
def test_ui_runner_isolates_knowledge_database():
    script = (ROOT / "tests/ui/run.sh").read_text(encoding="utf-8")
    assert 'export PA_KB_FILE="$TMP/kb.sqlite"' in script
    assert 'PA_KB_FILE="$PA_KB_FILE"' in script
```

- [ ] **Step 5: Run the new isolation test and verify it fails.**

Run: `uv run pytest tests/test_install_scripts.py::test_ui_runner_isolates_knowledge_database -q`  
Expected: FAIL because the runner neither exports nor forwards `PA_KB_FILE`.

- [ ] **Step 6: Isolate the UI runner's knowledge database.**

Add `export PA_KB_FILE="$TMP/kb.sqlite"` to `tests/ui/run.sh` and pass `PA_KB_FILE="$PA_KB_FILE"` to the child `uv run python webapp.py` process.

- [ ] **Step 7: Run the UI suite and check for repository pollution.**

Run: `bash tests/ui/run.sh`

Expected: all UI tests pass; no new root-level `kb.sqlite` is created.

- [ ] **Step 8: Commit.**

```bash
git add tests/ui/run.sh tests/test_install_scripts.py tests/test_cli_kb.py tests/test_mcp_tools.py
git commit -m "test: isolate UI knowledge database and LLM clients"
```

## Task 2: Add account workspaces and the platform SQLite store

**Files:** Create `podcast_article/workspace.py`, `podcast_article/platform_store.py`, `tests/test_workspace.py`, `tests/test_platform_store.py`; modify `pyproject.toml` and `tests/conftest.py`.

**Interfaces:**

- `WorkspacePaths` is a frozen dataclass with `account_id`, `root`, `output_root`, `library_path`, `settings_path`, `feeds_path`, `kb_path` and `integrations_path`.
- `workspace_for(account_id: str, data_root: Path) -> WorkspacePaths` accepts only canonical UUID account IDs and returns paths under `data_root/users/<account_id>`.
- `data_root()` reads `PA_DATA_ROOT` or defaults to `PROJECT_ROOT/data`; once workspace migration completes, request handlers never read the old process-wide `PA_OUTPUT_DIR`/`PA_*_FILE` locations.
- `Account` contains `id`, `username`, `role`, `enabled`, `must_change_password` and `quota`; `AuthCredential` contains `Account` plus `password_hash`.
- `PlatformStore(path: Path)` exposes `bootstrap_admin(username, password_hash, now)`, `create_invite(created_by, token_hash, quota, expires_at)`, `register_invite(token_hash, username, password_hash, now)`, `credential_for_username(username) -> AuthCredential | None`, `user_by_id(user_id) -> Account | None`, `set_enabled(user_id, enabled)`, `replace_password_hash(user_id, password_hash, must_change)`, `record_login_attempt(account_key, ip_key, now)`, `login_allowed(account_key, ip_key, now)`, `create_session(user_id, token_hash, created_at, idle_expires_at, absolute_expires_at)`, `resolve_session(token_hash, now) -> Account | None`, `revoke_session(token_hash)` and `revoke_user_sessions(user_id)`.
- Invite lifecycle errors use `InviteError(ValueError)` so API and CLI can distinguish invalid, expired, revoked and consumed tokens from storage failures.
- `AccountQuota` contains `asr_month_seconds: int | None`, `llm_month_cny: Decimal | None`, `cache_bytes: int | None`, `queue_items: int | None` and `max_upload_bytes: int | None`; `None` means unlimited only when explicitly set by an administrator.

- [ ] **Step 1: Add workspace path tests.**

```python
from podcast_article.workspace import workspace_for

def test_workspace_paths_are_distinct_and_confined(tmp_path):
    alice = workspace_for("11111111-1111-4111-8111-111111111111", tmp_path)
    bob = workspace_for("22222222-2222-4222-8222-222222222222", tmp_path)
    assert alice.output_root != bob.output_root
    assert alice.kb_path != bob.kb_path
    assert alice.root.is_relative_to(tmp_path)
```

Add a second test that rejects `../outside` and non-UUID IDs.

- [ ] **Step 2: Add platform-store lifecycle tests.**

Cover unique usernames (case-folded), bootstrap-only admin creation, one-time invite consumption, expiry, revocation, session expiry, login-attempt throttling and session revocation after disable/reset. Use a temporary SQLite path per test.

```python
import hashlib
import secrets
from decimal import Decimal
import pytest
from podcast_article.platform_store import AccountQuota, InviteError

def test_invite_token_is_single_use(store, admin, alice_password_hash):
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    quota = AccountQuota(3600, Decimal("1.00"), 1_048_576, 5, 2_097_152)
    store.create_invite(admin.id, token_hash, quota=quota, expires_at=2_000)
    alice = store.register_invite(token_hash, "alice", alice_password_hash, now=1_000)
    assert alice.role == "member"
    with pytest.raises(InviteError):
        store.register_invite(token_hash, "alice2", alice_password_hash, now=1_001)
```

- [ ] **Step 3: Run the new tests and verify they fail.**

Run: `uv run pytest tests/test_workspace.py tests/test_platform_store.py -q`

Expected: failures for missing modules and methods.

- [ ] **Step 4: Implement path construction and transactional SQLite methods.**

Create tables for accounts, invites, sessions, login attempts and schema version. Use `BEGIN IMMEDIATE` for invite consumption and account quota reservations. Store only SHA-256 hashes of random invitation/session tokens; never store raw tokens in SQLite. Normalize usernames with Unicode NFKC plus case-folding; reject empty names, names over 64 characters and passwords shorter than 12 characters or over 256 UTF-8 bytes. Passwords accept spaces and passphrases without composition rules.

- [ ] **Step 5: Run the new tests and commit.**

Run the command from Step 3. Expected: all tests pass. Commit `feat: add platform account store and workspace paths`.

## Task 3: Add Argon2id authentication and the administrator CLI

**Files:** Create `podcast_article/auth.py`, `podcast_article/admin.py`, `tests/test_auth.py`, `tests/test_admin_cli.py`; modify `pyproject.toml` and `uv.lock`.

**Interfaces:**

- `hash_password(password: str) -> str` and `verify_password(encoded_hash: str, password: str) -> bool` use Argon2id.
- `validate_new_password(password: str) -> None` raises `ValueError` unless the password has 12–256 UTF-8 bytes; spaces and passphrases are allowed.
- `safe_next_path(raw: str | None) -> str` returns a same-origin relative path or `/`; it rejects schemes, hosts, protocol-relative URLs and backslashes.
- `podcast-admin bootstrap`, `podcast-admin secrets-key`, `podcast-admin invite create`, `podcast-admin invite revoke`, `podcast-admin user disable`, `podcast-admin user enable`, `podcast-admin user reset-password` and `podcast-admin migrate-legacy` are CLI entry points.
- Password input uses `getpass`; no password is accepted as a positional argument.

- [ ] **Step 1: Test password hashing and CLI parsing.**

Assert that the stored hash differs from the password, verifies the correct password, rejects a wrong password, and that `reset-password --help` has no password-value argument. Assert `secrets-key` creates a mode-0600 env file entry, prints no key and refuses to overwrite an existing key.

```python
def test_password_hash_is_argon2id_and_verifies():
    encoded = hash_password("a long test passphrase")
    assert encoded.startswith("$argon2id$")
    assert encoded != "a long test passphrase"
    assert verify_password(encoded, "a long test passphrase")
    assert not verify_password(encoded, "different passphrase")
```

- [ ] **Step 2: Run `uv run pytest tests/test_auth.py tests/test_admin_cli.py -q`.**

Expected: missing-module failures.

- [ ] **Step 3: Implement the hash wrapper and CLI commands.**

Add `argon2-cffi` to `pyproject.toml` and refresh `uv.lock`; configure Argon2id with memory 19 MiB, time cost 2 and parallelism 1. The CLI creates invites with an explicit quota and a 7-day default expiry, prints a raw invite link exactly once with the token in the fragment, and persists only its hash. `podcast-admin secrets-key --env-file PATH` creates `PA_USER_SECRETS_KEY` only when absent, sets mode 0600 and never prints the key. Resetting a password revokes all active sessions and marks the account to change the password at next login.

```python
from argon2 import PasswordHasher

_PASSWORD_HASHER = PasswordHasher(memory_cost=19 * 1024, time_cost=2, parallelism=1)

def hash_password(password: str) -> str:
    validate_new_password(password)
    return _PASSWORD_HASHER.hash(password)
```

- [ ] **Step 4: Run the focused tests and commit.**

Expected: `uv run pytest tests/test_auth.py tests/test_admin_cli.py -q` passes. Commit `feat: add invite and account administration CLI`.

## Task 4: Add Flask login, session, CSRF and authorization middleware

**Files:** Modify `webapp.py`; create `tests/test_auth_api.py`; modify `tests/conftest.py`.

**Interfaces:**

- Public routes: `GET /login`, `GET /invite`, `POST /api/auth/login`, `POST /api/auth/register`, `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/password` and `GET /api/auth/csrf`.
- `g.current_user` contains only the resolved server-side `Account` record.
- `auth.resolve_request(request) -> Account | None` hashes the session cookie and asks `PlatformStore.resolve_session`; it never trusts user/account IDs from headers or JSON.
- A default-deny `before_request` guard protects all non-allowlisted routes. API requests receive JSON 401; page requests redirect to `/login`.
- `guest_client` and authenticated `client` fixtures are separate. Existing backend fixtures authenticate as the test admin.
- Invite URLs carry the one-time token in a URL fragment (for example `/invite#<token>`); the page reads and clears the fragment before posting the token to `/api/auth/register`, so the token is absent from HTTP paths and proxy access logs.
- `GET /api/auth/csrf` issues a random pre-login token; unsafe requests require the same token in the `pa_csrf` cookie and `X-CSRF-Token` header. Login rotates the CSRF token and stores its hash in the server-side session.

- [ ] **Step 1: Add auth API tests.**

Test guest access to `/api/library` returns 401; valid login sets `Secure`, `HttpOnly`, `SameSite=Lax` cookie; invalid login is generic and rate-limited; an expired/reused invitation is rejected; logout and password reset revoke sessions; password change requires the current password; missing/mismatched CSRF cookie/header return 403; an external `next=https://evil.example` cannot redirect away from the app.

```python
def test_guest_api_is_denied(guest_client):
    response = guest_client.get("/api/library")
    assert response.status_code == 401
    assert response.get_json()["error"] == "authentication_required"
```

Add a second test that submits `next=https://evil.example` to login and asserts the successful response redirects to `/`, never to the supplied host.

- [ ] **Step 2: Run `uv run pytest tests/test_auth_api.py -q`.**

Expected: auth routes are missing and guest API access is still 200.

- [ ] **Step 3: Implement the allowlist, login endpoints and server-side session lookup.**

Generate 32 random bytes per session, store only the token hash, rotate the token on login/password change, enforce a 12-hour idle timeout and 7-day absolute timeout (configuration keys `PA_SESSION_IDLE_SECONDS` and `PA_SESSION_ABSOLUTE_SECONDS`), and scope the cookie to `podcast.squareconf.cn`. Accept `next` only when it is a relative same-origin path. While `must_change_password` is true, allow only auth-me, password-change and logout routes.

```python
@app.before_request
def authenticate_request():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint == "static":
        return None
    account = auth.resolve_request(request)
    if account is None:
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication_required"}), 401
        return redirect(url_for("login_page", next=safe_next_path(request.full_path)))
    g.current_user = account
    g.workspace = workspace_for(account.id, DATA_ROOT)
```

- [ ] **Step 4: Run auth tests plus existing route tests.**

Run: `uv run pytest tests/test_auth_api.py tests/test_webapp.py -q`

Expected: auth tests and route tests pass with the authenticated fixture; guest calls remain denied.

- [ ] **Step 5: Commit.**

Commit `feat: add invite-only Flask authentication`.

## Task 5: Make persistent stores resolve from an account workspace

**Files:** Modify `podcast_article/library.py`, `settings.py`, `feeds.py`, `kb.py`, `search.py`, `export.py`, `webapp.py`; create `tests/test_workspace_storage.py`; extend `tests/test_kb.py`.

**Interfaces:**

- Keep existing CLI defaults, but add explicit keyword paths to storage functions: `store_path` for library, `settings_path` for settings, `feeds_path` for feeds, and `db_path` for KB operations.
- Route handlers call `_workspace()` once and pass the returned `WorkspacePaths` to all reads/writes.
- `kb.stats` receives the same `db_path` used by its connection so its reported path and size belong to the current account.

- [ ] **Step 1: Add two-account storage isolation tests.**

Create Alice and Bob workspaces, add a category/feed/setting/memory to Alice, and assert Bob receives empty defaults. Add tests that missing KB directories are created under `data/users/<id>`, never under the source tree. In `tests/test_kb.py`, alternate queries against two KB database paths and assert vector/IDF cache entries always match the path being queried.

```python
def test_library_store_is_per_workspace(alice_client, bob_client):
    assert alice_client.post("/api/categories", json={"name": "Alice"}).status_code == 200
    bob = bob_client.get("/api/categories").get_json()
    assert bob["categories"] == []
```

- [ ] **Step 2: Run `uv run pytest tests/test_workspace_storage.py tests/test_library.py tests/test_settings.py tests/test_feeds.py tests/test_kb.py -q`.**

Expected: the new isolation tests fail against module-global paths; existing tests remain the compatibility baseline.

- [ ] **Step 3: Thread explicit paths through storage layers.**

Use function-local paths instead of changing `os.environ` or module globals during a request. Keep CLI default paths as optional fallbacks. Store personal settings under the workspace and move server-level credentials out of the member settings API.

```python
def test_library_snapshot_uses_requested_store(alice_workspace, bob_workspace):
    library.create("Alice-only", store_path=alice_workspace.library_path)
    assert library.snapshot(store_path=bob_workspace.library_path)["categories"] == []
```

- [ ] **Step 4: Run focused tests and commit.**

Expected: all listed storage tests pass, and a member cannot read another member's KB counts or settings. Commit `refactor: scope podcast stores to account workspaces`.

## Task 6: Scope API resources and transient jobs to their owner

**Files:** Modify `webapp.py`, `podcast_article/deepdive.py`, `qa_store.py`, `tts.py`, `usage.py`; create `tests/test_workspace_isolation.py`.

**Interfaces:**

- `workspace_for(g.current_user.id, data_root)` is the only source for paths in a request or background task.
- Every task record has `owner_id`; `get_job(owner_id, job_id)` returns no record for another owner.
- Owner scope covers `_JOBS`, `_ASKS`, `_TTS_JOBS`, `_KB_JOB`, job logs, SSE streams, QA threads and live usage state.
- Usage recorders are per job and passed explicitly through Pipeline, `summarize._chat`, DeepDive and library ask; remove reliance on one module-global active recorder for web requests.

- [ ] **Step 1: Add API ownership tests.**

Create the same episode slug for two accounts. Verify Bob receives 404 for Alice's article, audio, cover, export, QA thread, ask ID and SSE stream, while Bob can access his own matching slug.

```python
def test_member_cannot_read_other_users_article(alice_workspace, bob_client):
    episode = alice_workspace.output_root / "episode-a"
    episode.mkdir(parents=True)
    (episode / "article.md").write_text("Alice private article", encoding="utf-8")
    response = bob_client.get(f"/api/file/{episode.name}/article.md")
    assert response.status_code == 404
```

- [ ] **Step 2: Add in-memory-state isolation tests.**

Start one Alice ask/TTS/KB task and assert Bob's current/status endpoints never return its ID, URL, progress or logs. Use barriers to interleave two usage recorders and verify costs stay with their owners.

- [ ] **Step 3: Run `uv run pytest tests/test_workspace_isolation.py tests/test_webapp.py tests/test_memory_flow.py tests/test_assistant_api.py -q`.**

Expected: cross-owner lookups currently expose shared process state or return the wrong task; tests must fail before implementation.

- [ ] **Step 4: Add owner checks to all file, audio, export, search, KB, memory, TTS, QA, ask, feed and settings routes.**

Pass the immutable workspace into every spawned thread. Resolve a directory only under `workspace.output_root`; return 404 on owner mismatch. Key volatile maps by `(owner_id, task_id)` or move queryable status into the platform store.

```python
def owned_episode(workspace: WorkspacePaths, slug: str) -> Path | None:
    candidate = (workspace.output_root / slug).resolve()
    if workspace.output_root.resolve() not in candidate.parents or not candidate.is_dir():
        return None
    return candidate
```

- [ ] **Step 5: Run focused tests and commit.**

Expected: every cross-owner access is denied, with existing owner-level behavior unchanged. Commit `feat: enforce account ownership across workbench APIs`.

## Task 7: Add a fair global queue and race-safe monthly quotas

**Files:** Modify `podcast_article/platform_store.py`, `queue.py`, `pipeline.py`, `usage.py`, `summarize.py`, `webapp.py`; create `podcast_article/quota.py`, `tests/test_quotas.py` and `tests/test_platform_jobs.py`.

**Interfaces:**

- `enqueue_job(owner_id, payload, created_at) -> Job` and `next_job() -> Job | None` persist jobs in SQLite. The scheduler rotates accounts and preserves FIFO within each account.
- `reserve_asr(owner_id, job_id, month_key, audio_seconds) -> Reservation` and `reserve_llm_call(owner_id, call_id, month_key, quote_cny) -> Reservation` are atomic; settlement is idempotent and releases unused reservations.
- `quote_llm_upper_bound(model, prompt_utf8_bytes, max_tokens) -> Decimal` uses a conservative input-token upper bound and the versioned price table. Unknown models without a price entry fail closed before provider calls.
- `Pipeline` calls `before_billable_asr(episode)` after metadata resolution and before download/ASR. `summarize._chat` receives an owner-specific quota guard and `UsageRecorder`; no web request uses the module-global recorder.
- Month keys use Asia/Shanghai `YYYY-MM`.

- [ ] **Step 1: Add quota and job-store tests.**

Cover two accounts with independent caps, exact-cap acceptance, over-cap rejection before provider invocation, unknown-price model rejection, cache/upload/queue limits, concurrent reservations where only one can consume the final allowance, failed-job settlement and duplicate settlement idempotency.
Configure the Alice fixture with `llm_month_cny=Decimal("0.01")`.

```python
from decimal import Decimal
import pytest
from podcast_article.quota import QuotaExceeded

def test_reservation_cannot_exceed_remaining_budget(store, alice):
    store.reserve_llm_call(alice.id, "call-a", "2026-09", Decimal("0.006"))
    with pytest.raises(QuotaExceeded):
        store.reserve_llm_call(alice.id, "call-b", "2026-09", Decimal("0.006"))
```

- [ ] **Step 2: Run `uv run pytest tests/test_quotas.py tests/test_platform_jobs.py -q`.**

Expected: missing table/service failures.

- [ ] **Step 3: Implement transactional reservations and the one-worker scheduler.**

Use SQLite `BEGIN IMMEDIATE` for reservations and job claims. Reserve ASR seconds before the first billed step; if source duration is unknown, reserve the configured per-job maximum or reject before downloading. Reserve each LLM call using `quote_llm_upper_bound` before sending it; settle with provider-reported usage and release the remainder afterward. Charge costs already incurred by failed calls. Check local-cache bytes, single-upload bytes and per-account queued-item count before accepting work. Keep `PA_SCHEDULER=0` as the live default; when an administrator enables scheduled feeds, scan each workspace independently and send discovered jobs through the same quota-aware scheduler.

- [ ] **Step 4: Add per-user queue and quota endpoints/UI data.**

Return only the caller's jobs. Return 429 with remaining ASR seconds or CNY budget when a cap blocks work; never include another account's URL or usage in the response. Apply the guard to article generation, DeepDive, KB ask and every other server-paid model call.

- [ ] **Step 5: Run quota, queue and existing usage tests; commit.**

Run: `uv run pytest tests/test_quotas.py tests/test_platform_jobs.py tests/test_queue.py tests/test_usage.py tests/test_pipeline_cloud.py -q`

Expected: all pass without network or provider credentials. Commit `feat: enforce per-account usage quotas`.

## Task 8: Scope OSS objects, personal secrets and MCP permissions

**Files:** Modify `podcast_article/object_storage.py`, `pipeline.py`, `settings.py`, `mcp_config.py`, `webapp.py`, `pyproject.toml`, `uv.lock` and `.env.example`; create `podcast_article/integration_secrets.py`, `tests/test_integrations.py`, `tests/test_object_storage_tenancy.py`.

**Interfaces:**

- New object prefix: `<existing OSS_PREFIX>/users/<account_id>`; the owner account retains the pre-existing prefix and existing `meta.json` object keys.
- `IntegrationSecrets(data_root, key)` exposes `set_for_user(user_id, name, value)`, `get_for_user(user_id, name)` and `status_for_user(user_id)`; ciphertext is stored per user, and `PA_USER_SECRETS_KEY` is read only from `/etc/podcast-article/server.env`.
- MCP list/test/call/configuration routes require admin role; member settings cannot configure or execute commands.

- [ ] **Step 1: Add tenancy and encryption tests.**

Assert Alice's new object key starts with Alice's prefix, Bob's prefix is different, and an old owner key is left unchanged. Assert encrypted secret files do not contain plaintext and cannot be decrypted with another key. Assert member requests to MCP list/test/call endpoints return 403 without spawning a subprocess.

```python
def test_user_secret_is_ciphertext_and_owner_scoped(secret_store, alice, bob, tmp_path):
    secret_store.set_for_user(alice.id, "NOTION_TOKEN", "alice-only-token")
    encrypted = (tmp_path / "users" / alice.id / "integrations.enc").read_bytes()
    assert b"alice-only-token" not in encrypted
    assert secret_store.get_for_user(bob.id, "NOTION_TOKEN") is None
    with pytest.raises(InvalidToken):
        IntegrationSecrets(tmp_path, key=Fernet.generate_key()).get_for_user(alice.id, "NOTION_TOKEN")
```

- [ ] **Step 2: Run `uv run pytest tests/test_integrations.py tests/test_object_storage_tenancy.py tests/test_object_storage.py -q`.**

Expected: missing per-user secret helper and prefix scoping.

- [ ] **Step 3: Implement authenticated encryption and account-derived OSS prefixes.**

Add `cryptography` to the project dependencies and use `cryptography.fernet.Fernet` with a server-only key; version the ciphertext format and fail closed if the key is missing or invalid. Pass an `ObjectStorage(prefix=...)` instance into `Pipeline`; never derive the prefix from a request parameter.

- [ ] **Step 4: Protect integration settings.**

Store personal Notion/TTS secrets encrypted; return only configured/masked state. Require admin role for MCP config/test/call and move the admin configuration under the writable data root.

- [ ] **Step 5: Run focused tests and commit.**

Expected: no user can sign an OSS URL for another prefix or read another user's integration status/value. Commit `feat: isolate OSS and external integrations by account`.

## Task 9: Build the login, invite and account UI

**Files:** Create `web/login.html`, `web/login.js`, `web/login.css`, `tests/ui/auth.test.js`, `tests/ui/session_seed.py`; modify `web/index.html`, `web/app.js`, `web/app.css`, `tests/ui/harness.js`, `tests/ui/run.sh`.

**Interfaces:**

- Login/register pages call the auth API with same-origin cookies and CSRF headers.
- The login form IDs are `login-form`, `username`, `password` and `login-error`; the invitation form uses `invite-form` and `invite-error`.
- The application shell calls `/api/auth/me` before loading user data; a 401 clears local app state and navigates to `/login`.
- `/api/auth/me` returns username, role, quota and remaining usage; the account menu exposes username, monthly ASR/LLM usage, change password and logout.
- Member UI hides administrator-only service-key and MCP controls; server routes still enforce admin role even if a client manually calls them.
- The UI runner sets `PA_PLATFORM_DB="$TMP/platform.sqlite"`, `PA_DATA_ROOT="$TMP/data"`, `PA_USER_SECRETS_KEY`, `PA_TEST_USER_ID` and `PA_UI_SESSION_FILE`; `session_seed.py` creates the test admin/session and writes the token to a mode-0600 file. `run.sh` seeds `tests/ui/seed.js` under `$PA_DATA_ROOT/users/$PA_TEST_USER_ID/output` before launching Flask. `harness.js` attaches the cookie to loopback requests. The Flask code has no test-only auth bypass.

- [ ] **Step 1: Add UI tests for login and session expiry.**

Assert invalid login renders one generic error; invite registration sends the fragment token once and removes it from browser history; valid `/api/auth/me` renders the app; an API 401 clears user data and redirects; quota values render without showing secrets. `session_seed.py` writes its token file mode 0600 inside `$TMP`; `harness.js` reads it and adds the cookie header to its Node fetch wrapper; no token is printed by the UI runner.

```javascript
const assert = require("node:assert/strict");
const page = await boot({ url: `${BASE}/login`, beforeParse(w) {
  w.fetch = async (url) => String(url).endsWith("/api/auth/csrf")
    ? { ok: true, json: async () => ({ token: "csrf-test" }) }
    : { ok: false, status: 401, json: async () => ({ error: "invalid_credentials" }) };
}});
page.$("username").value = "alice";
page.$("password").value = "wrong passphrase";
page.$("login-form").dispatchEvent(new page.window.Event("submit", { bubbles: true, cancelable: true }));
assert.equal(await until(() => page.$("login-error").textContent.length > 0), true);
```

- [ ] **Step 2: Run `PA_UI_TESTS=auth.test.js bash tests/ui/run.sh`.**

Expected: the login page, API handshake and account menu are missing.

- [ ] **Step 3: Implement accessible login/invite forms and account menu.**

Keep login page styles in `login.css`; add `auth.test.js` to `DEFAULT_TESTS` in `tests/ui/run.sh`; add only the session badge/menu and 401 handling to the existing app shell. Read invitation tokens from `location.hash`, immediately clear the fragment with `history.replaceState`, and POST the token once. Do not place a session token, password, invite token or provider key in localStorage.

- [ ] **Step 4: Run the focused UI test and the full UI suite.**

Run: `bash tests/ui/run.sh`

Expected: all existing UI interactions still pass; root-level files present before the run keep the same size and modification time, and every new state file remains under `$TMP`.

- [ ] **Step 5: Commit.**

Commit `feat: add invite login and account controls`.

## Task 10: Add a dry-run legacy migration and server configuration

**Files:** Modify `podcast_article/admin.py`, `deploy/podcast-article.service`, `deploy/README.md`, `README.md` and `README.en.md`; create `tests/test_migration.py`.

**Interfaces:**

- `LegacyMigrator.dry_run(source_root, legacy_project_root, owner_id) -> MigrationReport` reads inventory only; `LegacyMigrator.apply(...)` copies/normalizes supported files and records completion in `platform.sqlite`.
- `podcast-admin migrate-legacy --source-root <data-root> --legacy-project-root <repo-root> --owner kevin --dry-run` reports counts and source paths without writing.
- The same command with `--apply` idempotently assigns existing output/library/queue/feeds/settings/KB state to Kevin; server provider secrets are never copied into a user workspace.
- Existing OSS object keys remain in place and are not deleted or rewritten.

- [ ] **Step 1: Add migration tests using a temporary legacy tree.**

Cover dry-run non-mutation, successful import, rerun idempotency, conflicting destination rejection and preservation of `audio_object_key` values.

```python
def test_legacy_migration_dry_run_does_not_change_source(legacy_tree, migration):
    before = {p: p.read_bytes() for p in legacy_tree.rglob("*") if p.is_file()}
    report = migration.dry_run(legacy_tree, owner="kevin")
    assert report.episode_count == 2
    assert {p: p.read_bytes() for p in before} == before
```

- [ ] **Step 2: Run `uv run pytest tests/test_migration.py -q`.**

Expected: the migration command is missing.

- [ ] **Step 3: Implement backup-first dry-run/apply and the new data-root configuration.**

Set `PA_DATA_ROOT=/srv/podcast-article/data` in the systemd unit and remove the single-workspace `PA_OUTPUT_DIR`, `PA_LIBRARY_FILE`, `PA_QUEUE_FILE` and `PA_FEEDS_FILE` overrides. Derive all account paths from `PA_DATA_ROOT`. Create `PA_USER_SECRETS_KEY` in `/etc/podcast-article/server.env` with the CLI; never place it in GitHub Actions. Keep core provider keys in `/etc/podcast-article/server.env`. Update Chinese and English run instructions so local Web use creates a local secrets key and runs `podcast-admin bootstrap` before opening the app.

- [ ] **Step 4: Run migration tests and review the generated dry-run report.**

Expected: tests pass; the current live data report shows the existing local output tree is empty and OSS keys are preserved as-is.

- [ ] **Step 5: Commit.**

Commit `feat: add owner-preserving legacy migration`.

## Task 11: Remove the shared Caddy Basic Auth in a coordinated cutover

**Files:** In Portfolio Hub, modify `Caddyfile`, `compose.yaml`, `.github/workflows/deploy.yml`, `deploy/README.md`; create `scripts/check-podcast-proxy.sh`. In Podcast Article, modify `deploy/Caddyfile`, `deploy/README.md`.

**Interfaces:** Caddy proxies HTTPS requests to the Flask app without Basic Auth. Flask remains the only authentication and ownership authority. Caddy retains SSE flush and the 1 GB request-size limit.

- [ ] **Step 1: Add configuration checks.**

Create `scripts/check-podcast-proxy.sh` with these assertions and invoke it in the Portfolio Hub deploy workflow before image build:

```bash
#!/usr/bin/env bash
set -euo pipefail
grep -q 'reverse_proxy host.docker.internal:8788' Caddyfile
grep -q 'flush_interval -1' Caddyfile
grep -q 'max_size 1GB' Caddyfile
! grep -qiE 'basic_auth|PODCAST_BASIC_AUTH_HASH' Caddyfile compose.yaml .github/workflows/deploy.yml
```

Add this step before `docker/build-push-action` in `.github/workflows/deploy.yml`:

```yaml
- name: Validate Podcast proxy configuration
  run: bash scripts/check-podcast-proxy.sh
```

- [ ] **Step 2: Run the config checks against the current files.**

Expected: they fail on the current `basic_auth` block and hash environment variable.

- [ ] **Step 3: Update both repositories in local branches/worktrees.**

Keep the Podcast Article changes on the existing feature worktree. For Portfolio Hub, create `feature/podcast-multitenant-auth` from the latest `origin/main` in a separate worktree after confirming a clean base. Remove only the password gate and its secret plumbing. Preserve `podcast.squareconf.cn` upstream, HTTPS, SSE settings and body limit. Update both deployment documents to describe Flask login.

- [ ] **Step 4: Validate Caddy/Compose configuration locally.**

Run `docker compose config --quiet` in Portfolio Hub, then validate both Caddyfiles with the already available image:

```bash
docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2-alpine \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker run --rm -v "$PWD/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2-alpine \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Run each command from its corresponding repository root. The static route checks must pass in all environments.

- [ ] **Step 5: Commit locally in each repository; do not push.**

Commit `chore: delegate podcast authentication to Flask` in Portfolio Hub and `docs: describe Flask account authentication` in Podcast Article. Remove the GitHub Secret only during the separately approved production cutover.

## Task 12: Run complete local acceptance and prepare the release review

**Files:** `tests/test_auth_api.py`, `tests/test_workspace_isolation.py`, `tests/test_quotas.py`, `tests/test_migration.py`, `tests/ui/run.sh`, `tests/ui/harness.js`, `deploy/README.md`.

- [ ] **Step 1: Run all offline backend tests.**

Run: `uv run pytest tests/ -q`

Expected: all tests pass without `DEEPSEEK_API_KEY`, OSS access, real RSS fetches or provider calls.

- [ ] **Step 2: Run all UI tests.**

Run: `bash tests/ui/run.sh`

Expected: all tests pass and no `kb.sqlite`, `settings.json`, `.env`, queue or feed file is created in the repository root.

- [ ] **Step 3: Run two-account acceptance tests.**

Create Alice and Bob in a temporary data root. Exercise login, invitation, logout, disabled account, cross-user route matrix, quota exhaustion, queue rotation, secret separation and OSS prefix checks.

- [ ] **Step 4: Review deployment and rollback steps without executing them.**

The runbook must back up `/srv/podcast-article/data`, `/etc/podcast-article/server.env`, the current Portfolio Hub Caddy configuration and the current image tag. Verify model price entries against current official provider pricing before setting production CNY caps, and verify an image-transfer path that does not depend on the previously slow GHCR pull. Rollback restores the previous app/Caddy pair; no command deletes OSS objects.

- [ ] **Step 5: Report local implementation separately from deployment.**

Provide the commit IDs in both repositories, test results, remaining quota values to configure and a live-cutover checklist. Wait for a separate explicit deployment approval before pushing or changing production.

## Review Focus Mapping

- Invitation expiry/reuse: Task 2 repository transaction tests and Task 3 CLI/API tests.
- Cross-user path/ID/OSS access: Task 5, Task 6 and Task 7 ownership tests.
- Concurrent quota reservations/duplicate settlement: Task 7 platform-store tests.
- Expired/disabled sessions and CSRF: Task 4 auth API tests.
- Wrong secret key/interrupted migration: Task 8 encryption tests and Task 10 dry-run/idempotency tests.
