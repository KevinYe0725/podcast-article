# Podcast Article usability and acceptance — 2026-10-02

The existing white interface, green accents, sidebar, article cards and reader layout are retained. Changes focus on shorter instructions, working controls and reliable operations.

## Changes

- Generation rejects repeated clicks, preserves the pasted link on failure and uses Ctrl/Cmd + Enter consistently; ordinary Enter adds a line.
- Search and article loading ignore responses from older requests. Clearing search or leaving an article cancels its pending presentation.
- Connection failures offer a retry instead of logging the account out. CSRF rejection refreshes the token and retries once; other failures are not replayed. Private JSON and page responses use `Cache-Control: no-store`.
- Settings retain unsaved edits when navigating within the application. Save controls appear only for actual changes. API key saving enables the selected account service without saving unrelated edits. Connection checks use that account's saved endpoint and encrypted key.
- Invitation defaults remain seven days, two hours of transcription and CNY 10 of monthly writing usage. Optional limits are collapsed behind one control.
- Notion token and destination configuration are available in the existing publishing settings. Unsupported macOS voices are disabled on the Linux host; default transcription accurately reports cloud operation.
- Queue errors are displayed. Production background scheduling is enabled, so queued tasks continue without keeping a browser open; subscriptions follow each account's existing preferences.
- Completed generation does not interrupt settings, search, queue views or an article already being read.
- The audio player no longer overlaps the assistant controls. Its phone position follows the measured input footer height.
- Delayed conversation-history loading cannot erase a new question. Closing the assistant keeps its conversation and incoming answer; changing article or starting a new conversation invalidates older responses and transports.
- The unused duplicate search-disable control was removed; the existing default-networking and per-question switches remain.
- Usage aggregation ignores malformed JSON and empty entries, preserves mixed billing labels and reports zero for an empty library.
- Deployment updates code and service configuration while preserving account data and private file permissions. Previously deployed owner-only Portfolio import endpoints remain included.

## Local verification

- Complete Python suite: **891 passed, 1 skipped**.
- Complete UI suite: **20 groups passed**, using isolated temporary accounts and files. Coverage includes login, invitations, reader navigation, categories, deletion, playback, search, reading status, queue, feeds, exports, memory, assistant, readonly behavior and usability regressions.
- JavaScript parsing, Python compilation, deployment shell syntax and Git whitespace checks passed.
- Real-browser checks at 390 px confirmed no horizontal overflow, settings draft retention and unobstructed assistant controls. Private test data did not alter repository data files.

## Live acceptance

Target: `https://podcast.squareconf.cn`, on the existing Alibaba ECS `116.62.168.32`.

| Feature | Observed result |
| --- | --- |
| Existing administrator sign-in | Successful; existing sessions survive the service update |
| Default model connection | Returned `deepseek-flash` and `deepseek-v4-pro` |
| Bilibili ingestion | `BV1TQS1YzERH`: 31-minute video, 16.3 MB audio; article produced in about 1 minute 44 seconds |
| Article and transcript | Article readable; transcript contains 33,963 characters |
| Timestamp playback | Jumped to 01:16; audio duration about 31:22, ready state 4, no media error |
| Markdown export | Downloaded 4,958 bytes, including article heading, source URL and timestamps |
| Reading assistant | Actual answers restored and a subsequent answer received while the drawer was closed; reopening preserved both answers |
| Batch queue | Two existing video URLs processed consecutively; both shown as completed, with no queue errors |
| Default web search | Bing returned three results for the connection test |
| Deployment integrity | Runtime files matched the local SHA-256 manifest; scheduler active |
| Other public services | Portfolio homepage and IELTS sign-in returned HTTP 200 |
| Original covers | All four online article covers loaded successfully; phone content width equals the 390 px viewport |

Notion publishing and account-specific TTS require their respective credentials and destinations. Their configuration, encryption, isolation and request flows were tested in temporary workspaces; no external Notion page was published during this acceptance.

## Recovery

The server holds a private pre-release backup under `/srv/backups/podcast-article/usability-20261002-2c112059`: original runtime files, service unit, environment file and a consistent SQLite backup. Frontend revisions also have separate snapshots. The environment backup remains mode 0600 on the server and is not part of this repository. No account content or OSS object was deleted.
