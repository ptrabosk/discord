# Discord Ops Bot

Creates and manages the Discord operations server structure, controlled agent onboarding, and ongoing access synchronization.

## Server structure

- SETUP — public
  - initial-setup
- BOT — Ops Bot Admin and Moderator
  - errors — Ops Bot Admin only
- OPERATIONS — public to read; only Leadership and Workflows post
  - announcements
  - general
  - schedules
  - help
- PROFESSIONAL SERVICES — role locked
  - ps-announcements
  - ps-questions
  - ps-resources
- WHITE GLOVE — role locked
  - wg-announcements
  - wg-questions
  - wg-resources
- GROWTH STRATEGY — role locked
  - gs-announcements
  - gs-questions
  - gs-resources
- ORDER OPERATIONS — role locked
  - oo-announcements
  - oo-questions
  - oo-resources
- FASHION NOVA — role locked
  - fn-announcements
  - fn-questions
  - fn-resources
- CONCIERGE — role locked
  - concierge-announcements
  - team-nadir
  - team-josh
  - team-thaniel
  - shift-releases — all Concierge, Leadership, Moderator, Ops Bot Admin (QA excluded)
- WORKFLOWS — role locked
  - main — public override
  - wiw-adjustments
  - surveys
  - qa-disputes — plus QA; Leadership and Ops Bot Admin view
  - location-change — plus Leadership and Ops Bot Admin view
- QA — role locked
  - qa-announcements
  - qa-questions
  - qa-resources
- LEADERSHIP — role locked
  - operations
  - qa
  - staffing
  - reporting
  - chat
- CASUAL — public
  - chat
  - pets
  - meme-department
- REACHOUTS — Leadership and Ops Bot Admin; per-agent channels are managed by the reachouts module

`config/server_structure.json` options: `public`, `role` (full access), `also_roles` (full access to
the whole category, team channels included), `roles` / `view_roles` (extra full or view-only access),
`post_roles` (on a public category: only these roles post), and `private` (a channel that ignores
its category's permissions), and `read_only` (a channel in a public category that everyone can read but not post in).

## Google Sheet

Create a worksheet named `Discord Access` with these headers:

Email | Attentive Email | Concierge Email | First Name | Team Lead Email | Last Name | Active | Professional Services | White Glove | Growth Strategy | Order Operations | Fashion Nova | Concierge | Workflows | Leadership | QA | Verified

The bot finds columns by header name, so column letters can change. `Attentive Email` and `Concierge Email` are optional; disputes and timesheet adjustments use `Concierge Email` and fall back to the personal email when it is blank. The bot sets `Verified` to TRUE when a person completes Verify Personal Email. Verify Personal Email matches only the `Email` column, and access sync does the same. If an agent's `Email` changes, the next access sync removes their managed roles until they verify again with the new address. An address that is not on an active row, or that is already linked to another Discord account, is refused; each refusal is logged and written to `audit_log` with its reason.

Boolean access fields accept TRUE/YES/1/X.

`Team Lead Email` is the person's team lead. It grants the team channel of that lead (via `team_leads` in
[`config/role_mapping.json`](config/role_mapping.json), lead email → Nadir/Josh/Thaniel), and it is
the lead who receives that agent's reachout notifications. A lead's own row gets their team channel from their
own email being listed in `team_leads`; leave their Team Lead Email blank if they have none. Leave it blank for
Leadership. Rosters that still have a `Concierge Team` column keep working until it is renamed.

`QA` grants the QA role and the QA category (`qa-announcements`, `qa-questions`, `qa-resources`),
plus every CONCIERGE channel listed in `config/server_structure.json` (`also_roles`).

Share the access-roster Sheet with the service-account email from `credentials.json` as Viewer.

## Agent forms

Verified agents start Timesheet Adjustments, Emergency Shift Releases, and Qs & Feedback
with `/workflow`. All agent prompts and confirmations are private/ephemeral;
every step after the first has a **Back** button, and a submitted form's message becomes its
confirmation with no controls left, so it cannot be submitted twice;
no public launcher message is needed. Previously posted `Submit a Form` buttons still work, but the
bot no longer posts new ones. Submissions still post review cards to configured private reviewer
channels. Identity comes from the verified-user database and the `Name`/`Email` fields in the
access roster; users never enter identity manually.

### Editing form questions and answers

Edit [`config/forms.json`](config/forms.json), then restart the bot. It contains:

- `form_names`: the top-level choices shown to agents.
- `when_i_work_names`: the two When I Work choices.
- `categories`: the Question, Concern, and Feedback category answers.
- `adjustment_types`: the Timesheet Adjustment type answers.
- `start_hours`: available start times as 24-hour numbers (for example, `14` shows as `2pm`).
- `prompts`: the wording shown at each selection step.
- `message_fields`: each form's modal title, required paragraph question, and placeholder.

Change the displayed labels and answer lists, but **do not rename the JSON keys** such as
`question` or `timesheet_adjustment`: those are stable IDs used by SQLite, Sheets, and review
logic. Discord allows at most 25 answers in a select, and labels have length limits; the bot
validates the file at startup. Adding an entirely new field or form type still requires code and
storage changes. Run `.venv/bin/python -m unittest discover -s tests -v` before restarting.

Set `GOOGLE_FORMS_SHEET_ID` to a separate spreadsheet ID. If it is blank, forms use
`GOOGLE_SHEET_ID`. Share the forms spreadsheet with the service account as **Editor**. The bot
uses the read/write `https://www.googleapis.com/auth/spreadsheets` OAuth scope; per-file Google
sharing still allows the access-roster Sheet to remain Viewer-only.

The bot creates missing forms tabs when permitted. Existing tabs must have these exact headers:

- `WIW Timesheet Adjustments`: Submission ID, Submitted At ET, Discord User ID, Concierge Email (falls back
  to the personal email), Agent Name, Adjustment Type, Start Time ET, End Time ET, Adjustment Reason, Reviewed By,
  Reviewed At ET
- `WIW Emergency Releases`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Agent Email, Agent Name, Status, Shift Start ET, Shift Start UTC, Release Reason, Reviewed By,
  Reviewed At ET, Reviewed At UTC
- `Questions Concerns Feedback`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Agent Email, Agent Name, Type, Category, Message, Status, Reviewed By, Reviewed At ET,
  Reviewed At UTC
- `Disputes`: Timestamp (Unix epoch seconds of Submitted At UTC), Submitted By (Concierge Email, falling back to the personal email), Send ID
  (the Message ID), Dispute Reasoning, Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Agent Name
- `Working Location`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Agent Email, Agent Name, Status, Start Date, End Date, New Location, Reviewed By,
  Reviewed At ET, Reviewed At UTC

Configure private Discord review channels by numeric ID:

- `WIW_REVIEW_CHANNEL_ID`
- `QUESTION_REVIEW_CHANNEL_ID`
- `DISPUTE_REVIEW_CHANNEL_ID`
- `WORKING_LOCATION_REVIEW_CHANNEL_ID`

Concern and Feedback are retired and no longer need a channel. Form types may share a channel ID. The bot refuses to post agent data to
a channel visible to `@everyone`. Set `FORM_ERROR_CHANNEL_ID` to a private operations channel for
technical form errors; it defaults to `1550994656966344714`. Error alerts contain only an event
type and reference, not agent messages or credentials. An agent is told about delivery trouble
only if both the review-channel post and Sheets write fail. Existing launcher buttons and reviewer
buttons survive bot restarts.

Timesheet Adjustments and Emergency Shift Releases open one modal with typed fields in New York time: the shift date as `MM/DD/YYYY` (within `FORM_DATE_PAST_DAYS` and `FORM_DATE_FUTURE_DAYS`, default 30 each way), the start time as `hh:mm AM/PM`, and for timesheets the end time as `hh:mm AM/PM` on the same date. Start times must be one of `start_hours` in `config/forms.json` on the hour; end times must fall on a `00`, `15`, `30`, or `45` minute. Local datetimes use `America/New_York` and are stored in both ET and UTC.

Disputes open one modal with a required Message ID and Dispute Reasoning. Working Location opens
one modal with required Start Date, End Date, and New Location. Both dates must be `MM/DD/YYYY`, real
dates, with the end date on or after the start date; they are stored in that format.

SQLite is the durable source of truth. A submission succeeds once it is stored locally. Sheets
sync retries every `FORM_SHEETS_RETRY_MINUTES` and searches column A for the Submission ID before
appending, so retries update the existing row rather than duplicating it.

## Agent reachouts

Operators release reachouts from a Google Sheet; the bot posts them in each agent's private,
read-only channel, collects acknowledgments and responses through button-launched forms, DMs the
assigned team lead, and nudges the lead when a required item is overdue. The feature is off until
`REACHOUT_SHEET_ID` is set, and starts in test mode. See [`guides/reachouts/`](guides/reachouts/):

- [Implementation overview](guides/reachouts/overview.md)
- [Google Sheets setup and data dictionary](guides/reachouts/sheets-setup.md)
- [Administrator deployment guide](guides/reachouts/admin-deployment.md)
- [Operator and team-lead guide](guides/reachouts/operator-lead-guide.md)
- [Acceptance-test checklist](guides/reachouts/acceptance-tests.md)

## Discord application setup

1. Create a Discord application and bot in the Discord Developer Portal.
2. Enable **Server Members Intent**.
3. Invite it to the target server.
4. Give it:
   - View Channels
   - Send Messages
   - Read Message History
   - Embed Links
   - Manage Roles
   - Manage Channels
   - Use Application Commands
5. The bot role must be ABOVE every role it manages.
6. Do not give Administrator unless needed temporarily for troubleshooting.

## Install

Python 3.11+ recommended.

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill `.env` and put the Google service-account JSON at `credentials.json`.

## Development tests

Use a separate development environment so the production virtual environment stays runtime-only:

```bash
python -m venv .venv-dev
.venv-dev/bin/pip install -r requirements-dev.txt
.venv-dev/bin/python -m pytest
```

## Email

Configure SMTP in `.env`. The SMTP account sends six-digit verification codes.

## Initial server creation

```bash
python main.py setup
```

This creates/reconciles roles, categories, channels, permissions, and the persistent verification button.

The setup is intended to be rerunnable. Existing matching categories/channels/roles are reused.

## Run bot

```bash
python main.py
```

## Access behavior

Google Sheets is the authorization source of truth.

1. Agent clicks **Verify Personal Email**.
2. Bot checks domain, roster presence, and Active status.
3. Bot sends a six-digit code to the personal email.
4. Agent enters the code.
5. Bot re-reads the Sheet.
6. Bot calculates desired managed roles.
7. Missing authorized roles are added.
8. Unauthorized managed roles are removed.
9. The bot periodically reconciles verified users.

If `Active` becomes FALSE or the agent disappears from the Sheet, managed access roles are removed on the next sync.

## Security

Never commit:
- `.env`
- `credentials.json`
- `data/opsbot.db`

The bot owns only roles listed in `config/role_mapping.json`. Other Discord roles are not removed by synchronization.

## Admin commands

Members with `Ops Bot Admin` or Discord Administrator can use:

- `/access_status @member`
- `/access_sync @member`
- `/reachout_status reachout_id`, `/reachout_poll`, `/reachout_sync`, `/reachout_retry_failed` (when reachouts are enabled).
  `/reachout_poll` is the only command that releases ticked rows and sends. `/reachout_sync` rewrites the Sheets tabs from the database and sends nothing. The bot never polls Sheets on its own.

Any verified agent can run `/workflow` privately.

## Notes

This is the initial access-management foundation. WIW workflows, surveys, structured team questions, QA disputes, and reporting can be added as separate modules later.
