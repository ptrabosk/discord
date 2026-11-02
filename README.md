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
its category's permissions).

## Google Sheet

Create a worksheet named `Discord Access` with these headers:

Email | First Name | Last Name | Active | Professional Services | White Glove | Growth Strategy | Order Operations | Fashion Nova | Concierge | Team Lead Email | Workflows | Leadership | QA

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

- `WIW Timesheet Adjustments`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Agent Email, Agent Name, Status, Adjustment Type, Start Time ET, Start Time UTC, End Time ET,
  End Time UTC, Adjustment Reason, Reviewed By, Reviewed At ET, Reviewed At UTC
- `WIW Emergency Releases`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Agent Email, Agent Name, Status, Shift Start ET, Shift Start UTC, Release Reason, Reviewed By,
  Reviewed At ET, Reviewed At UTC
- `Questions Concerns Feedback`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Agent Email, Agent Name, Type, Category, Message, Status, Reviewed By, Reviewed At ET,
  Reviewed At UTC
- `Disputes`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID, Agent Email,
  Agent Name, Status, Message ID, Dispute Reasoning, Reviewed By, Reviewed At ET, Reviewed At UTC
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

Timesheet Adjustments show one date menu containing today and the previous 14 days in New York.
The end date is automatically the same as the selected start date; agents choose only the end
time. Emergency Shift Releases keep the range-then-date picker, configurable with
`FORM_DATE_PAST_DAYS` and `FORM_DATE_FUTURE_DAYS` (maximum 350 selectable dates). Start times are
selected from `start_hours` in `config/forms.json`, with minutes fixed at `:00`; end times are
selected by hour and then `00`, `15`, `30`, or `45`. Local datetimes use `America/New_York` and are
stored in both ET and UTC.

Disputes open one modal with a required Message ID and Dispute Reasoning. Working Location opens
one modal with required Start Date, End Date, and New Location. Discord modals have no date input,
so both dates are free text (the placeholder suggests `MM/DD/YYYY`) and are stored as typed.

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

1. Agent clicks **Verify Work Email**.
2. Bot checks domain, roster presence, and Active status.
3. Bot sends a six-digit code to the work email.
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
- `/reachout_status reachout_id`, `/reachout_poll`, `/reachout_retry_failed` (when reachouts are enabled).
  `/reachout_poll` is the only thing that reads or writes the reachout Sheet; the bot never polls it on its own.

Any verified agent can run `/workflow` privately.

## Notes

This is the initial access-management foundation. WIW workflows, surveys, structured team questions, QA disputes, and reporting can be added as separate modules later.
