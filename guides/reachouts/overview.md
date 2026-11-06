# Agent reachouts — implementation overview

Operators release reachouts from Google Sheets. The bot posts each one in the agent's permanent
private Discord channel, opens a thread for it, and collects acknowledgments and responses through
button-launched forms. It DMs the assigned team lead about every agent submission and nudges the
lead (never the agent) when a required item is overdue.

## Components

| File | Responsibility |
|---|---|
| `reachouts/domain.py` | Pure rules: the four types, row validation, payload snapshot, business-day deadlines, agent-directory evaluation, config parsing. |
| `reachouts/repository.py` | SQLite tables and every state transition, each in one `BEGIN IMMEDIATE` transaction together with the jobs it implies. |
| `reachouts/sheets.py` | Header-based reads, batched `RAW` writes, record-tab mirroring, and `reachout-sheet-setup` template creation. |
| `reachouts/service.py` | The poller, the dispatch state machine, the job runner and its Discord handlers, nudge scheduling, and interaction actions. |
| `reachouts/views.py` | Restart-safe buttons (`discord.ui.DynamicItem`), modals, message rendering, and admin slash commands. |

### Integration points with the existing bot

- `main.py`: `OpsBot.reachouts` is created only when `REACHOUT_SHEET_ID` is set. `setup_hook`
  registers the buttons and commands, `on_ready` starts `ReachoutLoop`, and startup runs
  `ReachoutService.init()` (table creation and lease release). `python main.py reachout-sheet-setup`
  builds the tabs without logging in to Discord.
- **User directory.** Agents are identified by personal email. First Name, Last Name and Active come from the existing
  `Discord Access` roster (`AccessSheet.all_records()`), and the Discord user ID comes from the
  existing `verified_users` table, the same mapping `/workflow` uses. `Reachout Agents` only adds what
  the roster lacks: lead, channel, timezone, and fallback.
- **Authentication and authorization.** It uses the same Google service account
  (`GOOGLE_SERVICE_ACCOUNT_FILE`) as the roster and forms. Admin commands use the existing
  `is_admin` check (Discord Administrator or `Ops Bot Admin`).
- **Persistence.** New `reachout_*` tables are in the existing `data/opsbot.db`. As with forms,
  SQLite is the durable source of truth and Sheets is synchronized from it.
- **Alerts.** Alerts go to the private technical-error channel (`REACHOUT_ERROR_CHANNEL_ID`, falling
  back to `FORM_ERROR_CHANNEL_ID`). They carry only an event kind and a reference ID, never
  reachout content.
- **Deployment.** The loop runs inside the existing `offsight-bot.service`, whose `flock` ensures one
  instance. No new service, timer, or Python dependency is added.

## Processing flow

Sheets are read and written **only when an admin runs `/reachout_poll`**, which calls
`ReachoutService.tick()` and runs every step below. There is no automatic Sheets check. Separately,
every `REACHOUT_POLL_SECONDS` (default 60) `ReachoutLoop` runs only steps 7 and 8
(`background_tick()`), which never touch Sheets. A failure in one step never blocks the others.

1. **Read.** One batched Sheets read covers `Reachout Queue`, `Reachout Agents`,
   and `Reachout Config`, with formatted values, so formula results are read like typed text.
2. **Config.** The bot parses config, falls back to defaults for invalid keys, and
   writes each key's status to `bot_status`. The last good config is kept in SQLite.
3. **Directory.** The bot validates `Reachout Agents` against the roster (read fresh on each poll)
   and Discord membership. It detects lead reassignment and
   queues channel-permission updates.
4. **Queue.** Each row with `send` ticked and no `reachout_id` is re-read
   on its own. If it is unchanged, the bot records a new ID in SQLite, writes it to the row, and
   processes that row in the same pass; every row with a known ID goes through the dispatch state
   machine below.
5. **Write back.** Only bot-managed cells whose value differs are written, in batched `RAW`
   updates. Queue cells are located by `reachout_id` just before writing, because operators delete
   queue rows by hand.
6. **Mirror.** Changed responses, obligations, events and Reachout History rows are upserted to
   their tabs. Every `/reachout_poll` compares every row with SQLite.
7. **Nudges.** The bot creates one nudge per overdue pending obligation whose `next_nudge_at` has
   passed.
8. **Jobs.** The bot runs due Discord side effects: posts, DMs, message edits, permission updates.

Agent and lead button actions run the job queue immediately after saving, so thread posts and lead
DMs normally appear within seconds.

**Latency.** Ticked rows are delivered within seconds of `/reachout_poll`. Delivery details,
responses, History, roster and lead changes, and Config edits reach Sheets or the bot only on the
next `/reachout_poll`; between polls Sheets can be out of date, but SQLite and Discord are not.
Jobs are claimed `REACHOUT_BATCH_SIZE` (default 10) at a time. Discord rate limits are handled by
discord.py. A Sheets 429 or 5xx error fails that poll; run `/reachout_poll` again.

## State model

### Dispatch state (`reachouts.dispatch_state`, shown as `processing_status`)

```
 ticked, no ID ─▶ ID_ASSIGNED ──valid + eligible──▶ QUEUED ──▶ SENDING ──▶ DELIVERED
   (on /reachout_poll) │                             │  │         │
                    ├─ errors ─▶ INVALID ◀───┐        │  │         └─ channel / audience /
                    └─ env    ─▶ BLOCKED  ───┴─ re-checked on each /reachout_poll while ticked
                                                      │  └─ row edited ─▶ HELD_CHANGED
 unticked before delivery ─▶ WITHDRAWN  (ticking again re-validates and re-snapshots)
```

The queue shows these as `WAITING` (ID_ASSIGNED, WITHDRAWN), `SENDING` (QUEUED, SENDING), `SENT`
(DELIVERED) and `ERROR` (INVALID, BLOCKED, HELD_CHANGED, copied or unknown IDs), with the reason in
`status_detail`. `CANCELLED` exists only for reachouts cancelled before the checkbox queue.

- While a transient error is retried, `status_detail` shows "Retrying …" with
  backoff (1 minute, doubling, capped at 1 hour, ±20 % jitter, retried indefinitely and alerted
  after the third failure). Handlers raise a permanent failure only when retrying cannot help.
  Those jobs become `FAILED`, are alerted, and can be re-queued with `/reachout_retry_failed`.
- Copied and unknown IDs describe a row, not a record. Nothing is processed from such rows.
- Once `DELIVERED`, the checkbox no longer has any effect.

### Workflow status

`NOT_SENT` → `OPEN` (on delivery). Operators can no longer cancel or resolve; reachouts that were
`CANCELLED` or `RESOLVED` before the checkbox queue keep that final status. Cancelled ones have a
disabled button and reject new replies; resolved ones still allow optional agent responses and
plain lead replies, but not new required responses.

### Obligations

| Type | Created | Satisfied by |
|---|---|---|
| `ACKNOWLEDGMENT` | On delivery, for Agent behaviors, Opt outs, Flagged messages | The agent's next saved submission (with text if `text_required`) |
| `FOLLOW_UP_RESPONSE` | When a lead uses **Reply — response required** | The agent's next saved written submission |

States: `AWAITING_DELIVERY` (follow-up saved, thread post not yet confirmed) → `PENDING` → one of
`SATISFIED`, `SUPERSEDED` (a newer lead request replaced it), `CANCELLED`, or `CLOSED_RESOLVED`.

**At most one open agent obligation per reachout**, enforced by a unique index. A new lead request
supersedes the open one, and its nudges become stale. Each form records which obligation it was
opened for. If that changed before submission, nothing is saved and the agent's text is shown back
so they can resubmit.

Weekly performance has no initial obligation and therefore no overdue nudge. Only a lead's explicit
**Reply — response required** creates one.

### Agent submissions and lead review

Each submission is one append-only `reachout_responses` row, unique on the Discord interaction ID,
so a replayed interaction never duplicates while a deliberate new submission always creates a new
row. `review_status` is `UNREVIEWED`, then `REPLIED` or `ACKNOWLEDGED`, and applies only to that
one response. Lead replies are their own rows (`actor_role` = `LEAD`).

### Nudges

`QUEUED` → `SENT` → `HANDLED` (reminder sent or snoozed) or `STALE` (the agent responded, the
obligation closed, or the lead was reassigned). Delivery problems end in `FAILED`. Stale and
handled nudge DMs are edited to disable their buttons. Every button also re-checks state when
clicked, so an old DM can never act.

## Source of truth

| Data | Authoritative system | Notes |
|---|---|---|
| Operator inputs (`send`, content) | Sheets `Reachout Queue` | Read and acted on only by `/reachout_poll`. Content is snapshotted at release; rows may then be deleted. |
| Delivered content and conversation | SQLite `reachouts.snapshot_json`, `reachout_responses` | Mirrored to `Reachout History`. Later row edits never change it. |
| Agent directory inputs (channel, timezone, fallback, active) | Sheets `Reachout Agents`; the lead is the roster's `Team Lead Email` | Cached in `reachout_agents`. Button authorization uses this cache (as of the last `/reachout_poll`). |
| Agent name, roster Active | Sheets `Discord Access` | Existing roster. |
| Agent Discord ID | SQLite `verified_users` | Existing verification flow. |
| Reachout IDs, dispatch and workflow state, obligations, responses, nudges, events, jobs | SQLite | Mirrored to Sheets. Edits to mirrored cells are overwritten. |
| Config | Sheets `Reachout Config` | Last good copy kept in SQLite (`reachout_kv`). |
| Test mode, intervals, category, alert channel | `.env` | Deliberately not in Sheets, so a sheet editor cannot turn test mode off. |
| Discord messages | Discord | The bot never deletes them. |

## Delivery guarantees and their limits

There is no exactly-once delivery across Discord and Google APIs. The bot provides
**at-least-once processing with duplicate suppression**:

- Every state change and the jobs it implies commit in one SQLite transaction.
- Before any Discord send, the job is marked `in_flight` in SQLite. The mark is cleared only when
  Discord definitely refused (4xx other than 429). If the process dies or the request times out
  after Discord accepted the message, the next attempt searches the last 100 messages in that
  channel, thread, or DM for the record ID at the end of the bot's embed footer, and adopts that
  message instead of posting again.
  - **Limitations:** a matching message older than the last 100 is not seen. A message someone
    deleted is re-posted. If a lead's DM attempt timed out and the retry fell back to the fallback
    channel, the lead could see the notice twice.
- Jobs are leased (5 minutes) so overlapping runs cannot take the same job. Leases are released at
  startup because systemd's `flock` allows one instance. **Running two bot processes against one
  database or on two hosts is not supported.**
- **Sheets has no compare-and-set.** A new ID is written only after re-reading that row and
  confirming it is the same ticked row without an ID. If the row is sorted in the second between
  re-read and write, the ID can land on a different row. That row's content fingerprint then
  differs from the one recorded at assignment, so it is held (`HELD_CHANGED`) instead of sent.
  Status cells are located by `reachout_id` just before writing, so sorting or deleting rows only
  risks a misplaced write in the instant between that lookup and the write; every poll compares every
  known row and corrects it.

## Discord model

- **Channels.** There is one channel per agent, `reachout-<first name>`, created in
  `REACHOUT_CHANNEL_CATEGORY_ID` on first delivery, or an existing channel named by
  `existing_channel_id`. The bot sets only these permission overwrites, merging into whatever
  overwrite already exists:
  - `@everyone`: View Channel denied.
  - Agent: View Channel and Read Message History allowed; Send Messages, Send Messages in Threads,
    Create Public Threads, and Create Private Threads denied. View is turned off while the agent is
    inactive or removed from `Reachout Agents`.
  - Assigned lead: View, Read History, Send Messages, Send Messages in Threads.
  - Bot: View, Send, Send in Threads, Create Public Threads, Manage Threads, Embed Links, Read History.
  - A previous lead's member overwrite is removed on reassignment.

  Other overwrites (roles, other members) are never changed. If they let anyone other than the
  agent, the lead, the bot, or server administrators view the channel, the bot refuses to post and
  shows why. No broad team-lead role is granted.
- **Threads.** One thread per reachout is attached to the original message. It is a public thread
  inside the private channel, so its audience is the channel's audience, and the bot re-checks that
  before every post. Threads auto-archive after 7 days. The bot un-archives before posting and
  never locks threads; read-only access comes from permissions. A deleted thread is recreated on
  the original message when next needed. If the original message or channel is deleted, posts for
  that reachout fail permanently and are alerted, and saved data stays in SQLite and Sheets.
- **Buttons.** Buttons and modals work for users without Send Messages, because component
  interactions require only that the user can see the message. Modals use Discord's Label and Text
  Display components (discord.py ≥ 2.6). Custom IDs carry the record ID (`ro:agent:<reachout_id>`,
  `ro:lr:` and `ro:lrr:<response_id>`, `ro:lack:<response_id>`, `ro:rem:`, `ro:crem:`,
  `ro:snz:<nudge_id>`) and are routed after restarts. A form that was open but not submitted when
  the bot restarted must be reopened.
- **Mentions.** Every send sets `allowed_mentions` explicitly. Supplied text sits in embeds, which
  never ping. Message content contains only the bot's own `<@agent>` (or `<@lead>` for the DM
  fallback), and that is the only mention allowed.
- **Ordinary lead messages** typed in the channel or thread are **not recorded** anywhere by the
  bot. Only button and form actions are tracked. The bot does not read message content (no Message
  Content intent).
