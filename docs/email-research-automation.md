# Email-to-Deep-Research automation

Odysseus can deterministically turn approved inbound email into a durable Deep
Research job, acknowledge it in-thread, and deliver the completed sanitized
HTML report in the same thread. The language model researches and writes the
report; it does not discover mail, control the queue, choose recipients, or
decide whether delivery succeeded.

## Architecture and safety

`EmailResearchWorker` runs as an independent asynchronous application worker
named `email-research-worker`. It calls the existing IMAP/SMTP account helpers,
`ResearchHandler`, endpoint resolver, settings storage, and SQLAlchemy database
layer. It never enters the generic scheduled-agent runner or its browser
activity gate. Browser tabs, foreground API traffic, chat sessions, and login
heartbeats therefore cannot pause or cancel it.

The worker stays in the application process by default. This is deliberate for
SQLite deployments: all queue changes use the application's transactions,
SQLite WAL mode, busy timeout, and atomic leases. A second container writing
the same SQLite file is not safe. A future separate Compose service should be
enabled only with a server database or an authenticated internal queue API.

Every inbound message has a unique `(owner, account, RFC Message-ID)` row.
Acknowledgements and final replies have stable idempotency keys and deterministic
outbound Message-IDs. Before a send, the row enters `sending`; after a restart,
the worker reconciles the deterministic Message-ID against Sent mail. If the
provider outcome cannot be established, Odysseus fails closed with
`delivery_uncertain` instead of blindly sending a duplicate.

The explicit states are `received`, `acknowledged`, `research_queued`,
`researching`, `research_complete`, `delivering`, `delivered`, `retry_wait`,
`failed`, `rejected`, and `cancelled`. Leases expire after a crash, so another
worker cycle can resume the row. Temporary failures use bounded exponential
backoff; authentication, policy, validation, and uncertain-delivery failures
are permanent.

Inbound content is untrusted. Odysseus:

- ignores its own outbound address and automation headers;
- ignores auto-submitted, bulk/list, vacation, delivery-failure, and default
  no-reply messages;
- enforces sender allow/block rules, subject policy, size limits, and daily
  limits in application code;
- removes common signatures, reply history, quoted lines, and tracking markup;
- extracts only configured, size-bounded text attachments;
- stores sanitized HTML but constructs the topic from cleaned text; and
- wraps the topic in an immutable server-side security boundary that denies
  tool use, secret access, commands, file access, setting changes, recipient
  changes, and policy bypass.

Outbound HTML is sanitized with a strict allowlist. Scripts, forms, embedded
objects, event attributes, unsafe URL schemes, comments, and remote trackers
are removed. Small reports appear in the HTML body. Reports above the configured
threshold have a summary in the body and a sanitized `report.html` attachment.

## Enable through the UI

1. In **Settings → Integrations**, create or select a non-production IMAP/SMTP
   account and test both incoming and outgoing connectivity. Passwords and
   OAuth tokens stay in the existing encrypted account storage.
2. Open **Settings → Email → Email Research Automation**.
3. Select the account and, optionally, a dedicated research endpoint/model.
4. Configure at least one allowed address or domain. An empty allowlist accepts
   any sender not otherwise blocked, so it is inappropriate for an exposed
   mailbox.
5. Set the limits and templates, save, then enable the toggle.
6. Use **Mock lifecycle** to validate parsing and policy without external
   calls. Use **Test acknowledgement** only when the selected mailbox may
   safely receive a real test message.
7. Use **Run now** to execute a cycle immediately. It bypasses browser-idle
   scheduling; only the worker's own cycle lock can return a no-op.

The activity panel shows masked sender, subject, state, attempts, next retry,
timestamps, delivery state, and safe error summaries. Failed jobs can be
retried. Received, queued, and retrying jobs can be cancelled.

## Safe example configuration

This example contains no secrets. Configure credentials only in the UI.

```dotenv
ODYSSEUS_INPROCESS_EMAIL_RESEARCH=1
```

Suggested initial UI values for a test:

```text
Enabled: false until tests pass
Required subject prefix: Research:
Allowed sender addresses: researcher@example.test
Allowed sender domains: example.test
Maximum request length: 50000
Maximum attachment size: 1048576
Maximum simultaneous jobs: 1
Maximum requests per sender per day: 3
Maximum total jobs per day: 10
HTML attachment threshold: 200000
Retry limit: 5
Research timeout: 1800 seconds
Plain-text attachments: enabled
```

## Database migration and rollback

Normal startup creates `email_research_jobs` and adds the endpoint
`model_profile` plus task-run `result_status`/`result_summary_json` columns.
The job table contains request metadata, sanitized request content, lifecycle
state, leases, retries, research identifiers, report identifiers, and
idempotent delivery evidence. It does not store account passwords.

To apply only the job-table migration:

```bash
venv/bin/python scripts/migrate_email_research.py
```

Back up the data directory before rollback. Disable automation and stop the app,
then:

```bash
venv/bin/python scripts/migrate_email_research.py --rollback
```

Rollback refuses to delete job history. Export or back it up before using
`--force`. The additive `model_profile` and task result columns are intentionally
left in place because SQLite cannot safely drop them on older supported
versions; old application code ignores them. Restoring the pre-upgrade database
backup is the complete rollback.

## Rebuild and upgrade

```bash
docker compose down
docker compose up -d --build
docker compose logs -f odysseus
```

GPU installations use the same override file they used previously, for example:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu-nvidia.yml up -d --build
```

Do not start a second email worker against the same SQLite volume. Set
`ODYSSEUS_INPROCESS_EMAIL_RESEARCH=0` only when intentionally disabling it.

## Ornith and llama.cpp

The model profile layer is centralized in `src/model_profiles.py`. All
non-Ornith models use the unchanged default profile. Ornith can be detected by
model id or forced/disabled per endpoint in **Settings → AI → Model capability
profile**.

The Ornith profile keeps `reasoning_content` separate from visible output,
accepts only real OpenAI `tool_calls`, validates the offered name and JSON
schema, and never parses prose or Markdown fences into actions. It exposes a
small intent-specific tool set and retries one malformed structured call with a
compact schema correction. Document calls include the exact active document id
and version, validate structured operations/ranges, and use optimistic
concurrency so a failed or stale edit preserves the current document.

Use **Test tool calls** on an Ornith endpoint. It offers one harmless forced
function and requires a real `tool_calls` object. A warning means the endpoint
emitted prose/pseudo-calls or invalid JSON. Tool-capable llama.cpp serving
requires a compatible tool-aware chat template and Jinja/tool-call support;
Odysseus does not silently rewrite arbitrary responses.

## Manual test plan

Use a disposable mailbox and fake/non-sensitive research topics.

1. Keep automation disabled, configure the mailbox, allow only your test sender,
   run the mock lifecycle, and send the test acknowledgement.
2. Enable automation and send one `Research:` message. Confirm one database/UI
   job and one acknowledgement with correct `In-Reply-To` and `References`.
3. Click **Run now** while a chat tab is active. Confirm immediate progress.
4. Restart Odysseus during research. Confirm the same job/research reference
   resumes and only one final reply arrives.
5. Resend the exact raw message/Message-ID. Confirm no second job or reply.
6. Send auto-reply, list, no-reply, unauthorized, empty, over-size, and
   over-rate messages. Confirm ignored/rejected states without research starts.
7. Simulate a temporary SMTP/model outage and confirm `retry_wait`, bounded
   backoff, and eventual delivery. Simulate permanent authentication failure
   and confirm `failed` with no busy loop.
8. Request a report above the attachment threshold. Confirm the body summary,
   sanitized `report.html`, working citation links, and absence of scripts or
   remote trackers.
9. Include prompt-injection text asking for secrets, shell access, settings
   changes, or another recipient. Confirm it remains quoted research subject
   matter and no application tool executes.
10. For Ornith, run the endpoint diagnostic, then create and edit a disposable
    document. Confirm version increments and that a stale edit is rejected
    without modifying the document.

## Known operational risks

- IMAP Sent-folder reconciliation depends on the provider exposing sent mail
  and preserving Message-ID. When it cannot, Odysseus suppresses a possible
  duplicate and requires operator review; delivery may remain incomplete.
- IMAP polling scans a bounded recent UID window. An inbox receiving more than
  that window between polls can delay discovery; deploy with an appropriate
  interval and volume limit.
- HTML sanitization intentionally removes advanced styling and embedded media.
- Process-local cycle locks plus database leases are designed for the supported
  single application process. Multi-process SQLite deployments are unsupported.
- Provider-specific threading presentation varies, although the standard
  `Message-ID`, `In-Reply-To`, and `References` headers are preserved.

