# Agent reachouts — administrator deployment guide

## 1. Environment variables (`/opt/offsight-bot/app/.env`)

| Variable | Default | Meaning |
|---|---|---|
| `REACHOUT_SHEET_ID` | blank | Reachout spreadsheet ID. **Blank disables the feature entirely**: no loop, buttons, or commands. |
| `REACHOUT_CHANNEL_CATEGORY_ID` | blank | Category where `reachout-<first name>` channels are created. Use a private category; in test mode, a test category. |
| `REACHOUT_TEST_MODE` | `true` | Anything other than `false`/`0`/`no`/`off` keeps test mode on. |
| `REACHOUT_TEST_ALLOWED_USER_IDS` | blank | Comma-separated verified work emails and/or Discord user IDs. In test mode only reachouts whose agent **and** lead are both listed are sent, and only listed leads receive DMs. |
| `REACHOUT_POLL_SECONDS` | `60` (minimum 30) | Background pass: overdue reminders and Discord job retries. Never touches Sheets; only `/reachout_poll` does. |
| `REACHOUT_BATCH_SIZE` | `10` (1–50) | Jobs per claim. `/reachout_poll` sends every ticked row regardless. |
| `REACHOUT_ERROR_CHANNEL_ID` | `FORM_ERROR_CHANNEL_ID` | Private channel for technical alerts (kind and reference only). The bot refuses to use a channel visible to `@everyone`. |

Existing variables are reused: `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`,
`GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SHEET_ID`, and `GOOGLE_SHEET_TAB` (the roster).

## 2. Discord application and permissions

- **Intents:** Server Members, already enabled for access sync. It is required for audience checks
  and member lookups. No Message Content intent is needed.
- **Bot role permissions**, in addition to the current View Channels, Send Messages, Read Message
  History, Embed Links, Manage Roles, Manage Channels, and Use Application Commands:
  - **Create Public Threads**
  - **Send Messages in Threads**
  - **Manage Threads**

  The bot can grant only permissions it holds. Manage Roles is needed to edit channel overwrites,
  and Manage Channels to create agent channels. Give these at server level or on the reachout
  category. Administrator is not required.
- Slash commands (`/reachout_status`, `/reachout_poll`, `/reachout_retry_failed`) sync to the guild
  at startup like the existing ones. They are usable by Discord administrators and `Ops Bot Admin`.
- An agent with server Administrator can type anyway, because Discord bypasses overwrites for
  administrators. Do not test the read-only behavior with an admin account.

## 3. Database migration

There are no manual migrations. On startup `ReachoutService.init()` runs `CREATE TABLE IF NOT EXISTS`
for `reachout_kv`, `reachout_agents`, `reachouts`, `reachout_obligations`, `reachout_responses`,
`reachout_nudges`, `reachout_events`, and `reachout_jobs` in the existing `data/opsbot.db`. Existing
tables are untouched. Back up first:

```bash
sudo -u offsightbot cp /opt/offsight-bot/data/opsbot.db /opt/offsight-bot/backups/opsbot-$(date +%F-%H%M).db
```

## 4. Worker and scheduler

Nothing new to install. The reachout loop runs inside `offsight-bot.service` (`ReachoutLoop`, every
`REACHOUT_POLL_SECONDS`) and only sends overdue reminders and retries Discord jobs; it never reads or
writes Sheets. Deadlines and nudges are stored in SQLite, so a stopped bot catches up on the first
pass after restart: overdue items get their single nudge then, and pending jobs resume. Run
`/reachout_poll` after a restart to bring Sheets up to date.
The service's existing `flock` keeps it to one instance, which the delivery guarantees rely on.

## 5. Test mode and staged rollout

1. **Test server or test category.** Point `REACHOUT_CHANNEL_CATEGORY_ID` at a private test
   category, and `REACHOUT_SHEET_ID` at a **test spreadsheet** created by `reachout-sheet-setup`.
2. Keep `REACHOUT_TEST_MODE=true` and set `REACHOUT_TEST_ALLOWED_USER_IDS` to two or three test
   accounts that are verified (`Verify Work Email`), Active in the roster, and **not** server
   administrators.
3. Add those accounts to `Reachout Agents`, with one test account as another's lead.
4. Run the [acceptance checklist](acceptance-tests.md).
5. For production, switch `REACHOUT_SHEET_ID` to the production spreadsheet and the category to the
   production category. Set `REACHOUT_TEST_MODE=false` only when ready, then restart.

## 6. Deploy and verify

On the server, after pulling the change into `/opt/offsight-bot/app`:

```bash
cd /opt/offsight-bot/app
python3 -m venv /tmp/opsbot-dev && /tmp/opsbot-dev/bin/pip install -r requirements-dev.txt
/tmp/opsbot-dev/bin/python -m pytest -q          # development tests, never in the production .venv
sudo -u offsightbot cp /opt/offsight-bot/data/opsbot.db /opt/offsight-bot/backups/opsbot-$(date +%F-%H%M).db
sudoedit /opt/offsight-bot/app/.env              # add the REACHOUT_* variables
sudo -u offsightbot .venv/bin/python main.py reachout-sheet-setup
sudo systemctl restart offsight-bot.service
journalctl -u offsight-bot.service -n 100 --no-pager
```

No new Python packages are required. Production `.venv` already has discord.py 2.7.1 and
gspread 6.2.1.

Verify:

- The journal shows `Logged in as …` and no `Reachout Sheets poll failed`.
- `/reachout_poll` in Discord answers `Poll complete: {…}` with no `poll_error`.
- `Reachout Config` `bot_status` shows `OK` for every key.
- `Reachout Agents` shows `directory_status` `OK` for the test agents.
- An existing `/workflow` submission and the `Verify Work Email` button still work.

## 7. Recovery and reconciliation

| Situation | Action |
|---|---|
| Sheets looks wrong or a bot column was edited | `/reachout_poll` runs a full reconcile: every queue row's bot cells and every record row are compared with SQLite and rewritten. |
| Jobs failed permanently (`FAILED`, alerted), e.g. lead DMs closed with no fallback | Fix the cause (lead enables DMs, or set a valid `fallback_channel_id`), then run `/reachout_retry_failed`. |
| Bot was down | Start it. Due nudges and pending jobs run on the first background pass; run `/reachout_poll` to refresh Sheets. |
| Crash during a send | Automatic. The next attempt searches recent channel history for the record ID before re-posting (see the overview's limits). |
| A reachout's Discord message, thread, or channel was deleted | Threads are recreated automatically. A deleted message or channel is permanent for that reachout: data stays in SQLite and Sheets. Issue a new reachout if needed. The agent's next reachout recreates their channel. |
| A queue row was deleted | Add a row containing only the `reachout_id`. The bot refills the bot columns. |
| Inspect one reachout | `/reachout_status <reachout_id>`. |
| Database lost or corrupted | Stop the bot and restore the latest `opsbot.db` backup. Reachouts released after that backup show `UNKNOWN_ID` and are not re-sent. Their Discord messages exist, but their buttons answer "not available". Re-issue them as new rows after confirming with the agent. |

Useful read-only queries (run as `offsightbot`, `python3 -c "import sqlite3; …"`):

```sql
SELECT kind, status, count(*) FROM reachout_jobs GROUP BY 1, 2;
SELECT kind, ref_id, attempts, last_error FROM reachout_jobs WHERE status != 'DONE';
SELECT reachout_id, dispatch_state, workflow_status, status_detail FROM reachouts ORDER BY updated_at DESC LIMIT 20;
```

## 8. Rollback

- **Disable the feature:** clear `REACHOUT_SHEET_ID` and restart. Polling, nudges, buttons, and
  commands stop. Existing reachout buttons then show Discord's "This interaction failed". The
  `reachout_*` tables stay in place and are harmless. Forms and access sync are unaffected.
- **Revert the code:** `git revert` the reachout commit and redeploy. The tables can remain. Drop
  them only if you are sure you no longer need the history.
- Re-enabling later resumes from SQLite, including overdue nudges.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `processing_status` = `BLOCKED`, "Test mode" | Add both agent and lead IDs to `REACHOUT_TEST_ALLOWED_USER_IDS`, or turn test mode off for production. |
| `BLOCKED`, "has not completed Discord work-email verification" | The agent must click **Verify Work Email**. |
| `BLOCKED`, "not a member of the Discord server" | The agent or lead left. Re-invite them or reassign the lead. |
| `BLOCKED`, "visible to @everyone" or "visible to N other member(s)" | Remove broad overwrites from that channel (roles, other members), or clear `existing_channel_id` to let the bot create a dedicated channel. |
| `BLOCKED`, "REACHOUT_CHANNEL_CATEGORY_ID is not a category" | Set it to a category ID and restart. |
| `RETRYING` with `Forbidden` / `Missing Permissions` | The bot role lacks a permission from §2 on the category or channel, or sits below a role it must manage. |
| `lead_notify_status` = `FAILED`, "DM failed (Forbidden)" | The lead blocks DMs from server members or the bot. Ask them to allow DMs, or set a `fallback_channel_id` visible only to that lead (and administrators). Then run `/reachout_retry_failed`. |
| `lead_notify_status` = `FAILED`, "fallback channel rejected" | The fallback channel is visible to others. Restrict it. |
| `Reachout Sheets poll failed` alerts, `SheetSchemaError` | A tab or header is missing or renamed. Run `reachout-sheet-setup`. |
| Poll failed with `APIError` 403 | The spreadsheet is not shared with the service account as Editor. |
| Poll failed with `APIError` 429 | The Google quota was exceeded. Wait a minute and run `/reachout_poll` again. |
| Discord IDs show as `1.23457E+17` | The column was not plain text. Format it as plain text (Format → Number → Plain text) and re-enter the ID. |
| A form will not open ("This interaction failed") | The bot is down, or the feature is disabled. Old forms opened before a restart must be reopened. |
