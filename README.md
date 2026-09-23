# Discord Ops Bot

Creates and manages the Discord operations server structure, controlled employee onboarding, and ongoing access synchronization.

## Server structure

- SETUP — public
  - initial-setup
- OPERATIONS — public
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
  - team-thandeka
  - team-mohammed
  - team-flex
- WORKFLOWS — role locked
  - main — public override
  - wiw-adjustments
  - surveys
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

## Google Sheet

Create a worksheet named `Discord Access` with these headers:

Email | Name | Active | Professional Services | White Glove | Growth Strategy | Order Operations | Fashion Nova | Concierge | Concierge Team | Workflows | Leadership

Boolean access fields accept TRUE/YES/1/X.

`Concierge Team` accepts Nadir, Thandeka, Mohammed, or Flex. Multiple teams can be comma-separated.

Share the access-roster Sheet with the service-account email from `credentials.json` as Viewer.

## Employee forms

Verified employees start Timesheet Adjustments, Emergency Shift Releases, Questions,
Concerns, and Feedback with `/form`. All employee prompts and confirmations are private/ephemeral;
no public launcher message is needed. Previously posted `Submit a Form` buttons still work, but the
bot no longer posts new ones. Submissions still post review cards to configured private reviewer
channels. Identity comes from the verified-user database and the `Name`/`Email` fields in the
access roster; users never enter identity manually.

### Editing form questions and answers

Edit [`config/forms.json`](config/forms.json), then restart the bot. It contains:

- `form_names`: the top-level choices shown to employees.
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
  Employee Email, Employee Name, Status, Adjustment Type, Start Time ET, Start Time UTC, End Time ET,
  End Time UTC, Adjustment Reason, Reviewed By, Reviewed At ET, Reviewed At UTC
- `WIW Emergency Releases`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Employee Email, Employee Name, Status, Shift Start ET, Shift Start UTC, Release Reason, Reviewed By,
  Reviewed At ET, Reviewed At UTC
- `Questions Concerns Feedback`: Submission ID, Submitted At ET, Submitted At UTC, Discord User ID,
  Employee Email, Employee Name, Type, Category, Message, Status, Reviewed By, Reviewed At ET,
  Reviewed At UTC

Configure private Discord review channels by numeric ID:

- `WIW_REVIEW_CHANNEL_ID`
- `QUESTION_REVIEW_CHANNEL_ID`
- `CONCERN_REVIEW_CHANNEL_ID`
- `FEEDBACK_REVIEW_CHANNEL_ID`

The three general form types may use the same channel ID. The bot refuses to post employee data to
a channel visible to `@everyone`. Set `FORM_ERROR_CHANNEL_ID` to a private operations channel for
technical form errors; it defaults to `1550994656966344714`. Error alerts contain only an event
type and reference, not employee messages or credentials. An agent is told about delivery trouble
only if both the review-channel post and Sheets write fail. Existing launcher buttons and reviewer
buttons survive bot restarts.

Timesheet Adjustments show one date menu containing today and the previous 14 days in New York.
The end date is automatically the same as the selected start date; employees choose only the end
time. Emergency Shift Releases keep the range-then-date picker, configurable with
`FORM_DATE_PAST_DAYS` and `FORM_DATE_FUTURE_DAYS` (maximum 350 selectable dates). Start times are
selected from `start_hours` in `config/forms.json`, with minutes fixed at `:00`; end times are
selected by hour and then `00`, `15`, `30`, or `45`. Local datetimes use `America/New_York` and are
stored in both ET and UTC.

SQLite is the durable source of truth. A submission succeeds once it is stored locally. Sheets
sync retries every `FORM_SHEETS_RETRY_MINUTES` and searches column A for the Submission ID before
appending, so retries update the existing row rather than duplicating it.

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

1. Employee clicks **Verify Work Email**.
2. Bot checks domain, roster presence, and Active status.
3. Bot sends a six-digit code to the work email.
4. Employee enters the code.
5. Bot re-reads the Sheet.
6. Bot calculates desired managed roles.
7. Missing authorized roles are added.
8. Unauthorized managed roles are removed.
9. The bot periodically reconciles verified users.

If `Active` becomes FALSE or the employee disappears from the Sheet, managed access roles are removed on the next sync.

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

Any verified employee can run `/form` privately.

## Notes

This is the initial access-management foundation. WIW workflows, surveys, structured team questions, QA disputes, and reporting can be added as separate modules later.
