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

Share the Google Sheet with the service-account email from `credentials.json` as Viewer.

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

## Notes

This is the initial access-management foundation. WIW workflows, surveys, structured team questions, QA disputes, and reporting can be added as separate modules later.
