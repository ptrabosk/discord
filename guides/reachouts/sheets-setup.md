# Agent reachouts — Google Sheets setup and data dictionary

## 1. Create the spreadsheet and share it

1. Create a **new** spreadsheet for reachouts. Do not reuse the `Discord Access` roster, which
   should stay Viewer-only for the bot.
2. Share it with the service account's email (the `client_email` in the bot's
   `credentials.json`) as **Editor**. Share it with operators as Editor and with nobody else.
   Reachout content is private.
3. Put its ID (the part of the URL between `/d/` and `/edit`) in `REACHOUT_SHEET_ID` in `.env`.
4. On the server, as an administrator, run:

   ```bash
   cd /opt/offsight-bot/app
   sudo -u offsightbot .venv/bin/python main.py reachout-sheet-setup
   ```

   (`main.py` loads `.env` from the app directory itself.)

   This only talks to Google Sheets. It never logs in to Discord and cannot message anyone. It:
   - Creates any missing tab with its headers. On existing tabs it appends missing headers at the
     right end and never reorders, renames, or deletes columns or rows.
   - Freezes and bolds the header row and greys bot-managed headers.
   - Formats Discord-ID columns as plain text.
   - Adds the dropdowns listed below.
   - Protects bot-managed columns and the four bot-only tabs so only the service account (and the
     spreadsheet owner) can edit them. Operator header rows get a warning-only protection.
   - **Only when it creates a tab**, writes default config rows, one inactive `example.agent@example.invalid` agent, and eight unticked sample reachouts. Example
     addresses are rejected by validation even if someone ticks them.
   - Hides `Reachout Responses`, `Reachout Obligations` and `Reachout Events` (the detailed audit
     trail). `Reachout History` is the everyday view.
   - **Moves an old dropdown-style queue aside.** If `Reachout Queue` still has a `dispatch_action`
     column, it is renamed to `Reachout Queue (old <date>)` as a backup and a new checkbox queue is
     created. Rows not yet sent are copied over (READY becomes ticked, DRAFT unticked); sent and
     cancelled rows stay in the backup only.

   Re-running it is safe. To throw the reachout tabs away and build them fresh, add `--recreate` (asks you to type DELETE; other tabs are untouched).

Secrets (bot token, service-account key, SMTP) never go in the spreadsheet. Test mode is also
controlled only from `.env`.

### Rules that apply to every tab

- Columns are found **by header name**. You may reorder columns or add your own extra columns
  (they are ignored and never overwritten). Do not rename the listed headers.
- Rows are identified by `reachout_id` / `agent_email`, never by row number. You may sort, filter, and
  move rows.
- Grey columns are written by the bot. Edits to them are overwritten on the next `/reachout_poll`.
- The bot writes with `RAW` input: text that starts with `=` is stored literally, never as a
  formula. Discord IDs are written as text. Type Discord IDs into plain-text-formatted cells (setup
  does this), or Sheets will round them, for example to `1.23457E+17`, and the bot will reject them.
- Timestamps written by the bot are ISO 8601 in the `Reachout Config` timezone with the UTC offset,
  for example `2026-11-04T14:00:00-05:00`. SQLite stores UTC.

## 2. `Reachout Queue`

The queue only holds what still has to be sent. Fill a row, tick `send`, run `/reachout_poll` in
Discord, and delete the row once it says `SENT`. What was sent, and every reply, is kept in
**Reachout History** (§4) and in the bot's database, so deleting queue rows loses nothing.

### Operator columns

| Column | Purpose | Type | Required | Allowed values | Default | Example | Validation |
|---|---|---|---|---|---|---|---|
| `send` | Release control: ticked rows are sent on the next `/reachout_poll` | Checkbox | Yes | Ticked / unticked | Unticked | ☑ | Nothing reads the sheet automatically; only `/reachout_poll` does. |
| `agent_email` | Who receives it | Text | Yes | An `agent_email` in `Reachout Agents` (case-insensitive) | — | `jordan@company.com` | Must have `directory_status` OK. `…@example.invalid` is always rejected. |
| `reachout_type` | Category | Dropdown | Yes | `Weekly performance`, `Agent behaviors`, `Opt outs`, `Flagged messages`, `Concerning` | — | `Opt outs` | Case-insensitive. |
| `title` | Heading shown to the agent | Text | Yes | 1–150 characters | — | `Opt-out handling on 11/3` | |
| `message_body` | The reachout content, delivered exactly as written | Text | Yes | 1–3500 characters. Discord markdown renders. | — | `On 11/3 the customer replied STOP…` | Never rewritten by the bot. |
| `reference_urls` | Optional source links | Text | No | Up to 5 `http(s)://` links, separated by new lines, commas, or spaces, each ≤ 400 characters | Blank | `https://help.example/ticket/123` | Shown to the agent as Link 1, Link 2, … |
| `due_at_override` | Replace the default seven-day deadline | Text | No | `YYYY-MM-DD HH:MM`, 24-hour, in the `Reachout Config` `timezone` | Blank | `2026-11-06 17:00` | Must be in the future when sent. Rejected on Weekly performance. |

A written response is required for every type except Weekly performance, which has no buttons
and requires nothing.

### Bot-managed columns (grey, protected)

| Column | Meaning | Values |
|---|---|---|
| `reachout_id` | Stable identity, assigned when the row is sent | `RO-YYYYMMDD-XXXXXXXX`. Never type or copy one. |
| `processing_status` | Where the row is | `WAITING` (not sent), `SENDING`, `SENT` (delete the row whenever you like), `ERROR` (read `status_detail`, fix the row, run `/reachout_poll` again) |
| `status_detail` | Reason or next step | Text |

### Example rows (two per type)

Setup inserts eight unticked rows for `example.agent@example.invalid`, which can never be sent. The second
row of each type ("(formatted)") shows Discord formatting typed into the cell as plain characters:

| Type | Plain example | Formatted example |
|---|---|---|
| Weekly performance | Week of Jan 5 (positive) | Week of Jan 12 (mixed): bold headings, bullet list, italic, underline, quote |
| Agent behaviors | Behavior follow-up | Tone in chat: bold labels, italic, strikethrough |
| Opt outs | Opt-out handling | Missed STOP request: bold, numbered list, italic, quote |
| Flagged messages | Flagged message review (`due_at_override`) | Flagged wording: quote, italic, underline |

**Formatting in `message_body`:** Sheets' own bold/italic buttons are not carried to Discord. Type Discord
markdown instead: `**bold**`, `*italic*`, `__underline__`, `~~strikethrough~~`, `> quote`, `- bullet`,
`1. numbered`. Start a new line inside a cell with Ctrl+Enter (Cmd+Enter on Mac).

To use one, copy it to a new row, change `agent_email` to a real agent, rewrite the content, and leave
`send` unticked until it is finished.

## 3. `Reachout Agents`

One row per agent. The agent's name and active roster status come from the existing
`Discord Access` tab, matched by email, and their Discord ID comes from their completed
`Verify Personal Email` step. This tab does not duplicate the roster.

| Column | Purpose | Type | Required | Allowed values | Default | Edited by | Example | Validation |
|---|---|---|---|---|---|---|---|---|
| `agent_email` | The agent's identity; links to the `Discord Access` roster | Text | Yes | Roster email | — | Operator | `jordan@company.com` | Must be in the roster with a Name. |
| `active` | Whether reachouts may be sent | Dropdown | Yes | `TRUE`, `FALSE` | — | Operator | `TRUE` | Effective only if the roster's `Active` is also TRUE. |
| `team` | Optional grouping | Text | No | Any | Blank | Operator | `Concierge - Josh` | Informational. |
| `timezone_override` | Agent's IANA timezone for their deadlines and their Discord displays | Text | No | e.g. `America/Chicago`, `Asia/Manila` | Blank, uses `Reachout Config` `timezone` | Operator | `America/Chicago` | Must be a valid IANA name. |
| `fallback_channel_id` | Restricted channel used only if the lead's DMs fail | Plain text | No | 17–20 digits | Blank | Admin | `123456789012345678` | Used only if no one but the lead, the bot, and administrators can view it. |
| `existing_channel_id` | Reuse an existing private channel instead of creating `reachout-<first name>` | Plain text | No | 17–20 digits | Blank | Admin | `123456789012345678` | Must be a text channel whose audience is only the agent, the lead, the bot, and administrators. |
| `agent_name` | From the roster `First Name` + `Last Name` | Bot | — | | | Bot | `Jordan Lee` | |
| `agent_discord_user_id` | From verification | Bot | — | | | Bot | | |
| `lead_email` | The roster's `Team Lead Email` for this agent | Bot | — | | | Bot | `nadir@company.com` | Change the lead in `Discord Access`, not here. |
| `lead_discord_user_id` | That lead's Discord ID, from their verification | Bot | — | | | Bot | | The lead must have completed Verify Personal Email. |
| `private_channel_id` | The channel actually used | Bot | — | | | Bot | | Filled on first delivery. |
| `directory_status` | Directory validity | Bot | — | `OK`, `INVALID`, `BLOCKED`, `INACTIVE`, `NO_LEAD` (blank Team Lead Email, e.g. Leadership: never sent), `EXAMPLE` | | Bot | `OK` | |
| `directory_error` | Why it is not OK | Bot | — | Text | | Bot | `The agent has not completed Discord personal-email verification.` | |
| `directory_synced_at` | Last time the bot changed this row | Bot | — | Timestamp | | Bot | | |

Deleting an agent row marks the agent inactive (their channel access is hidden) but keeps history
and the last known lead.

## 4. Bot-only record tabs (fully protected; append-only rows, status cells update)

### `Reachout History`: one row per sent reachout (the everyday view)

Built from the bot's database, so it is complete however many queue rows you delete. Updated, and
every row compared, only when someone runs `/reachout_poll`.

| Column | Meaning |
|---|---|
| `reachout_id` | The reachout |
| `sent_at`, `agent_email`, `agent_name`, `reachout_type`, `title`, `message` | What was sent, to whom, and when |
| `status` | `AWAITING_AGENT`, `OVERDUE`, `AWAITING_TL`, `COMPLETE`, `SENT` (Weekly performance), or `CANCELLED` / `RESOLVED` for older reachouts |
| `due_at` | When the open required item is due |
| `agent_reply_1` | The agent's first reply, with name and time |
| `needs_tl_reply` | `TRUE` while the agent's latest reply has not been replied to or acknowledged by the team lead |
| `tl_reply` | The team lead's first reply after `agent_reply_1` |
| `agent_reply_2` | The agent's next reply after `tl_reply` |
| `later_replies` | Everything after that, in order |
| `images` | Number of images the agent attached |
| `thread_url` | Link to the reachout thread |
| `synced_at` | Last write |

The tabs below are the detailed audit trail. Setup hides them; unhide them from the sheet's tab menu.

### `Reachout Responses`: every agent submission and lead reply

| Column | Meaning / values |
|---|---|
| `response_id` | `RSP-YYYYMMDD-XXXXXXXX` |
| `reachout_id`, `agent_email` | Which reachout and agent |
| `obligation_id` | The obligation this agent submission satisfied (blank for optional responses and lead rows) |
| `actor_discord_user_id`, `actor_name` | Who submitted it. For leads, the name is their server display name at the time. |
| `actor_role` | `AGENT` or `LEAD` |
| `response_type` | `AGENT_ACKNOWLEDGMENT`, `AGENT_ACKNOWLEDGMENT_WITH_RESPONSE`, `AGENT_RESPONSE`, `LEAD_REPLY`, `LEAD_REPLY_RESPONSE_REQUIRED` |
| `acknowledgment` | `TRUE` if this submission satisfied the original acknowledgment |
| `text` | Submitted text, stored literally. Blank means acknowledgment only. |
| `submitted_at` | Timestamp |
| `in_reply_to_response_id` | For lead replies: the agent response replied to |
| `created_obligation_id` | For `LEAD_REPLY_RESPONSE_REQUIRED`: the new obligation |
| `review_status` | Agent rows: `UNREVIEWED`, `REPLIED`, `ACKNOWLEDGED`. Lead rows: `N/A`. |
| `reviewed_by_discord_user_id`, `reviewed_at` | Latest review action |
| `thread_message_id`, `thread_post_status` | `QUEUED`, `RETRYING`, `POSTED`, `FAILED` |
| `lead_notify_status` | Agent rows: `QUEUED`, `RETRYING`, `DM_SENT`, `FALLBACK_SENT`, `FAILED`, `FAILED_NO_LEAD` |
| `lead_notified_discord_user_id` | Lead who received the DM |
| `last_error` | Last delivery error |
| `synced_at` | When this row last changed in Sheets |

### `Reachout Obligations`

| Column | Meaning / values |
|---|---|
| `obligation_id` | `OBL-YYYYMMDD-XXXXXXXX` |
| `reachout_id`, `agent_email` | |
| `obligation_type` | `ACKNOWLEDGMENT` or `FOLLOW_UP_RESPONSE` |
| `text_required` | `TRUE` / `FALSE` (always TRUE for follow-ups) |
| `requested_by_discord_user_id`, `requesting_response_id` | Lead and lead reply that created a follow-up (blank for the initial acknowledgment) |
| `required_agent_discord_user_id` | The only user who can satisfy it |
| `status` | `AWAITING_DELIVERY`, `PENDING`, `SATISFIED`, `SUPERSEDED`, `CANCELLED`, `CLOSED_RESOLVED` |
| `overdue` | `TRUE` when PENDING and past due. Refreshed on each `/reachout_poll`. |
| `created_at`, `delivered_at`, `due_at` | Timestamps |
| `satisfied_by_response_id`, `satisfied_at` | |
| `superseded_by`, `closed_at` | |
| `next_nudge_at`, `nudge_count` | Next automatic reminder, and reminders so far |
| `reminder_count`, `last_reminder_at`, `last_reminder_by` | Agent reminders sent at a lead's request |
| `synced_at` | |

### `Reachout Events`: audit and reminder log

| Column | Meaning / values |
|---|---|
| `event_id` | `EVT-YYYYMMDD-XXXXXXXX` |
| `reachout_id`, `response_id`, `obligation_id` | Related records |
| `event_type` | `ID_ASSIGNED`, `QUEUED`, `VALIDATION_FAILED`, `DISPATCH_BLOCKED`, `HELD_CHANGED`, `WITHDRAWN`, `CANCELLED_BEFORE_SEND`, `DELIVERED`, `THREAD_CREATED`, `THREAD_RECREATED`, `AGENT_SUBMITTED`, `LEAD_NOTIFIED`, `LEAD_NOTIFY_FAILED`, `LEAD_REPLIED`, `LEAD_ACKNOWLEDGED`, `LEAD_NUDGE_QUEUED`, `LEAD_NUDGED`, `LEAD_NUDGE_FAILED`, `NUDGE_SUPPRESSED`, `NUDGE_SNOOZED`, `STANDARD_REMINDER`, `CUSTOM_REMINDER`, `CANCELLED`, `RESOLVED`, `LEAD_REASSIGNED`, `QUEUE_ROW_MISSING`, `ORIGINAL_MESSAGE_MISSING`, `AGENT_CHANNEL_RECREATED` |
| `actor_role`, `actor_discord_user_id` | `AGENT`, `LEAD`, `OPERATOR` (via Sheets), or `BOT` |
| `occurred_at` | Timestamp |
| `outcome` | `OK`, `REJECTED`, `WARNING`, `SKIPPED`, `FAILED`. Reminders: `QUEUED` → `POSTED`, `SKIPPED_NO_LONGER_PENDING`, or `FAILED`. |
| `discord_channel_id`, `discord_message_id` | Where it was delivered |
| `details` | Error text, or custom reminder text |
| `synced_at` | |

## 5. `Reachout Config`

Columns: `key`, `value`, `description`, and `bot_status` (bot-managed: `OK`,
`OK: blank, using default`, `INVALID: … Using the default.`, or `IGNORED: unknown key`).

| key | Meaning | Default | Valid values |
|---|---|---|---|
| `timezone` | Operational timezone for deadlines, `due_at_override`, and Sheets timestamps | `America/New_York` | IANA name |
| `business_weekdays` | Business days | `MON,TUE,WED,THU,FRI` | Comma-separated `MON`…`SUN` |
| `default_deadline_business_days` | Unused: required items are due 7 calendar days after delivery | `3` | 1–30 |
| `nudge_repeat_business_days` | `0` = one overdue reminder only; N = remind the agent in the thread again every N business days while still overdue | `0` | 0–30 |
| `reminder_template` | Standard reminder text | `Reminder: your {requirement} for "{title}" was due {due_at}. …` | Placeholders `{requirement}`, `{title}`, `{due_at}` only; ≤ 1500 characters |
| `acknowledgment_notice` | Text at the top of every acknowledgment form | `Submitting this form records that you received and reviewed this reachout. It does not necessarily mean you agree with its contents.` | ≤ 1500 characters |

Config changes apply to deadlines computed **after** the change. Existing due times are never
recalculated. Poll intervals, test mode, and channel settings are in `.env` (see the admin guide).

## 6. Sending reachouts and what edits do

1. Add a row and fill `agent_email`, `reachout_type`, `title`, `message_body`, and any optional fields.
2. Proofread. What is in the row when it is sent is exactly what the agent sees.
3. Tick `send`. Tick as many rows as you like.
4. Run `/reachout_poll` in Discord. Every ticked row is validated and sent; `processing_status`
   becomes `SENT`. Rows with problems show `ERROR` and are not sent.
5. Delete the `SENT` rows whenever you like.

| Situation | Result |
|---|---|
| A row is ticked but `/reachout_poll` has not been run | Nothing happens; the bot never reads the sheet on its own. |
| A row shows `ERROR` | Nothing was sent. Fix what `status_detail` says, keep `send` ticked, and run `/reachout_poll` again. |
| A ticked row is unticked before `/reachout_poll` | Not sent. |
| A sent row is edited | Discord is not changed. Send a new reachout to correct it. |
| A row is deleted before it is sent | It is never sent. |
| A sent row is deleted | Nothing changes: replies, reminders and Reachout History continue. To see its queue status again, add a row containing only that `reachout_id`. |
| Rows are deleted while `/reachout_poll` runs | Safe. The bot finds each row by `reachout_id` just before writing, so status never lands on the wrong row. |
| A row is copied, including its `reachout_id` | Both copies show `ERROR` and are ignored until the copy is deleted. |
| Cancelling or resolving a sent reachout | Not available. To withdraw one, delete its Discord message by hand. |

