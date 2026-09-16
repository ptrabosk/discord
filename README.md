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
3. In Discord, enable Developer Mode under **User Settings > Advanced**, right-click the target server icon, and select **Copy Server ID**.
4. Put that ID in `.env` as `DISCORD_GUILD_ID=<copied server ID>`.
5. Invite the bot to the target server.
6. Give it:
   - View Channels
   - Send Messages
   - Read Message History
   - Embed Links
   - Manage Roles
   - Manage Channels
   - Use Application Commands
7. The bot role must be ABOVE every role it manages.
8. Do not give Administrator unless needed temporarily for troubleshooting.

`DISCORD_GUILD_ID` is the only server where the bot is allowed to operate. The Discord application may remain Public so authorized users can install it, but the bot logs and automatically leaves every other server immediately without running setup, commands, interactions, or synchronization there.

## Install

Python 3.9+ is required; Python 3.11+ is recommended.

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill `.env`, including the copied server ID in `DISCORD_GUILD_ID`, and put the Google service-account JSON at `credentials.json`.

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
- `/health`

## Notes

This is the initial access-management foundation. WIW workflows, surveys, structured team questions, QA disputes, and reporting can be added as separate modules later.

## Hetzner Production Deployment

The production layout keeps code, mutable data, and backups separate:

```text
/opt/offsight-bot/
├── app/                       # Git checkout and virtual environment
│   ├── .env                   # server-only configuration, mode 600
│   ├── credentials.json       # server-only Google key, mode 600
│   └── .venv/
├── data/
│   └── opsbot.db              # persistent SQLite database
└── backups/                   # timestamped SQLite backups
```

The application resolves `.env`, relative Google credential paths, configuration JSON, and the local development database from the application directory rather than the shell's current directory. Production must set `DATABASE_PATH=/opt/offsight-bot/data/opsbot.db`, which keeps SQLite outside the Git checkout. A Git pull therefore cannot replace the production database.

### First deployment

The commands below target Ubuntu/Debian. Replace `<SERVER_IP>` and `<SSH_USER>` with the Hetzner host and administrative SSH account.

1. Connect and install prerequisites:

   ```bash
   ssh <SSH_USER>@<SERVER_IP>
   sudo apt-get update
   sudo apt-get install -y python3 python3-venv git
   python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else "Python 3.9 or newer is required")'
   ```

2. Create the dedicated, non-login service account and persistent directories:

   ```bash
   getent group offsightbot >/dev/null || sudo groupadd --system offsightbot
   id -u offsightbot >/dev/null 2>&1 || sudo useradd --system --gid offsightbot --home-dir /opt/offsight-bot --shell /usr/sbin/nologin offsightbot
   sudo install -d -o root -g offsightbot -m 0750 /opt/offsight-bot
   sudo install -d -o offsightbot -g offsightbot -m 0750 /opt/offsight-bot/data
   sudo install -d -o offsightbot -g offsightbot -m 0750 /opt/offsight-bot/backups
   ```

   These checks preserve an existing `offsightbot` account and group.

3. Clone the repository and create its virtual environment:

   ```bash
   sudo git clone https://github.com/ptrabosk/discord.git /opt/offsight-bot/app
   sudo python3 -m venv /opt/offsight-bot/app/.venv
   sudo /opt/offsight-bot/app/.venv/bin/python -m pip install --upgrade pip
   sudo /opt/offsight-bot/app/.venv/bin/python -m pip install -r /opt/offsight-bot/app/requirements.txt
   ```

4. Create the server-only environment file and edit every required value:

   ```bash
   sudo install -o offsightbot -g offsightbot -m 0600 /opt/offsight-bot/app/.env.example /opt/offsight-bot/app/.env
   sudoedit /opt/offsight-bot/app/.env
   ```

   Use these production paths:

   ```dotenv
   GOOGLE_SERVICE_ACCOUNT_FILE=/opt/offsight-bot/app/credentials.json
   DATABASE_PATH=/opt/offsight-bot/data/opsbot.db
   ```

   Configure `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, the Google Sheet ID/tab, SMTP settings, allowed email domains, verification limits, and sync interval. `BOT_VERSION` is optional; when blank, `/health` reports the checked-out Git commit when available. Never commit `.env`.

5. If Google service-account authentication is used, first create a private staging directory as the administrative SSH user on the server:

   ```bash
   install -d -m 0700 "$HOME/offsight-transfer"
   ```

   Copy the existing key from the Windows workstation using local PowerShell:

   ```powershell
   scp .\credentials.json <SSH_USER>@<SERVER_IP>:offsight-transfer/credentials.json
   ```

   Then install and remove the staged key on the server:

   ```bash
   sudo install -o offsightbot -g offsightbot -m 0600 "$HOME/offsight-transfer/credentials.json" /opt/offsight-bot/app/credentials.json
   rm "$HOME/offsight-transfer/credentials.json"
   rmdir "$HOME/offsight-transfer"
   ```

   Do not commit `credentials.json` and do not create a fake production key.

6. Transfer the existing SQLite database with SQLite's online backup mechanism. For the cleanest cutover, stop the Windows bot first so no records can change after the backup. Re-create the private staging directory on the server:

   ```bash
   install -d -m 0700 "$HOME/offsight-transfer"
   ```

   From local PowerShell in the project directory:

   ```powershell
   python -c "import sqlite3; source=sqlite3.connect(r'data\opsbot.db'); destination=sqlite3.connect(r'opsbot-transfer.db'); source.backup(destination); destination.close(); source.close()"
   scp .\opsbot-transfer.db <SSH_USER>@<SERVER_IP>:offsight-transfer/opsbot.db
   Remove-Item .\opsbot-transfer.db
   ```

   Then install the backup on the server without changing its schema or records:

   ```bash
   sudo install -o offsightbot -g offsightbot -m 0600 "$HOME/offsight-transfer/opsbot.db" /opt/offsight-bot/data/opsbot.db
   rm "$HOME/offsight-transfer/opsbot.db"
   rmdir "$HOME/offsight-transfer"
   ```

   If there is no existing database, omit this transfer; the bot creates a new one at first startup. The bot never automatically moves, deletes, or migrates an existing database file.

7. Apply production ownership and permissions:

   ```bash
   sudo chown -R root:offsightbot /opt/offsight-bot/app
   sudo chmod 0750 /opt/offsight-bot /opt/offsight-bot/app
   sudo chown -R offsightbot:offsightbot /opt/offsight-bot/data /opt/offsight-bot/backups
   sudo chmod 0750 /opt/offsight-bot/data /opt/offsight-bot/backups
   sudo chown offsightbot:offsightbot /opt/offsight-bot/app/.env /opt/offsight-bot/app/credentials.json
   sudo chmod 0600 /opt/offsight-bot/app/.env /opt/offsight-bot/app/credentials.json
   ```

   If this deployment does not use `credentials.json`, omit it from the final two commands. Code is root-owned and read-only to the service account; secrets are readable only by `offsightbot` and root; the data and backup directories are writable by `offsightbot`.

8. Perform offline validation without connecting to Discord:

   ```bash
   sudo /opt/offsight-bot/app/.venv/bin/python -m compileall -q /opt/offsight-bot/app/main.py /opt/offsight-bot/app/app_paths.py /opt/offsight-bot/app/bot /opt/offsight-bot/app/database /opt/offsight-bot/app/integrations
   sudo -u offsightbot env PYTHONPATH=/opt/offsight-bot/app /opt/offsight-bot/app/.venv/bin/python -c "import app_paths, bot.commands, bot.guild_authorization, bot.health, bot.instance_lock, bot.onboarding, bot.permissions, bot.setup_server, bot.sync, database.database, integrations.email_verification, integrations.google_sheets"
   ```

9. Confirm the Windows/local production bot is stopped. Never run the Windows bot and Hetzner bot simultaneously. To perform a live foreground test before installing the service:

   ```bash
   sudo -u offsightbot /opt/offsight-bot/app/.venv/bin/python /opt/offsight-bot/app/main.py
   ```

   Confirm the startup logs, Discord connection, authorized guild, and integration checks, then press `Ctrl+C`. The bot also uses a lock beside the database to reject a second local instance.

10. Install, enable, and start the systemd service:

    ```bash
    sudo install -o root -g root -m 0644 /opt/offsight-bot/app/deploy/offsight-bot.service /etc/systemd/system/offsight-bot.service
    sudo systemctl daemon-reload
    sudo systemctl enable offsight-bot
    sudo systemctl start offsight-bot
    ```

    `enable` configures automatic startup at boot. Check it with:

    ```bash
    sudo systemctl is-enabled offsight-bot
    sudo systemctl status offsight-bot
    ```

11. Inspect current and recent journald logs:

    ```bash
    sudo journalctl -u offsight-bot -f
    sudo journalctl -u offsight-bot -n 100 --no-pager
    ```

12. Reboot-test automatic startup:

    ```bash
    sudo reboot
    ```

    After reconnecting over SSH:

    ```bash
    sudo systemctl is-enabled offsight-bot
    sudo systemctl is-active offsight-bot
    sudo systemctl status offsight-bot
    sudo journalctl -u offsight-bot -n 100 --no-pager
    ```

`deploy/install.sh` is an optional first-install helper after the repository has been cloned into `/opt/offsight-bot/app`; run it with `sudo bash /opt/offsight-bot/app/deploy/install.sh`. It installs prerequisites, creates the account/directories and virtual environment, and deliberately does not start the service. It enables boot startup only when an existing server-side configuration passes import validation, and refuses to replace an existing unit or virtual environment.

### Operation and updates

Service controls:

```bash
sudo systemctl start offsight-bot
sudo systemctl stop offsight-bot
sudo systemctl restart offsight-bot
sudo systemctl status offsight-bot
sudo journalctl -u offsight-bot -f
sudo journalctl -u offsight-bot -n 100 --no-pager
```

Deploy future GitHub updates with the checked-in helper:

```bash
sudo bash /opt/offsight-bot/app/deploy/deploy.sh
```

It performs a fast-forward-only pull, installs requirements, runs syntax/import validation, restarts the unit, checks that it remains active, and shows recent logs. It does not touch `.env`, `credentials.json`, `/opt/offsight-bot/data`, or `/opt/offsight-bot/backups`.

Create an on-demand, transactionally consistent SQLite backup with:

```bash
sudo bash /opt/offsight-bot/app/deploy/backup.sh
```

The script uses Python's SQLite backup API and writes a mode-600 timestamped file such as `/opt/offsight-bot/backups/opsbot-2026-09-16_143000.db`. For a custom database or backup directory, pass them as the first and second arguments. It never includes `.env` or Google credentials.

Backups are not scheduled automatically. A later root cron entry could run `deploy/backup.sh` at a chosen interval, or the same command can be placed in a dedicated systemd oneshot service triggered by a timer. Test restoration and retention before enabling either schedule.

### Health and shutdown behavior

`/health` is ephemeral and limited to members with the `Ops Bot Admin` role or Discord Administrator permission. It reports uptime/version, authorized-guild connectivity, read-only Google Sheet access, database access, SMTP configuration (without sending mail), last successful sync/failure, and whether the background sync task is running. It exposes no credential values.

Systemd sends `SIGTERM`; the bot catches it, cancels the periodic sync task, closes the Discord client, releases its instance lock, and lets active SQLite context managers close normally. Crashes restart after five seconds and all logs go to stdout/stderr for journald.
