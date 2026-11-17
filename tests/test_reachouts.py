import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import discord

from reachouts import views
from reachouts.domain import (
    ACK_REQUIRED_TYPES,
    MAX_BODY,
    MAX_TITLE,
    MAX_URL,
    Payload,
    ReachoutConfig,
    add_business_days,
    evaluate_agent_row,
    iso,
    parse_config,
    parse_due_override,
    utcnow,
    validate_row,
)
from reachouts.repository import ReachoutRepository, StaleForm, WorkflowError
from reachouts.service import ReachoutLoop, ReachoutService, Settings
from reachouts.sheets import AGENTS_TAB, HISTORY_TAB, QUEUE_TAB, RESPONSES_TAB, TAB_HEADERS, Table

NY = ZoneInfo("America/New_York")
CONFIG = ReachoutConfig()
AGENT_ID, LEAD_ID, NEW_LEAD_ID, OTHER_ID = 111111111111111111, 222222222222222222, 333333333333333333, 444444444444444444
BOT_ID = 999999999999999999
CHANNEL_ID = 555555555555555555


def ny(*args):
    return datetime(*args, tzinfo=NY)


class BusinessDayTests(unittest.TestCase):
    def test_friday_afternoon_is_due_wednesday_same_time(self):
        due = add_business_days(ny(2026, 10, 30, 14, 0), 3, CONFIG)
        self.assertEqual(due.astimezone(NY), ny(2026, 11, 4, 14, 0))

    def test_weekend_delivery_counts_the_next_business_day_as_day_one(self):
        self.assertEqual(add_business_days(ny(2026, 10, 31, 10, 0), 3, CONFIG).astimezone(NY), ny(2026, 11, 4, 10, 0))
        self.assertEqual(add_business_days(ny(2026, 11, 1, 23, 0), 3, CONFIG).astimezone(NY), ny(2026, 11, 4, 23, 0))

    def test_only_configured_calendars_skip_holidays(self):
        holidays = frozenset({("us", date(2026, 11, 2)), ("other", date(2026, 11, 3))})
        config = ReachoutConfig(holidays=holidays, holiday_calendars=("us",))
        self.assertEqual(add_business_days(ny(2026, 10, 30, 14, 0), 3, config).astimezone(NY), ny(2026, 11, 5, 14, 0))

    def test_holiday_delivery_also_starts_counting_the_next_business_day(self):
        config = ReachoutConfig(holidays=frozenset({("default", date(2026, 11, 26))}))
        self.assertEqual(add_business_days(ny(2026, 11, 26, 9, 0), 3, config).astimezone(NY), ny(2026, 12, 1, 9, 0))

    def test_wall_clock_time_survives_daylight_saving_changes(self):
        spring = add_business_days(ny(2026, 3, 6, 14, 0), 3, CONFIG)
        self.assertEqual((spring.astimezone(NY).hour, spring.hour), (14, 18))  # EDT, from a 19:00 UTC EST start
        self.assertEqual(ny(2026, 3, 6, 14, 0).astimezone(timezone.utc).hour, 19)
        fall = add_business_days(ny(2026, 10, 30, 9, 0), 3, CONFIG)
        self.assertEqual((fall.astimezone(NY).hour, fall.hour), (9, 14))

    def test_a_skipped_wall_time_moves_forward(self):
        config = ReachoutConfig(business_weekdays=tuple(range(7)))
        due = add_business_days(ny(2026, 3, 7, 2, 30), 1, config)
        self.assertEqual(due.astimezone(NY).replace(tzinfo=None), datetime(2026, 3, 8, 3, 30))

    def test_due_override_is_in_the_operational_timezone(self):
        self.assertEqual(parse_due_override("2026-11-04 17:00", CONFIG), datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "gap"):
            parse_due_override("2026-03-08 02:30", CONFIG)
        with self.assertRaisesRegex(ValueError, "look like"):
            parse_due_override("11/4/2026 5pm", CONFIG)

    def test_config_falls_back_visibly(self):
        config, status, holidays = parse_config(
            [{"key": "default_deadline_business_days", "value": "zero"}, {"key": "timezone", "value": "America/Chicago"},
             {"key": "nonsense", "value": "1"}],
            [{"date": "2026-12-25", "calendar_id": "us"}, {"date": "12/26/2026"}],
        )
        self.assertEqual(config.deadline_business_days, 3)
        self.assertTrue(status["default_deadline_business_days"].startswith("INVALID"))
        self.assertEqual(config.timezone, "America/Chicago")
        self.assertTrue(status["nonsense"].startswith("IGNORED"))
        self.assertEqual(holidays, {0: "OK", 1: "INVALID: use YYYY-MM-DD"})
        self.assertEqual(ReachoutConfig.from_json(config.to_json()), config)


def queue_row(**overrides):
    row = {
        "send": "TRUE", "agent_email": "agent@example.com", "reachout_type": "Agent behaviors",
        "title": "Tone in chat", "message_body": "Please review the attached chat.", "reference_urls": "",
        "due_at_override": "",
    }
    return row | overrides


class ValidationTests(unittest.TestCase):
    def test_three_mandatory_categories_always_require_acknowledgment(self):
        for label in ("Agent behaviors", "Opt outs", "Flagged messages", "Concerning"):
            payload, errors = validate_row(queue_row(reachout_type=label), CONFIG, utcnow())
            self.assertEqual(errors, [])
            self.assertTrue(payload.ack_required)
            self.assertTrue(payload.response_text_required)
            # The old column can no longer make the written response optional.
            payload, _ = validate_row(queue_row(reachout_type=label, response_text_required="FALSE"), CONFIG, utcnow())
            self.assertTrue(payload.response_text_required)
        self.assertEqual(ACK_REQUIRED_TYPES, {"agent_behaviors", "opt_outs", "flagged_messages", "concerning"})
        # No operator column exists that could switch acknowledgment off.
        self.assertFalse(any("ack" in header for header in TAB_HEADERS[QUEUE_TAB][:12]))

    def test_weekly_performance_never_requires_anything(self):
        payload, errors = validate_row(queue_row(reachout_type="weekly performance"), CONFIG, utcnow())
        self.assertEqual(errors, [])
        self.assertFalse(payload.ack_required)
        self.assertFalse(payload.response_text_required)
        payload, _ = validate_row(queue_row(reachout_type="Weekly performance", response_text_required="TRUE"), CONFIG, utcnow())
        self.assertFalse(payload.response_text_required)
        _, errors = validate_row(queue_row(reachout_type="Weekly performance", due_at_override="2099-01-01 10:00"),
                                 CONFIG, utcnow())
        self.assertIn("only applies", errors[0])

    def test_actionable_errors(self):
        _, errors = validate_row(queue_row(agent_email="", title="", message_body="x" * 4000, reachout_type="Coaching",
                                           reference_urls="ftp://x",
                                           due_at_override="2000-01-01 10:00"), CONFIG, utcnow())
        text = " ".join(errors)
        for fragment in ("agent_email is required", "reachout_type must be one of", "title is required", "limit is 3500",
                         "http(s)", "in the past"):
            self.assertIn(fragment, text)
        _, errors = validate_row(queue_row(agent_email="someone@example.invalid"), CONFIG, utcnow())
        self.assertIn("never be sent", errors[0])

    def test_agent_directory_rows(self):
        record = {"Name": "Agent One", "Active": "TRUE", "Team Lead Email": "Lead@X.com "}
        base = {"agent_id": "A1", "agent_email": "A@x.com", "active": "TRUE"}
        ok = evaluate_agent_row(base, record, AGENT_ID, LEAD_ID)
        self.assertEqual((ok["directory_status"], ok["lead_email"], ok["lead_discord_user_id"]), ("OK", "lead@x.com", LEAD_ID))
        unverified_lead = evaluate_agent_row(base, record, AGENT_ID, None)
        self.assertEqual(unverified_lead["directory_status"], "BLOCKED")
        self.assertIn("lead@x.com", unverified_lead["directory_error"])
        leadership = evaluate_agent_row(base, {"Name": "Boss", "Active": "TRUE", "Team Lead Email": ""}, AGENT_ID, None)
        self.assertEqual(leadership["directory_status"], "NO_LEAD")
        self.assertEqual(evaluate_agent_row(base, record, None, LEAD_ID)["directory_status"], "BLOCKED")
        self.assertEqual(evaluate_agent_row(base, record | {"Active": "FALSE"}, AGENT_ID, LEAD_ID)["directory_status"], "INACTIVE")
        self.assertEqual(evaluate_agent_row(base, record, LEAD_ID, LEAD_ID)["directory_status"], "INVALID")
        self.assertEqual(evaluate_agent_row(base | {"agent_id": "EXAMPLE-AGENT-1"}, record, AGENT_ID, LEAD_ID)["directory_status"],
                         "EXAMPLE")

    def test_long_text_fits_discord_embed_limits(self):
        payload = Payload("A1", "flagged_messages", "t" * MAX_TITLE, "b" * MAX_BODY, ["https://e.x/" + "a" * (MAX_URL - 12)] * 5)
        rec = {"reachout_id": "RO-20261028-ABCDEF12", "reachout_type": "flagged_messages", "workflow_status": "OPEN"}
        embed = views.reachout_embed(rec, payload, NY, None)
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.title), 256)
        self.assertTrue(all(len(f.value) <= 1024 for f in embed.fields))
        links = "\n".join(f.value for f in embed.fields)
        self.assertEqual(links.count("](https://e.x/"), 5)
        self.assertEqual(embed.fields[0].name, "References")

    def test_reference_links_show_as_numbered_hyperlinks(self):
        payload = Payload("A1", "opt_outs", "T", "B", ["https://a.example/1", "https://b.example/2"])
        rec = {"reachout_id": "RO-20261028-ABCDEF12", "reachout_type": "opt_outs", "workflow_status": "OPEN"}
        field, = views.reachout_embed(rec, payload, NY, None).fields
        self.assertEqual((field.name, field.value),
                         ("References", "[Link 1](https://a.example/1)\n[Link 2](https://b.example/2)"))


# --- fakes: an in-memory spreadsheet and just enough of Discord ----------------------------

class FakeSheet:
    def __init__(self):
        self.grid = {tab: [list(headers)] for tab, headers in TAB_HEADERS.items()}
        self.records = {}

    def add(self, tab, values):
        self.grid[tab].append([values.get(h, "") for h in self.grid[tab][0]])
        return len(self.grid[tab])

    def row(self, tab, number):
        return dict(zip(self.grid[tab][0], self.grid[tab][number - 1]))

    def set(self, tab, number, **values):
        for key, value in values.items():
            self.grid[tab][number - 1][self.grid[tab][0].index(key)] = value

    def read(self, tabs):
        return {tab: Table(tab, [list(r) for r in self.grid[tab]]) for tab in tabs}

    def read_rows(self, tab, numbers):
        return {n: list(self.grid[tab][n - 1]) for n in numbers}

    def write(self, updates):
        for tab, row, col, value in updates:
            self.grid[tab][row - 1][col - 1] = value

    def write_by_key(self, tab, key, values):
        headers = self.grid[tab][0]
        for number, raw in enumerate(self.grid[tab][1:], 2):
            for header, value in values.get(raw[headers.index(key)], {}).items():
                self.grid[tab][number - 1][headers.index(header)] = value

    def upsert(self, batches):
        for tab, (key, rows) in batches.items():
            for row in rows:
                self.records.setdefault(tab, {})[row[key]] = row


class FakeMember(SimpleNamespace):
    __hash__ = object.__hash__  # usable as a permission-overwrite key, like real Members and Roles


def member(user_id):
    return FakeMember(id=user_id, display_name=f"user{user_id}", guild_permissions=SimpleNamespace(administrator=False))


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "x")


class FakeMessage:
    def __init__(self, channel, message_id, kwargs):
        self.channel, self.id, self.kwargs = channel, message_id, kwargs
        self.embeds = [kwargs["embed"]] if kwargs.get("embed") else []
        self.author = SimpleNamespace(id=BOT_ID)
        self.created_at = utcnow()
        self.edits = []

    async def create_thread(self, **kwargs):
        thread = FakeChannel(self.channel.bot, self.id, self.channel.guild)
        thread.parent, thread.archived = self.channel, False
        self.channel.guild.threads[self.id] = thread
        self.channel.bot.channels[self.id] = thread
        return thread

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, bot, channel_id, guild):
        self.bot, self.id, self.guild = bot, channel_id, guild
        self.sent, self.ow, self.members = [], {}, []
        self.fail_next = None

    @property
    def overwrites(self):
        return {discord.Object(id=target_id): value for target_id, value in self.ow.items()}

    async def send(self, **kwargs):
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error
        message = FakeMessage(self, 900000000000000000 + len(self.bot.messages), kwargs)
        self.sent.append(message)
        self.bot.messages[message.id] = message
        return message

    async def history(self, limit=100):
        for message in reversed(self.sent[-limit:]):
            yield message

    async def fetch_message(self, message_id):
        return self.bot.messages[message_id]

    def permissions_for(self, target):
        return SimpleNamespace(view_channel=False)

    def overwrites_for(self, target):
        return self.ow.get(target.id) or discord.PermissionOverwrite()

    async def set_permissions(self, target, overwrite=None, reason=None):
        self.ow[target.id] = overwrite

    async def edit(self, **kwargs):
        self.archived = kwargs.get("archived", False)


class FakeGuild:
    def __init__(self, bot):
        self.bot = bot
        self.members = {i: member(i) for i in (AGENT_ID, LEAD_ID, NEW_LEAD_ID, OTHER_ID)}
        self.me = member(BOT_ID)
        self.default_role = FakeMember(id=1)
        self.threads = {}

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def fetch_member(self, user_id):
        raise http_error(discord.NotFound, 404)

    def get_channel(self, channel_id):
        return self.bot.channels.get(channel_id)

    def get_thread(self, thread_id):
        return self.threads.get(thread_id)


class FakeUser:
    def __init__(self, bot, user_id, dms_open=True):
        self.id = user_id
        self.dm_channel = FakeChannel(bot, 800000000000000000 + user_id % 1000, None)
        if not dms_open:
            self.dm_channel.fail_next = http_error(discord.Forbidden, 403)


class FakeBot:
    guild_id = 1

    def __init__(self):
        self.user = SimpleNamespace(id=BOT_ID)
        self.channels, self.messages, self.users = {}, {}, {}
        self.guild = FakeGuild(self)
        channel = FakeChannel(self, CHANNEL_ID, self.guild)
        channel.members = [self.guild.members[AGENT_ID], self.guild.members[LEAD_ID]]
        self.channels[CHANNEL_ID] = channel

    def get_guild(self, guild_id):
        return self.guild

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id in self.channels:
            return self.channels[channel_id]
        raise http_error(discord.NotFound, 404)

    def get_user(self, user_id):
        return self.users.setdefault(user_id, FakeUser(self, user_id))

    def get_partial_messageable(self, channel_id):
        messages = self.messages
        return SimpleNamespace(get_partial_message=lambda message_id: messages[message_id])


class Interaction(SimpleNamespace):
    counter = 0

    @classmethod
    def make(cls, user_id):
        cls.counter += 1
        return cls(id=cls.counter, guild_id=1, user=SimpleNamespace(id=user_id, display_name=f"user{user_id}"))


class WorkflowBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = ReachoutRepository(Path(tmp.name) / "opsbot.db")
        self.sheet = FakeSheet()
        self.bot = FakeBot()
        self.settings = Settings(sheet_id="x", poll_seconds=60, batch=10,
                                 category_id=0, test_mode=False, test_allowed=frozenset(), error_channel_id=0)
        self.roster = [{"Email": "agent@example.com", "First Name": "Agent", "Last Name": "One", "Active": "TRUE",
                        "Team Lead Email": "lead@example.com"}]
        self.service = ReachoutService(self.bot, SimpleNamespace(all_records=lambda: self.roster), repo=self.repo,
                                       sheet=self.sheet, settings=self.settings)
        self.service.init()
        self.service.kick = lambda: None
        self.alerts = []

        async def alert(kind, reference, error=None):
            self.alerts.append(kind)

        self.service.alert = alert
        verified = {"agent@example.com": (AGENT_ID, "agent@example.com"), "lead@example.com": (LEAD_ID, "lead@example.com"),
                    "newlead@example.com": (NEW_LEAD_ID, "newlead@example.com")}
        by_id = {value[0]: value for value in verified.values()}
        patch("reachouts.service.get_verified_by_email", verified.get).start()
        patch("reachouts.service.get_verified_user", by_id.get).start()
        patch("reachouts.service.discord.TextChannel", FakeChannel).start()
        self.addCleanup(patch.stopall)
        self.agent_row = self.sheet.add(AGENTS_TAB, {
            "agent_email": "agent@example.com", "active": "TRUE", "existing_channel_id": str(CHANNEL_ID),
        })

    async def poll(self, send=True):
        return await self.service.poll_once(send=send)

    async def jobs(self):
        for _ in range(10):
            jobs = self.repo.claim_jobs(utcnow() + timedelta(days=60), 50)
            if not jobs:
                return
            for job in jobs:
                await self.service.run_job(job)

    async def deliver(self, **row):
        number = self.sheet.add(QUEUE_TAB, queue_row(**row))
        await self.poll()
        await self.jobs()
        return number, self.sheet.row(QUEUE_TAB, number)["reachout_id"]

    async def submit(self, rid, user_id=AGENT_ID, text="Understood.", obligation=..., images=()):
        if obligation is ...:
            ob = self.repo.open_obligation(rid)
            obligation = ob["obligation_id"] if ob else None
        return await self.service.submit_agent(Interaction.make(user_id), rid, obligation, text, images)

    def thread(self, rid):
        return self.bot.channels[self.repo.get(rid)["thread_id"]]


class DispatchTests(WorkflowBase):
    async def test_draft_rows_never_get_an_id_or_send(self):
        number = self.sheet.add(QUEUE_TAB, queue_row(send="FALSE"))
        await self.poll()
        await self.jobs()
        self.assertEqual(self.sheet.row(QUEUE_TAB, number)["reachout_id"], "")
        self.assertEqual(self.bot.channels[CHANNEL_ID].sent, [])

    async def test_ready_row_sends_once_to_the_private_channel_with_a_thread(self):
        number, rid = await self.deliver()
        channel = self.bot.channels[CHANNEL_ID]
        self.assertEqual(len(channel.sent), 1)
        sent = channel.sent[0].kwargs
        self.assertEqual([u.id for u in sent["allowed_mentions"].users], [AGENT_ID])
        self.assertFalse(sent["allowed_mentions"].everyone)
        self.assertFalse(sent["allowed_mentions"].roles)
        embed = sent["embed"]
        self.assertEqual(embed.title, "Tone in chat")
        self.assertEqual(embed.description, "Please review the attached chat.")
        self.assertEqual(embed.fields, [])
        self.assertEqual(embed.footer.text, "Respond with the button below, not in the thread. Typing is disabled.")
        self.assertNotIn(rid, embed.footer.text)
        rec = self.repo.get(rid)
        self.assertEqual((rec["dispatch_state"], rec["workflow_status"]), ("DELIVERED", "OPEN"))
        self.assertEqual(rec["thread_id"], rec["message_id"])
        ob = self.repo.open_obligation(rid)
        self.assertEqual(ob["obligation_type"], "ACKNOWLEDGMENT")
        self.assertEqual(ob["due_at"], iso(rec_time(rec["prepared_at"]) + timedelta(days=7)))
        agent = channel.ow[AGENT_ID]
        self.assertEqual((agent.view_channel, agent.read_message_history, agent.send_messages,
                          agent.send_messages_in_threads, agent.create_public_threads, agent.create_private_threads),
                         (True, True, False, False, False, False))
        self.assertTrue(channel.ow[BOT_ID].manage_threads)
        self.assertTrue(channel.ow[LEAD_ID].send_messages)
        # Inserting a row above it (a move) and editing it never re-sends.
        self.sheet.grid[QUEUE_TAB].insert(1, [""] * len(TAB_HEADERS[QUEUE_TAB]))
        self.sheet.set(QUEUE_TAB, number + 1, title="Edited title")
        await self.poll()
        await self.jobs()
        self.assertEqual(len(channel.sent), 1)
        moved = self.sheet.row(QUEUE_TAB, number + 1)
        self.assertEqual((moved["reachout_id"], moved["processing_status"]), (rid, "SENT"))

    async def test_new_channels_are_named_for_the_agents_first_name(self):
        created = {}

        class Category:
            channels = []

        async def create_text_channel(name, **kwargs):
            channel = FakeChannel(self.bot, 777777777777777777, self.bot.guild)
            channel.members = [self.bot.guild.members[AGENT_ID], self.bot.guild.members[LEAD_ID]]
            self.bot.channels[channel.id] = channel
            created["name"] = name
            return channel

        self.bot.channels[CHANNEL_ID] = None
        self.sheet.set(AGENTS_TAB, self.agent_row, existing_channel_id="")
        self.roster[0]["First Name"] = "Mary Ann"
        self.bot.channels[1] = Category()
        self.settings.category_id = 1
        self.bot.guild.create_text_channel = create_text_channel
        with patch("reachouts.service.discord.CategoryChannel", Category):
            await self.deliver()
        self.assertEqual(created["name"], "reachout-mary-ann")
        self.assertEqual(self.repo.get_agent("agent@example.com")["agent_name"], "Mary Ann One")

    async def test_agents_can_be_identified_by_email_alone(self):
        number, rid = await self.deliver(agent_email="Agent@Example.com")
        await self.poll()  # Sheets shows delivery on the next poll
        self.assertEqual(self.sheet.row(QUEUE_TAB, number)["processing_status"], "SENT")
        self.assertEqual(self.repo.get(rid)["agent_id"], "agent@example.com")
        self.assertEqual(len(self.bot.channels[CHANNEL_ID].sent), 1)

    async def test_formula_or_api_values_are_read_like_typed_ones(self):
        self.sheet.add(QUEUE_TAB, queue_row(send=" true ", reachout_type="opt outs"))
        await self.poll()
        await self.jobs()
        self.assertEqual(len(self.bot.channels[CHANNEL_ID].sent), 1)

    async def test_invalid_duplicate_and_unknown_rows_never_post(self):
        bad = self.sheet.add(QUEUE_TAB, queue_row(title=""))
        await self.poll()
        self.assertEqual(self.sheet.row(QUEUE_TAB, bad)["processing_status"], "ERROR")
        self.assertIn("title is required", self.sheet.row(QUEUE_TAB, bad)["status_detail"])
        number, rid = await self.deliver()
        copy = self.sheet.add(QUEUE_TAB, self.sheet.row(QUEUE_TAB, number))
        unknown = self.sheet.add(QUEUE_TAB, queue_row(reachout_id="RO-20260101-DEADBEEF"))
        await self.poll()
        await self.jobs()
        for row in (copy, number):
            self.assertEqual(self.sheet.row(QUEUE_TAB, row)["processing_status"], "ERROR")
            self.assertIn("is on 2 rows", self.sheet.row(QUEUE_TAB, row)["status_detail"])
        self.assertEqual(self.sheet.row(QUEUE_TAB, unknown)["processing_status"], "ERROR")
        self.assertIn("not issued by the bot", self.sheet.row(QUEUE_TAB, unknown)["status_detail"])
        self.assertEqual(len(self.bot.channels[CHANNEL_ID].sent), 2 - 1)

    async def test_ticked_rows_wait_for_reachout_poll(self):
        number = self.sheet.add(QUEUE_TAB, queue_row())
        await self.poll(send=False)  # the automatic poll
        await self.jobs()
        self.assertEqual(self.sheet.row(QUEUE_TAB, number)["reachout_id"], "")
        self.assertEqual(self.bot.channels[CHANNEL_ID].sent, [])
        await self.service.tick(full=True, send=True)  # what /reachout_poll runs
        self.assertEqual(len(self.bot.channels[CHANNEL_ID].sent), 1)
        await self.poll(send=False)
        self.assertEqual(self.sheet.row(QUEUE_TAB, number)["processing_status"], "SENT")

    async def test_reachout_poll_sends_every_ticked_row_beyond_the_batch_size(self):
        self.settings.batch = 2
        for n in range(5):
            self.sheet.add(QUEUE_TAB, queue_row(title=f"Row {n}"))
        await self.service.tick(full=True, send=True)
        self.assertEqual(len(self.bot.channels[CHANNEL_ID].sent), 5)

    async def test_edits_after_release_hold_the_send(self):
        number = self.sheet.add(QUEUE_TAB, queue_row())
        await self.poll()  # queued; jobs have not run
        rid = self.sheet.row(QUEUE_TAB, number)["reachout_id"]
        self.sheet.set(QUEUE_TAB, number, message_body="Changed while ticked")
        await self.poll()
        await self.jobs()
        self.assertEqual(self.repo.get(rid)["dispatch_state"], "HELD_CHANGED")
        self.assertEqual(self.sheet.row(QUEUE_TAB, number)["processing_status"], "ERROR")
        self.assertEqual(self.bot.channels[CHANNEL_ID].sent, [])

    async def test_deleting_rows_mid_poll_never_writes_to_the_wrong_row(self):
        first = self.sheet.add(QUEUE_TAB, queue_row(title="First"))
        second = self.sheet.add(QUEUE_TAB, queue_row(title="Second", send="FALSE"))
        await self.poll()
        await self.jobs()
        rid = self.sheet.row(QUEUE_TAB, first)["reachout_id"]
        real_write = self.sheet.write_by_key

        def delete_first_then_write(tab, key, values):
            del self.sheet.grid[QUEUE_TAB][first - 1]  # the operator deletes the sent row during the poll
            real_write(tab, key, values)

        self.sheet.write_by_key = delete_first_then_write
        await self.poll()
        self.assertEqual(self.sheet.row(QUEUE_TAB, second - 1)["title"], "Second")
        self.assertEqual(self.sheet.row(QUEUE_TAB, second - 1)["processing_status"], "")
        self.assertEqual(self.repo.get(rid)["row_missing"], 0)  # gone only from the next read on

    async def test_draft_then_ready_releases_the_edited_snapshot(self):
        number = self.sheet.add(QUEUE_TAB, queue_row())
        await self.poll()
        rid = self.sheet.row(QUEUE_TAB, number)["reachout_id"]
        self.sheet.set(QUEUE_TAB, number, send="FALSE")
        await self.poll()
        self.assertEqual(self.repo.get(rid)["dispatch_state"], "WITHDRAWN")
        self.sheet.set(QUEUE_TAB, number, send="TRUE", message_body="Final text")
        await self.poll()
        await self.jobs()
        self.assertEqual(self.bot.channels[CHANNEL_ID].sent[0].kwargs["embed"].description, "Final text")

    async def test_test_mode_blocks_everyone_not_allowlisted(self):
        self.settings.test_mode = True
        self.settings.test_allowed = frozenset({AGENT_ID})
        number = self.sheet.add(QUEUE_TAB, queue_row())
        await self.poll()
        await self.jobs()
        self.assertEqual(self.sheet.row(QUEUE_TAB, number)["processing_status"], "ERROR")
        self.assertIn("Test mode", self.sheet.row(QUEUE_TAB, number)["status_detail"])
        self.assertEqual(self.bot.channels[CHANNEL_ID].sent, [])
        # The allowlist also accepts verified personal emails.
        self.settings.test_allowed_emails = frozenset({"lead@example.com"})
        await self.poll()
        await self.jobs()
        self.assertEqual(len(self.bot.channels[CHANNEL_ID].sent), 1)

    async def test_leadership_rows_without_a_lead_are_never_sent(self):
        self.roster[0]["Team Lead Email"] = ""
        number = self.sheet.add(QUEUE_TAB, queue_row())
        await self.poll()
        await self.jobs()
        self.assertEqual(self.sheet.row(AGENTS_TAB, self.agent_row)["directory_status"], "NO_LEAD")
        self.assertEqual(self.sheet.row(QUEUE_TAB, number)["processing_status"], "ERROR")
        self.assertEqual(self.bot.channels[CHANNEL_ID].sent, [])

    async def test_a_crash_after_discord_accepted_the_post_does_not_repost(self):
        number = self.sheet.add(QUEUE_TAB, queue_row())
        await self.poll()
        rid = self.sheet.row(QUEUE_TAB, number)["reachout_id"]
        job = self.repo.claim_jobs(utcnow(), 10)[0]
        with patch.object(self.repo, "mark_delivered", side_effect=RuntimeError("process died")):
            await self.service.run_job(job)
        self.assertEqual(self.repo.get(rid)["dispatch_state"], "SENDING")
        job = self.repo.one("SELECT * FROM reachout_jobs WHERE id=?", (job["id"],))
        self.assertEqual(job["in_flight"], 1)
        await self.service.run_job(job)
        self.assertEqual(len(self.bot.channels[CHANNEL_ID].sent), 1)
        self.assertEqual(self.repo.get(rid)["dispatch_state"], "DELIVERED")

    async def test_a_definite_discord_refusal_retries_and_is_visible(self):
        self.bot.channels[CHANNEL_ID].fail_next = http_error(discord.HTTPException, 400)
        number = self.sheet.add(QUEUE_TAB, queue_row())
        await self.poll()
        job = self.repo.claim_jobs(utcnow(), 10)[0]
        await self.service.run_job(job)
        stored = self.repo.one("SELECT * FROM reachout_jobs WHERE id=?", (job["id"],))
        self.assertEqual((stored["in_flight"], stored["attempts"], stored["status"]), (0, 1, "PENDING"))
        await self.poll()
        row = self.sheet.row(QUEUE_TAB, number)
        self.assertEqual(row["processing_status"], "SENDING")
        self.assertIn("Retrying", row["status_detail"])
        self.assertIn("HTTPException", row["status_detail"])

    async def test_unsafe_channel_audience_blocks_delivery(self):
        self.bot.channels[CHANNEL_ID].members.append(self.bot.guild.members[OTHER_ID])
        number = self.sheet.add(QUEUE_TAB, queue_row())
        await self.poll()
        await self.jobs()
        await self.poll()
        self.assertEqual(self.sheet.row(QUEUE_TAB, number)["processing_status"], "ERROR")
        self.assertIn("other member", self.sheet.row(QUEUE_TAB, number)["status_detail"])
        self.assertEqual(self.bot.channels[CHANNEL_ID].sent, [])


def rec_time(value):
    return datetime.fromisoformat(value)


class ResponseTests(WorkflowBase):
    async def test_wrong_users_cannot_submit_or_act_as_lead(self):
        _, rid = await self.deliver()
        with self.assertRaises(PermissionError):
            self.service.agent_form(rid, OTHER_ID)
        with self.assertRaises(PermissionError):
            await self.submit(rid, user_id=LEAD_ID)
        response = await self.submit(rid)
        with self.assertRaises(PermissionError):
            await self.service.lead_acknowledge(Interaction.make(OTHER_ID), response["response_id"])
        with self.assertRaises(PermissionError):
            await self.service.submit_lead_reply(Interaction.make(AGENT_ID), response["response_id"], False, "hi")

    async def test_opening_the_form_is_not_acknowledgment(self):
        _, rid = await self.deliver()
        form = self.service.agent_form(rid, AGENT_ID)
        self.assertEqual((form["title"], form["text_required"]), ("Acknowledge & respond", True))
        self.assertIn("does not necessarily mean you agree", form["notice"])
        self.assertEqual(self.repo.get(rid)["ack_status"], "PENDING")

    async def test_acknowledgment_always_requires_written_text(self):
        _, rid = await self.deliver(response_text_required="FALSE")
        self.assertTrue(self.service.agent_form(rid, AGENT_ID)["text_required"])
        with self.assertRaisesRegex(ValueError, "written response is required"):
            await self.submit(rid, text="")
        self.assertEqual(self.repo.get(rid)["ack_status"], "PENDING")
        response = await self.submit(rid, text="Here is why.")
        self.assertEqual(response["response_type"], "AGENT_ACKNOWLEDGMENT_WITH_RESPONSE")
        self.assertEqual(self.repo.get(rid)["ack_status"], "ACKNOWLEDGED")
        self.assertEqual(self.repo.get(rid)["written_status"], "RECEIVED")

    async def test_submission_reaches_thread_lead_and_sheets_once(self):
        _, rid = await self.deliver()
        interaction = Interaction.make(AGENT_ID)
        ob = self.repo.open_obligation(rid)["obligation_id"]
        first = await self.service.submit_agent(interaction, rid, ob, "Understood.")
        replayed = await self.service.submit_agent(interaction, rid, ob, "Understood.")
        self.assertEqual(first["response_id"], replayed["response_id"])
        await self.jobs()
        thread = self.thread(rid)
        self.assertEqual(len(thread.sent), 1)
        embed = thread.sent[0].kwargs["embed"]
        self.assertIn("Understood.", embed.description)
        self.assertEqual(embed.author.name, "Agent One — agent")
        dm = self.bot.users[LEAD_ID].dm_channel
        self.assertEqual(len(dm.sent), 1)
        labels = [getattr(item, "item", item).label for item in dm.sent[0].kwargs["view"].children]
        self.assertEqual(labels, ["Reply", "Reply — response required", "Acknowledge", "Open thread"])
        await self.service.sync_records()
        self.assertEqual(self.sheet.records[RESPONSES_TAB][first["response_id"]]["agent_email"], "agent@example.com")
        stored = self.repo.get_response(first["response_id"])
        self.assertEqual((stored["thread_post_status"], stored["lead_notify_status"]), ("POSTED", "DM_SENT"))

    async def test_weekly_performance_has_no_buttons_footer_or_thread(self):
        _, rid = await self.deliver(reachout_type="Weekly performance")
        sent = self.bot.channels[CHANNEL_ID].sent[0].kwargs
        self.assertEqual(sent["view"].children, [])
        self.assertEqual((sent["embed"].title, sent["embed"].fields, sent["embed"].footer.text),
                         ("Tone in chat", [], None))
        self.assertIsNone(self.repo.get(rid)["thread_id"])

    async def test_additional_weekly_submissions_are_distinct_and_each_notifies(self):
        _, rid = await self.deliver(reachout_type="Weekly performance")
        self.assertIsNone(self.repo.open_obligation(rid))
        self.assertEqual(self.service.agent_form(rid, AGENT_ID)["title"], "Respond")
        with self.assertRaisesRegex(ValueError, "written response"):
            await self.submit(rid, text="")
        await self.submit(rid, text="Thanks")
        self.assertEqual(self.service.agent_form(rid, AGENT_ID)["title"], "Add response")
        await self.submit(rid, text="One more thing")
        await self.jobs()
        self.assertEqual(len(self.bot.users[LEAD_ID].dm_channel.sent), 2)
        self.assertEqual(self.service.schedule_nudges(utcnow() + timedelta(days=30)), 0)

    async def test_a_form_opened_for_an_obligation_that_closed_is_rejected(self):
        _, rid = await self.deliver()
        await self.submit(rid)
        with self.assertRaises(StaleForm):
            await self.submit(rid, obligation="OBL-20260101-00000000", text="late")

    async def test_lead_ack_and_reply_apply_to_one_response_and_never_resolve(self):
        _, rid = await self.deliver(reachout_type="Weekly performance")
        first = await self.submit(rid, text="first")
        second = await self.submit(rid, text="second")
        _, created = await self.service.lead_acknowledge(Interaction.make(LEAD_ID), first["response_id"])
        self.assertTrue(created)
        _, created = await self.service.lead_acknowledge(Interaction.make(LEAD_ID), first["response_id"])
        self.assertFalse(created)
        self.assertEqual(self.repo.get_response(second["response_id"])["review_status"], "UNREVIEWED")
        reply = await self.service.submit_lead_reply(Interaction.make(LEAD_ID), second["response_id"], False, "Noted")
        self.assertEqual(self.repo.get_response(second["response_id"])["review_status"], "REPLIED")
        self.assertIsNone(reply["created_obligation_id"])
        self.assertEqual(self.repo.get(rid)["workflow_status"], "OPEN")
        await self.jobs()
        thread = self.thread(rid)
        self.assertEqual([m.kwargs["embed"].title for m in thread.sent],
                         ["Response", "Response", "Response reviewed", "Team lead reply"])
        self.assertIn("does not resolve", thread.sent[2].kwargs["embed"].description)
        self.assertEqual(thread.sent[3].kwargs["embed"].author.name, f"user{LEAD_ID} — team lead")

    async def test_requesting_a_response_creates_one_new_obligation(self):
        _, rid = await self.deliver(reachout_type="Weekly performance")
        response = await self.submit(rid, text="first")
        reply = await self.service.submit_lead_reply(Interaction.make(LEAD_ID), response["response_id"], True, "Explain?")
        ob = self.repo.get_obligation(reply["created_obligation_id"])
        self.assertEqual((ob["status"], ob["obligation_type"], ob["text_required"]),
                         ("AWAITING_DELIVERY", "FOLLOW_UP_RESPONSE", 1))
        again = await self.service.submit_lead_reply(Interaction.make(LEAD_ID), response["response_id"], True, "Please?")
        self.assertEqual(self.repo.get_obligation(ob["obligation_id"])["status"], "SUPERSEDED")
        await self.jobs()
        current = self.repo.open_obligation(rid)
        self.assertEqual((current["obligation_id"], current["status"]), (again["created_obligation_id"], "PENDING"))
        self.assertIsNotNone(current["due_at"])
        thread = self.thread(rid)
        self.assertEqual([u.id for u in thread.sent[-1].kwargs["allowed_mentions"].users], [AGENT_ID])
        self.assertEqual(self.service.agent_form(rid, AGENT_ID)["title"], "Respond to follow-up")
        answer = await self.submit(rid, text="Because")
        self.assertEqual(answer["obligation_id"], current["obligation_id"])
        self.assertEqual(self.repo.get_obligation(current["obligation_id"])["status"], "SATISFIED")

    async def test_reachouts_cancelled_before_the_checkbox_queue_keep_history_and_refuse_new_actions(self):
        number, rid = await self.deliver()
        response = await self.submit(rid, text="hi")
        self.repo.close(rid, "CANCELLED", utcnow())  # cancel is no longer offered; older ones remain
        await self.jobs()
        await self.poll()
        self.assertEqual(self.repo.get(rid)["workflow_status"], "CANCELLED")
        self.assertIsNotNone(self.repo.get_response(response["response_id"]))
        with self.assertRaises(WorkflowError):
            self.service.agent_form(rid, AGENT_ID)
        with self.assertRaises(WorkflowError):
            await self.service.submit_lead_reply(Interaction.make(LEAD_ID), response["response_id"], False, "x")
        self.assertEqual(self.thread(rid).sent[-1].kwargs["embed"].title, "Cancelled")
        self.assertEqual(self.sheet.records[HISTORY_TAB][rid]["status"], "CANCELLED")

    async def test_reachouts_resolved_before_the_checkbox_queue_allow_optional_responses(self):
        number, rid = await self.deliver()
        self.repo.close(rid, "RESOLVED", utcnow())
        self.assertEqual(self.repo.get(rid)["workflow_status"], "RESOLVED")
        self.assertEqual(self.repo.obligations_for(rid)[0]["status"], "CLOSED_RESOLVED")
        self.assertEqual(self.service.agent_form(rid, AGENT_ID)["title"], "Add response")
        response = await self.submit(rid, text="ok")
        with self.assertRaisesRegex(WorkflowError, "resolved"):
            await self.service.submit_lead_reply(Interaction.make(LEAD_ID), response["response_id"], True, "more?")

    async def test_row_deletion_keeps_workflow_state(self):
        number, rid = await self.deliver()
        del self.sheet.grid[QUEUE_TAB][number - 1]
        await self.poll()
        self.assertEqual(self.repo.get(rid)["row_missing"], 1)
        self.assertEqual(self.repo.open_obligation(rid)["status"], "PENDING")
        restored = self.sheet.add(QUEUE_TAB, {"reachout_id": rid})
        await self.poll()
        self.assertEqual(self.sheet.row(QUEUE_TAB, restored)["processing_status"], "SENT")


class NudgeTests(WorkflowBase):
    async def overdue(self, **row):
        """A lead nudge DM as the bot sent them before overdue items were reminded automatically.

        The scheduler no longer creates these, but DMs already sent keep working buttons.
        """
        _, rid = await self.deliver(**row)
        ob = self.repo.open_obligation(rid)
        self.assertTrue(self.repo.create_nudge(ob["obligation_id"], ob["next_nudge_at"], LEAD_ID, None, utcnow()))
        await self.jobs()
        return rid, self.repo.one("SELECT * FROM reachout_nudges WHERE reachout_id=?", (rid,))

    async def test_nothing_is_overdue_before_seven_days(self):
        _, rid = await self.deliver()
        self.assertEqual(self.service.schedule_nudges(utcnow() + timedelta(days=6, hours=23)), 0)

    async def test_overdue_items_get_one_standard_reminder_in_the_thread_and_no_lead_dm(self):
        _, rid = await self.deliver()
        later = utcnow() + timedelta(days=7, minutes=1)
        self.assertEqual(self.service.schedule_nudges(later), 1)
        self.assertEqual(self.service.schedule_nudges(later + timedelta(days=5)), 0)  # no repeated barrage
        await self.jobs()
        sent = self.thread(rid).sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].kwargs["content"], f"<@{AGENT_ID}>")
        self.assertEqual([u.id for u in sent[0].kwargs["allowed_mentions"].users], [AGENT_ID])
        embed = sent[0].kwargs["embed"]
        self.assertEqual(embed.title, "Reminder")
        self.assertIn('Reminder: your acknowledgment and written response for "Tone in chat" was due', embed.description)
        self.assertNotIn(LEAD_ID, self.bot.users)  # nothing was sent to the lead
        self.assertIsNone(self.repo.one("SELECT * FROM reachout_nudges WHERE reachout_id=?", (rid,)))
        self.assertEqual(self.repo.open_obligation(rid)["reminder_count"], 1)

    async def test_the_automatic_pass_reminds_and_retries_without_touching_sheets(self):
        _, rid = await self.deliver()

        class NoSheet:
            def __getattr__(self, name):
                raise AssertionError(f"the automatic pass called Sheets {name}")

        self.service.sheet = NoSheet()
        self.service.access_sheet = NoSheet()
        later = utcnow() + timedelta(days=7, minutes=1)
        with patch("reachouts.service.utcnow", return_value=later), \
                patch("reachouts.repository.utcnow", return_value=later):
            self.assertEqual(await self.service.background_tick(), 1)
        self.assertEqual(self.thread(rid).sent[0].kwargs["embed"].title, "Reminder")
        self.assertEqual(self.alerts, [])  # a swallowed Sheets call would show up as an alert

    async def test_the_loop_runs_only_the_automatic_pass(self):
        calls = []

        async def background_tick():
            calls.append("background")

        async def tick(*args, **kwargs):
            calls.append("poll")

        self.service.background_tick, self.service.tick = background_tick, tick
        await ReachoutLoop.loop.coro(ReachoutLoop(self.bot, self.service))
        self.assertEqual(calls, ["background"])

    async def test_no_automatic_reminder_after_the_agent_responds(self):
        _, rid = await self.deliver()
        await self.submit(rid)
        await self.jobs()
        self.assertEqual(self.service.schedule_nudges(utcnow() + timedelta(days=8)), 0)

    async def test_lead_nudge_dms_already_sent_keep_their_controls(self):
        rid, nudge = await self.overdue()
        dm = self.bot.users[LEAD_ID].dm_channel
        self.assertEqual(len(dm.sent), 1)
        self.assertTrue(dm.sent[0].kwargs["embed"].title.startswith("Overdue"))
        labels = [getattr(item, "item", item).label for item in dm.sent[0].kwargs["view"].children]
        self.assertEqual(labels, ["Send reminder", "Custom reminder", "Snooze", "Open thread"])
        self.assertEqual(self.thread(rid).sent, [])
        self.assertEqual(nudge["status"], "SENT")

    async def test_reminder_rechecks_state_and_cannot_duplicate(self):
        rid, nudge = await self.overdue()
        await self.service.remind(Interaction.make(LEAD_ID), nudge["nudge_id"], None)
        with self.assertRaisesRegex(WorkflowError, "Already handled"):
            await self.service.remind(Interaction.make(LEAD_ID), nudge["nudge_id"], None)
        await self.jobs()
        thread = self.thread(rid)
        self.assertEqual(len(thread.sent), 1)
        self.assertEqual(thread.sent[0].kwargs["content"], f"<@{AGENT_ID}>")
        self.assertEqual([u.id for u in thread.sent[0].kwargs["allowed_mentions"].users], [AGENT_ID])
        ob = self.repo.open_obligation(rid)
        self.assertEqual((ob["reminder_count"], ob["last_reminder_by"]), (1, LEAD_ID))

    async def test_agent_response_makes_reminder_actions_stale(self):
        rid, nudge = await self.overdue()
        await self.submit(rid)
        with self.assertRaisesRegex(WorkflowError, "No longer needed"):
            await self.service.remind(Interaction.make(LEAD_ID), nudge["nudge_id"], "custom")
        with self.assertRaises(WorkflowError):
            self.service.nudge_context(nudge["nudge_id"], LEAD_ID)

    async def test_a_reminder_queued_just_before_the_agent_responds_is_not_posted(self):
        rid, nudge = await self.overdue()
        await self.service.remind(Interaction.make(LEAD_ID), nudge["nudge_id"], None)
        await self.submit(rid)
        await self.jobs()
        titles = [m.kwargs["embed"].title for m in self.thread(rid).sent]
        self.assertNotIn("Reminder from your team lead", titles)

    async def test_snooze_defers_the_nudge_but_not_the_due_date(self):
        rid, nudge = await self.overdue()
        due_before = self.repo.open_obligation(rid)["due_at"]
        until = await self.service.snooze(Interaction.make(LEAD_ID), nudge["nudge_id"], "1bd")
        ob = self.repo.open_obligation(rid)
        self.assertEqual((ob["due_at"], ob["next_nudge_at"]), (due_before, iso(until)))
        self.assertEqual(self.service.schedule_nudges(until + timedelta(minutes=1)), 0)  # not yet overdue then
        later = max(until, datetime.fromisoformat(due_before)) + timedelta(minutes=1)
        self.assertEqual(self.service.schedule_nudges(later), 1)

    async def test_lead_reassignment_revokes_old_controls_and_reroutes_work(self):
        rid, nudge = await self.overdue()
        self.roster[0]["Team Lead Email"] = "newlead@example.com"
        await self.poll()
        with self.assertRaises(PermissionError):
            await self.service.remind(Interaction.make(LEAD_ID), nudge["nudge_id"], None)
        self.assertEqual(self.repo.get_nudge(nudge["nudge_id"])["status"], "STALE")
        await self.jobs()
        channel = self.bot.channels[CHANNEL_ID]
        self.assertTrue(channel.ow[NEW_LEAD_ID].view_channel)
        self.assertIsNone(channel.ow[LEAD_ID])

    async def test_closed_dms_without_a_safe_fallback_are_recorded(self):
        self.bot.users[LEAD_ID] = FakeUser(self.bot, LEAD_ID, dms_open=False)
        _, rid = await self.deliver()
        response = await self.submit(rid)
        await self.jobs()
        stored = self.repo.get_response(response["response_id"])
        self.assertEqual(stored["lead_notify_status"], "FAILED")
        self.assertIn("no fallback_channel_id", stored["last_error"])
        self.assertIn("lead_dm_failed", self.alerts)

    async def test_closed_dms_use_a_restricted_fallback_channel(self):
        fallback = FakeChannel(self.bot, 666666666666666666, self.bot.guild)
        fallback.members = [self.bot.guild.members[LEAD_ID]]
        self.bot.channels[fallback.id] = fallback
        self.sheet.set(AGENTS_TAB, self.agent_row, fallback_channel_id=str(fallback.id))
        self.bot.users[LEAD_ID] = FakeUser(self.bot, LEAD_ID, dms_open=False)
        _, rid = await self.deliver()
        response = await self.submit(rid)
        await self.jobs()
        self.assertEqual(self.repo.get_response(response["response_id"])["lead_notify_status"], "FALLBACK_SENT")
        self.assertEqual([u.id for u in fallback.sent[0].kwargs["allowed_mentions"].users], [LEAD_ID])

    async def test_cancelled_reachouts_get_no_reminders(self):
        number, rid = await self.deliver()
        self.repo.close(rid, "CANCELLED", utcnow())
        self.assertEqual(self.service.schedule_nudges(utcnow() + timedelta(days=30)), 0)
        self.assertEqual(self.repo.obligations_for(rid)[0]["status"], "CANCELLED")


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_buttons_route_by_custom_id_after_restart(self):
        rid, rsp, ndg = "RO-20261028-ABCDEF12", "RSP-20261028-ABCDEF12", "NDG-20261028-ABCDEF12"
        cases = [
            (views.AgentRespondButton(rid), f"ro:agent:{rid}"),
            (views.LeadReplyButton(rsp, True), f"ro:lrr:{rsp}"),
            (views.LeadReplyButton(rsp, False), f"ro:lr:{rsp}"),
            (views.LeadAckButton(rsp), f"ro:lack:{rsp}"),
            (views.NudgeButton("snz", ndg), f"ro:snz:{ndg}"),
        ]
        for item, custom_id in cases:
            self.assertEqual(item.item.custom_id, custom_id)
            self.assertLessEqual(len(custom_id), 100)
            rebuilt = await type(item).from_custom_id(None, None, item.template.fullmatch(custom_id))
            self.assertEqual(rebuilt.item.custom_id, custom_id)

    async def test_pending_work_survives_a_new_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opsbot.db"
            first = ReachoutRepository(path)
            first.init()
            first.enqueue("update_original", "RO-20261028-ABCDEF12")
            second = ReachoutRepository(path)
            second.init()
            self.assertEqual(len(second.claim_jobs(utcnow(), 10)), 1)
            self.assertEqual(second.claim_jobs(utcnow(), 10), [])  # leased: an overlapping run cannot take it



class FakeSpreadsheet:
    def __init__(self, tabs):
        self.tabs = tabs
        self.batch_updates, self.appends = [], []

    def values_batch_get(self, ranges, params=None):
        return {"valueRanges": [{"values": self.tabs[r.strip("'")]} for r in ranges]}

    def values_batch_update(self, body):
        self.batch_updates.append(body)

    def values_append(self, range_name, params=None, body=None):
        self.appends.append((range_name, params, body))


class SheetAdapterTests(unittest.TestCase):
    def test_headers_are_found_by_name_and_writes_are_raw_and_minimal(self):
        from reachouts.sheets import EVENTS, EVENTS_TAB, ReachoutSheet

        headers = list(reversed(EVENTS)) + ["operator notes"]
        existing = {h: "" for h in headers} | {"event_id": "EVT-1", "outcome": "OK", "details": "old"}
        spreadsheet = FakeSpreadsheet({EVENTS_TAB: [headers, [existing[h] for h in headers]]})
        sheet = ReachoutSheet(None, "id")
        sheet._spreadsheet = spreadsheet
        unchanged = {h: existing.get(h, "") for h in EVENTS}
        sheet.upsert({EVENTS_TAB: ("event_id", [
            unchanged | {"details": "=HYPERLINK(\"x\")", "synced_at": "now"},
            {h: "" for h in EVENTS} | {"event_id": "EVT-2", "synced_at": "now"},
        ])})
        body = spreadsheet.batch_updates[0]
        self.assertEqual(body["valueInputOption"], "RAW")
        written = {d["range"]: d["values"][0][0] for d in body["data"]}
        details_col = headers.index("details") + 1
        from gspread.utils import rowcol_to_a1
        self.assertEqual(written[f"'{EVENTS_TAB}'!{rowcol_to_a1(2, details_col)}"], "=HYPERLINK(\"x\")")
        self.assertEqual(len(written), 2)  # details and synced_at only
        _, params, appended = spreadsheet.appends[0]
        self.assertEqual(params["valueInputOption"], "RAW")
        self.assertEqual(appended["values"][0][headers.index("event_id")], "EVT-2")
        self.assertEqual(len(appended["values"][0]), len(headers))

    def test_missing_headers_are_reported_with_the_fix(self):
        from reachouts.sheets import SheetSchemaError

        table = Table(QUEUE_TAB, [["dispatch_action", "agent_id"]])
        with self.assertRaisesRegex(SheetSchemaError, "reachout-sheet-setup"):
            table.require(TAB_HEADERS[QUEUE_TAB])


class FakeImage:
    def __init__(self, filename="screen.png", content_type="image/png", size=1000, data=b"png-bytes"):
        self.filename, self.content_type, self.size, self.data = filename, content_type, size, data

    async def save(self, path):
        Path(path).write_bytes(self.data)


class HistoryTests(WorkflowBase):
    def history(self, rid):
        return self.sheet.records[HISTORY_TAB][rid]

    async def test_history_keeps_the_content_and_the_conversation_in_order(self):
        number, rid = await self.deliver(title="Tone in chat", message_body="Please review the attached chat.")
        del self.sheet.grid[QUEUE_TAB][number - 1]  # the operator clears the queue; History is unaffected
        await self.poll(send=False)
        row = self.history(rid)
        self.assertEqual((row["title"], row["message"], row["agent_email"], row["reachout_type"]),
                         ("Tone in chat", "Please review the attached chat.", "agent@example.com", "Agent behaviors"))
        self.assertEqual((row["status"], row["needs_tl_reply"], row["agent_reply_1"]), ("AWAITING_AGENT", "FALSE", ""))
        self.assertTrue(row["sent_at"] and row["due_at"] and row["thread_url"])

        first = await self.submit(rid, text="It was a macro.")
        await self.poll(send=False)
        row = self.history(rid)
        self.assertTrue(row["agent_reply_1"].startswith("It was a macro.\n— Agent One (agent), "))
        self.assertEqual((row["status"], row["needs_tl_reply"]), ("AWAITING_TL", "TRUE"))

        await self.service.submit_lead_reply(Interaction.make(LEAD_ID), first["response_id"], True, "Which macro?")
        await self.submit(rid, text="The greeting one.")
        await self.submit(rid, text="Also the closing one.")
        await self.poll(send=False)
        row = self.history(rid)
        self.assertIn("Which macro?\n— ", row["tl_reply"])
        self.assertIn("(team lead)", row["tl_reply"])
        self.assertTrue(row["agent_reply_2"].startswith("The greeting one."))
        self.assertTrue(row["later_replies"].startswith("Also the closing one."))
        self.assertEqual(row["needs_tl_reply"], "TRUE")
        await self.service.lead_acknowledge(Interaction.make(LEAD_ID), self.repo.responses_for(rid)[-1]["response_id"])
        await self.poll(send=False)
        self.assertEqual((self.history(rid)["status"], self.history(rid)["needs_tl_reply"]), ("COMPLETE", "FALSE"))

    async def test_overdue_weekly_and_unsent_reachouts(self):
        _, rid = await self.deliver()
        _, weekly = await self.deliver(reachout_type="Weekly performance", title="Week 1")
        self.sheet.add(QUEUE_TAB, queue_row(send="FALSE", title="Draft"))
        with patch("reachouts.service.utcnow", return_value=utcnow() + timedelta(days=8)):
            await self.poll(send=False)
        self.assertEqual(self.history(rid)["status"], "OVERDUE")
        self.assertEqual(self.history(weekly)["status"], "SENT")
        self.assertEqual(len(self.sheet.records[HISTORY_TAB]), 2)  # drafts never appear

    async def test_unchanged_rows_are_not_rewritten_until_a_full_pass(self):
        _, rid = await self.deliver()
        await self.poll(send=False)
        calls = []
        real = self.sheet.upsert
        self.sheet.upsert = lambda batches: calls.append(batches) or real(batches)
        await self.poll(send=False)
        self.assertFalse(any(HISTORY_TAB in b for b in calls))
        await self.service.poll_once(full=True)  # the hourly reconcile, and the backfill after a restart
        self.assertTrue(any(HISTORY_TAB in b for b in calls))


class ImageAttachmentTests(WorkflowBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch("reachouts.service.ATTACHMENT_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = Path(tmp.name)

    async def test_images_are_posted_with_the_response_in_the_thread(self):
        _, rid = await self.deliver()
        response = await self.submit(rid, images=[FakeImage("a.png"), FakeImage("b.jpg", "image/jpeg")])
        await self.jobs()
        sent = self.thread(rid).sent[0].kwargs
        self.assertEqual([f.filename for f in sent["files"]], ["1-a.png", "2-b.jpg"])
        self.assertIn("2 attached", str(sent["embed"].fields))
        dm = self.bot.users[LEAD_ID].dm_channel.sent[0].kwargs
        self.assertIn("see the thread", str(dm["embed"].fields))
        self.assertEqual(len(json.loads(self.repo.get_response(response["response_id"])["attachments"])), 2)
        await self.poll(send=False)
        row = self.sheet.records[HISTORY_TAB][rid]
        self.assertEqual(row["images"], "2")
        self.assertIn("[2 image(s)]", row["agent_reply_1"])

    async def test_responses_without_images_post_no_files(self):
        _, rid = await self.deliver()
        await self.submit(rid)
        await self.jobs()
        self.assertEqual(self.thread(rid).sent[0].kwargs["files"], [])

    async def test_only_up_to_four_images_under_eight_mb_are_accepted(self):
        _, rid = await self.deliver()
        for images, message in (([FakeImage("notes.pdf", "application/pdf")], "not an image"),
                                ([FakeImage(size=9 * 1024 * 1024)], "larger than 8 MB"),
                                ([FakeImage(f"{n}.png") for n in range(5)], "at most 4")):
            with self.assertRaisesRegex(ValueError, message):
                await self.submit(rid, images=images)
        self.assertEqual(self.repo.get(rid)["ack_status"], "PENDING")
        self.assertEqual(list(self.folder.iterdir()), [])

    async def test_images_from_a_rejected_submission_are_deleted(self):
        _, rid = await self.deliver()
        with self.assertRaisesRegex(ValueError, "written response is required"):
            await self.submit(rid, text="", images=[FakeImage()])
        self.assertEqual(list(self.folder.iterdir()), [])


class ModalPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_forms_serialize_within_discord_limits(self):
        form = {"title": "Acknowledge & respond", "obligation_id": "OBL-1", "text_required": False,
                "notice": "**Opt outs: x**\n\n" + CONFIG.acknowledgment_notice, "label": "Written response (optional)",
                "description": "Records receipt/review, not necessarily agreement."}
        response = {"text": "", "response_id": "RSP-1"}
        for modal in (views.AgentResponseModal("RO-1", form), views.LeadReplyModal("RSP-1", True, response),
                      views.CustomReminderModal("NDG-1")):
            payload = modal.to_dict()
            self.assertLessEqual(len(payload["title"]), 45)
            kinds = [component["type"] for component in payload["components"]]
            # Text Display, then a Label wrapping the text input; the agent form adds a Label wrapping a file upload.
            self.assertEqual(kinds, [10, 18, 18] if isinstance(modal, views.AgentResponseModal) else [10, 18])
            for label in payload["components"][1:]:
                self.assertLessEqual(len(label["label"]), 45)
                self.assertLessEqual(len(label.get("description", "")), 100)
            self.assertEqual(payload["components"][1]["component"]["type"], 4)
            if isinstance(modal, views.AgentResponseModal):
                upload = payload["components"][2]["component"]
                self.assertEqual((upload["type"], upload["required"], upload["max_values"]), (19, False, 4))


class FakeWorksheet:
    def __init__(self, title, sheet_id, rows=None):
        self.title, self.id, self.rows, self.col_count = title, sheet_id, rows or [], 26

    def update(self, range_name, values, value_input_option=None):
        if range_name == "A1":
            self.rows[:1] = [list(values[0])]
        else:
            self.rows[0].extend(values[0])

    def row_values(self, number):
        return list(self.rows[number - 1]) if len(self.rows) >= number else []

    def append_rows(self, rows, value_input_option=None):
        self.rows.extend(rows)

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def update_title(self, title):
        self.title = title

    def add_cols(self, count):
        self.col_count += count


class FakeTemplateSpreadsheet:
    def __init__(self, existing):
        self.sheets = existing
        self.requests = []

    def worksheets(self):
        return list(self.sheets.values())

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWorksheet(title, len(self.sheets) + 1)
        return self.sheets[title]

    def del_worksheet(self, worksheet):
        del self.sheets[worksheet.title]

    def fetch_sheet_metadata(self, params=None):
        return {"sheets": [{"protectedRanges": []}]}

    def batch_update(self, body):
        self.requests.extend(body["requests"])


class SyncSheetsTests(WorkflowBase):
    """/reachout_sync refreshes Sheets from SQLite and must never release a ticked row or run a job."""

    async def test_sync_restores_history_and_sends_nothing(self):
        number, rid = await self.deliver(title="Already sent")
        await self.poll(send=False)  # History is written on the poll after delivery
        del self.sheet.records[HISTORY_TAB][rid]  # simulate History lost from Sheets
        ticked = self.sheet.add(QUEUE_TAB, queue_row(title="Ticked, not released"))
        sent_before = len(self.bot.channels[CHANNEL_ID].sent)
        with patch.object(self.service, "run_job", AsyncMock(side_effect=AssertionError("a job ran"))) as run_job:
            stats = await self.service.sync_sheets()
        run_job.assert_not_awaited()
        self.assertIn(rid, self.sheet.records[HISTORY_TAB])
        self.assertEqual(self.sheet.row(QUEUE_TAB, ticked)["reachout_id"], "")
        self.assertEqual(len(self.bot.channels[CHANNEL_ID].sent), sent_before)
        self.assertNotIn("poll_error", stats)


class TemplateSetupTests(unittest.TestCase):
    def test_setup_moves_the_old_queue_aside_and_carries_only_unsent_rows(self):
        from reachouts.sheets import ReachoutSheet

        headers = ["dispatch_action", "agent_email", "title", "message_body", "operator_notes", "reachout_id",
                   "processing_status"]
        old_queue = FakeWorksheet(QUEUE_TAB, 99, [headers,
                                                  ["READY", "a@x.com", "Ready one", "body", "n1", "", ""],
                                                  ["DRAFT", "b@x.com", "Draft one", "body", "n2", "", ""],
                                                  ["READY", "c@x.com", "Sent one", "body", "", "RO-1", "DELIVERED"],
                                                  ["READY", "d@x.com", "Invalid one", "", "", "RO-2", "INVALID"]])
        old_agents = FakeWorksheet(AGENTS_TAB, 98, [["agent_email", "my notes"], ["a@x.com", "keep"]])
        spreadsheet = FakeTemplateSpreadsheet({QUEUE_TAB: old_queue, AGENTS_TAB: old_agents})
        sheet = ReachoutSheet(None, "id")
        sheet._spreadsheet = spreadsheet
        report = sheet.setup("bot@project.iam.gserviceaccount.com")
        self.assertTrue(old_queue.title.startswith(f"{QUEUE_TAB} (old "))
        self.assertEqual(len(old_queue.rows), 5)  # the backup keeps everything
        queue = spreadsheet.sheets[QUEUE_TAB]
        self.assertIsNot(queue, old_queue)
        self.assertEqual(queue.rows[0], TAB_HEADERS[QUEUE_TAB])
        rows = [dict(zip(queue.rows[0], row)) for row in queue.rows[1:]]
        self.assertEqual([(r["title"], r["send"]) for r in rows],
                         [("Ready one", "TRUE"), ("Draft one", "FALSE"), ("Invalid one", "TRUE")])
        self.assertEqual(rows[2]["reachout_id"], "RO-2")
        self.assertTrue(any("Copied 3 unsent" in line for line in report))
        self.assertEqual(old_agents.rows[1], ["a@x.com", "keep"])  # other tabs: operator data untouched
        self.assertTrue(any(f"Added headers to {AGENTS_TAB!r}" in line for line in report))
        hidden = {r["updateSheetProperties"]["properties"]["sheetId"] for r in spreadsheet.requests
                  if "updateSheetProperties" in r and r["updateSheetProperties"]["fields"] == "hidden"}
        self.assertEqual(hidden, {spreadsheet.sheets[t].id for t in (RESPONSES_TAB, "Reachout Obligations", "Reachout Events")})
        # Running setup again leaves the new queue alone.
        sheet.setup("bot@project.iam.gserviceaccount.com")
        self.assertIs(spreadsheet.sheets[QUEUE_TAB], queue)

    def test_setup_creates_safe_tabs(self):
        from reachouts.sheets import CONFIG_TAB, QUEUE_BOT, ReachoutSheet

        sheet = ReachoutSheet(None, "id")
        created = FakeTemplateSpreadsheet({})
        sheet._spreadsheet = created
        sheet.setup("bot@project.iam.gserviceaccount.com")
        queue = created.sheets[QUEUE_TAB]
        samples = [dict(zip(queue.rows[0], row)) for row in queue.rows[1:]]
        self.assertEqual(sorted(r["reachout_type"] for r in samples),
                         sorted(["Agent behaviors", "Flagged messages", "Opt outs", "Weekly performance"] * 2))
        self.assertEqual(sum("**" in r["message_body"] for r in samples), 4)
        self.assertTrue(all(r["send"] == "FALSE" and r["agent_email"].endswith("@example.invalid") for r in samples))
        self.assertTrue(all(validate_row(r | {"send": "TRUE"}, CONFIG, utcnow())[1] for r in samples))
        self.assertEqual(created.sheets[HISTORY_TAB].rows[0], TAB_HEADERS[HISTORY_TAB])
        agents = created.sheets[AGENTS_TAB]
        self.assertEqual(dict(zip(agents.rows[0], agents.rows[1]))["active"], "FALSE")
        self.assertEqual({row[0] for row in created.sheets[CONFIG_TAB].rows[1:]},
                         {"timezone", "business_weekdays", "default_deadline_business_days",
                          "nudge_repeat_business_days", "reminder_template", "acknowledgment_notice"})
        validations = [r["setDataValidation"] for r in created.requests if "setDataValidation" in r]
        checkbox = [v for v in validations if v["rule"]["condition"]["type"] == "BOOLEAN"]
        self.assertEqual([(v["range"]["sheetId"], v["range"]["startColumnIndex"]) for v in checkbox],
                         [(created.sheets[QUEUE_TAB].id, 0)])
        protected = [r["addProtectedRange"]["protectedRange"] for r in created.requests if "addProtectedRange" in r]
        bot_locks = [p for p in protected if not p["warningOnly"]]
        self.assertTrue(all(p["editors"]["users"] == ["bot@project.iam.gserviceaccount.com"] for p in bot_locks))
        queue_locks = {p["description"].rsplit(" ", 1)[-1] for p in bot_locks if QUEUE_TAB in p["description"]}
        self.assertEqual(queue_locks, set(QUEUE_BOT))
        text_formats = [r for r in created.requests if "repeatCell" in r
                        and r["repeatCell"]["fields"] == "userEnteredFormat.numberFormat"]
        self.assertEqual(len(text_formats), 5)
        history_lock = [p for p in bot_locks if p["description"].endswith(HISTORY_TAB)]
        self.assertEqual(len(history_lock), 1)


    def test_recreate_deletes_only_reachout_tabs(self):
        from reachouts.sheets import ReachoutSheet

        spreadsheet = FakeTemplateSpreadsheet({QUEUE_TAB: FakeWorksheet(QUEUE_TAB, 1, [["agent_id"]]),
                                               "Reachout Holidays": FakeWorksheet("Reachout Holidays", 3, [["date"]]),
                                               "My notes": FakeWorksheet("My notes", 2, [["keep"]])})
        sheet = ReachoutSheet(None, "id")
        sheet._spreadsheet = spreadsheet
        self.assertEqual(sheet.delete_tabs(), [f"Deleted tab {QUEUE_TAB!r}.", "Deleted tab 'Reachout Holidays'."])
        self.assertEqual(set(spreadsheet.sheets), {"My notes"})
        sheet.setup("bot@project.iam.gserviceaccount.com")
        self.assertEqual(spreadsheet.sheets[QUEUE_TAB].rows[0], TAB_HEADERS[QUEUE_TAB])
        self.assertEqual(spreadsheet.sheets["My notes"].rows, [["keep"]])


class SnoozeViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_back_closes_the_picker_without_snoozing(self):
        from reachouts.views import SnoozeView

        view = SnoozeView("NDG-20261031-0000000A", owner_id=LEAD_ID)
        back = next(item for item in view.children if isinstance(item, discord.ui.Button))
        self.assertEqual(back.label, "Back")
        edit = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(id=LEAD_ID), response=SimpleNamespace(edit_message=edit))
        self.assertTrue(await view.interaction_check(interaction))
        await back.callback(interaction)
        edit.assert_awaited_once_with(content="Snooze cancelled. Nothing changed.", view=None)

    async def test_only_the_lead_who_opened_it_can_use_it(self):
        from reachouts.views import SnoozeView

        send = AsyncMock()
        stranger = SimpleNamespace(user=SimpleNamespace(id=AGENT_ID), response=SimpleNamespace(send_message=send))
        self.assertFalse(await SnoozeView("NDG-20261031-0000000A", owner_id=LEAD_ID).interaction_check(stranger))
        send.assert_awaited_once()


class ChannelCategoryTests(unittest.TestCase):
    """New agent channels spill into the overflow category once the main one has 50 channels."""

    class Category:
        def __init__(self, cid, count):
            self.id = cid
            self.name = f"cat{cid}"
            self.channels = [object()] * count
            self.overwrites = {}

    def service(self, main_count, overflow=None, overflow_id=0):
        cats = {1: self.Category(1, main_count)}
        if overflow is not None:
            cats[overflow_id] = self.Category(overflow_id, overflow)
        guild = SimpleNamespace(get_channel=lambda cid: cats.get(cid))
        svc = ReachoutService.__new__(ReachoutService)
        svc.settings = Settings(sheet_id="x", poll_seconds=60, batch=10, category_id=1, test_mode=False,
                                test_allowed=frozenset(), error_channel_id=0, overflow_category_id=overflow_id)
        return svc, guild

    def test_uses_main_category_while_room(self):
        with patch("reachouts.service.discord.CategoryChannel", self.Category):
            svc, guild = self.service(49)
            self.assertEqual(asyncio.run(svc.channel_category(guild)).id, 1)

    def test_spills_to_overflow_when_main_is_full(self):
        with patch("reachouts.service.discord.CategoryChannel", self.Category):
            svc, guild = self.service(50, overflow=3, overflow_id=2)
            self.assertEqual(asyncio.run(svc.channel_category(guild)).id, 2)

    def test_blocks_clearly_when_no_overflow_is_configured(self):
        with patch("reachouts.service.discord.CategoryChannel", self.Category):
            svc, guild = self.service(50)
            with self.assertRaisesRegex(Exception, "REACHOUT_CHANNEL_OVERFLOW_CATEGORY_ID"):
                asyncio.run(svc.channel_category(guild))

    def test_blocks_when_overflow_is_also_full(self):
        with patch("reachouts.service.discord.CategoryChannel", self.Category):
            svc, guild = self.service(50, overflow=50, overflow_id=2)
            with self.assertRaisesRegex(Exception, "also full"):
                asyncio.run(svc.channel_category(guild))

    def test_team_lead_gets_their_team_category_created_once(self):
        with patch("reachouts.service.discord.CategoryChannel", self.Category):
            svc, guild = self.service(50)
            svc.team_categories = {42: "reachouts-example"}
            made = self.Category(9, 0)
            made.name = "reachouts-example"
            guild.categories = []
            guild.create_category = AsyncMock(return_value=made)
            self.assertIs(asyncio.run(svc.channel_category(guild, 42)), made)
            guild.create_category.assert_awaited_once()
            self.assertEqual(guild.create_category.await_args.args[0], "reachouts-example")
            guild.categories = [made]
            guild.create_category.reset_mock()
            self.assertIs(asyncio.run(svc.channel_category(guild, 42)), made)
            guild.create_category.assert_not_awaited()

    def test_other_leads_still_use_the_shared_category(self):
        with patch("reachouts.service.discord.CategoryChannel", self.Category):
            svc, guild = self.service(10)
            svc.team_categories = {42: "reachouts-example"}
            self.assertEqual(asyncio.run(svc.channel_category(guild, 7)).id, 1)


if __name__ == "__main__":
    unittest.main()
