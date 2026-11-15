"""Reachout Discord UI: restart-safe buttons, forms, and message rendering."""
from __future__ import annotations

import json
import logging

import discord

from bot.permissions import has_workflow_access, is_admin
from reachouts.domain import (
    ACKNOWLEDGMENT,
    MAX_REMINDER,
    MAX_RESPONSE,
    REACHOUT_TYPES,
    WEEKLY,
    Payload,
    discord_time,
    local_text,
)
from reachouts.repository import StaleForm, WorkflowError

log = logging.getLogger(__name__)

RO = r"RO-\d{8}-[0-9A-F]{8}"
RSP = r"RSP-\d{8}-[0-9A-F]{8}"
NDG = r"NDG-\d{8}-[0-9A-F]{8}"
COLORS = {
    WEEKLY: discord.Color.blurple(),
    "agent_behaviors": discord.Color.orange(),
    "opt_outs": discord.Color.gold(),
    "flagged_messages": discord.Color.red(),
    "concerning": discord.Color.dark_red(),
}
NO_MENTIONS = discord.AllowedMentions.none()
SNOOZE_OPTIONS = {"1h": "1 hour", "4h": "4 hours", "1bd": "1 business day", "2bd": "2 business days"}


def mention_only(user_id: int) -> discord.AllowedMentions:
    return discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=user_id)], replied_user=False)


def jump_url(guild_id: int, channel_id: int | None, message_id: int | None = None) -> str | None:
    if not channel_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}" + (f"/{message_id}" if message_id else "")


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def requirement(obligation: dict | None) -> str:
    if not obligation:
        return "response"
    if obligation["obligation_type"] == ACKNOWLEDGMENT:
        return "acknowledgment and written response" if obligation["text_required"] else "acknowledgment"
    return "written response"


def _when(value, tz) -> str:
    return f"{discord_time(value)} ({local_text(value, tz)})" if value else "—"


# --- message rendering ---------------------------------------------------------------------

FOOTER = "Respond with the button below, not in the thread. Typing is disabled."

def reachout_embed(rec: dict, payload: Payload, tz, pending: dict | None, *, due_preview=None) -> discord.Embed:
    embed = discord.Embed(
        title=clip(payload.title, 256),
        description=payload.message_body,
        color=COLORS.get(payload.reachout_type, discord.Color.blurple()),
    )
    # Links show as "Link 1", "Link 2", …; packed into as few fields as Discord's 1024-character limit allows.
    value = ""
    for number, url in enumerate(payload.reference_urls, 1):
        line = f"[Link {number}]({url})"
        if value and len(value) + 1 + len(line) > 1024:
            embed.add_field(name="References" if not embed.fields else "\u200b", value=value, inline=False)
            value = ""
        value = f"{value}\n{line}" if value else line
    if value:
        embed.add_field(name="References" if not embed.fields else "\u200b", value=value, inline=False)
    if payload.reachout_type != WEEKLY:
        embed.set_footer(text=FOOTER)
    return embed


def reachout_view(rec: dict, pending: dict | None, guild_id: int, has_agent_responses: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if rec.get("reachout_type") == WEEKLY:
        return view  # Weekly performance is informational: no buttons.
    closed = rec.get("workflow_status") == "CANCELLED"
    if closed:
        label = "Cancelled"
    elif pending and pending["obligation_type"] == ACKNOWLEDGMENT:
        label = "Acknowledge & respond"
    elif pending:
        label = "Respond to follow-up"
    elif rec.get("reachout_type") == WEEKLY and not has_agent_responses:
        label = "Respond"
    else:
        label = "Add response"
    view.add_item(AgentRespondButton(rec["reachout_id"], label=label, disabled=closed))
    url = jump_url(guild_id, rec.get("thread_id"))
    if url:
        view.add_item(discord.ui.Button(label="Open thread", style=discord.ButtonStyle.link, url=url))
    return view


def respond_view(reachout_id: str, label="Respond") -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(AgentRespondButton(reachout_id, label=label))
    return view


def agent_post_embed(response: dict, tz) -> discord.Embed:
    titles = {
        "AGENT_ACKNOWLEDGMENT": "Acknowledgment",
        "AGENT_ACKNOWLEDGMENT_WITH_RESPONSE": "Acknowledgment and response",
        "AGENT_RESPONSE": "Response",
    }
    embed = discord.Embed(
        title=titles[response["response_type"]],
        description=response["text"] or "**Acknowledgment only — no written response.**",
        color=discord.Color.green(),
    )
    embed.set_author(name=f"{response['actor_name']} — agent")
    embed.add_field(name="Submitted", value=_when(response["submitted_at"], tz), inline=False)
    images = len(json.loads(response.get("attachments") or "[]"))
    if images:
        embed.add_field(name="Images", value=f"{images} attached below", inline=False)
    embed.set_footer(text=f"Posted by the bot for {response['actor_name']} from their form submission · {response['response_id']}")
    return embed


def lead_post_embed(reply: dict, tz, due) -> discord.Embed:
    required = reply["response_type"] == "LEAD_REPLY_RESPONSE_REQUIRED"
    embed = discord.Embed(
        title="Team lead reply — response requested" if required else "Team lead reply",
        description=reply["text"],
        color=discord.Color.blue(),
    )
    embed.set_author(name=f"{reply['actor_name']} — team lead")
    embed.add_field(name="Sent", value=_when(reply["submitted_at"], tz), inline=False)
    if required:
        embed.add_field(name="Your written response is due", value=_when(due, tz), inline=False)
    embed.set_footer(text=f"Posted by the bot for {reply['actor_name']} from their reply form · {reply['response_id']}")
    return embed


def lead_ack_embed(response: dict, lead_name: str, tz) -> discord.Embed:
    embed = discord.Embed(
        title="Response reviewed",
        description=(
            f"{lead_name} (team lead) acknowledged the agent response submitted "
            f"{_when(response['submitted_at'], tz)}.\nThis records review of that response. "
            "It does not resolve the reachout."
        ),
        color=discord.Color.dark_green(),
    )
    embed.set_footer(text=f"Lead acknowledgment · ack-{response['response_id']}")
    return embed


def lead_dm_embed(response: dict, payload: Payload, agent: dict, tz, guild_id: int, rec: dict) -> discord.Embed:
    embed = discord.Embed(
        title=clip(f"Agent response: {REACHOUT_TYPES[payload.reachout_type]}: {payload.title}", 256),
        description=response["text"] or "**Acknowledgment only — no written response.**",
        color=discord.Color.green(),
    )
    embed.add_field(name="Agent", value=f"{agent['agent_name']} (<@{response['actor_discord_user_id']}>)", inline=True)
    embed.add_field(name="Submitted", value=_when(response["submitted_at"], tz), inline=True)
    embed.add_field(name="Kind", value=response["response_type"].replace("_", " ").title(), inline=True)
    embed.add_field(name="Original reachout", value=clip(payload.message_body, 1000), inline=False)
    images = len(json.loads(response.get("attachments") or "[]"))
    if images:
        embed.add_field(name="Images", value=f"{images} attached; see the thread", inline=False)
    url = jump_url(guild_id, rec.get("thread_id"))
    if url:
        embed.add_field(name="Thread", value=url, inline=False)
    embed.add_field(name="Review", value=review_line(response, tz), inline=False)
    embed.set_footer(text=f"Acknowledge marks this one response reviewed; it does not resolve the reachout · {response['response_id']}")
    return embed


def review_line(response: dict, tz) -> str:
    if response["review_status"] == "ACKNOWLEDGED":
        return f"Acknowledged by <@{response['reviewed_by_discord_user_id']}> {_when(response['reviewed_at'], tz)}."
    if response["review_status"] == "REPLIED":
        return f"Replied by <@{response['reviewed_by_discord_user_id']}> {_when(response['reviewed_at'], tz)}."
    return "Not reviewed yet."


def lead_dm_view(response: dict, thread_url: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    rid = response["response_id"]
    view.add_item(LeadReplyButton(rid, requires_response=False))
    view.add_item(LeadReplyButton(rid, requires_response=True))
    view.add_item(LeadAckButton(rid, disabled=response["review_status"] == "ACKNOWLEDGED"))
    if thread_url:
        view.add_item(discord.ui.Button(label="Open thread", style=discord.ButtonStyle.link, url=thread_url))
    return view


def nudge_embed(nudge: dict, obligation: dict, payload: Payload, agent: dict, rec: dict, tz, guild_id: int) -> discord.Embed:
    embed = discord.Embed(
        title=clip(f"Overdue: {REACHOUT_TYPES[payload.reachout_type]}: {payload.title}", 256),
        description=(
            f"{agent['agent_name']} has not submitted the required {requirement(obligation)}.\n"
            "No reminder has been sent to the agent. Choose an action below."
        ),
        color=discord.Color.dark_orange(),
    )
    embed.add_field(name="Agent", value=f"{agent['agent_name']} (<@{obligation['required_agent_discord_user_id']}>)", inline=False)
    embed.add_field(name="Originally delivered", value=_when(rec["delivered_at"], tz), inline=True)
    embed.add_field(name="Due", value=_when(obligation["due_at"], tz), inline=True)
    embed.add_field(name="Missing", value=requirement(obligation).capitalize(), inline=True)
    url = jump_url(guild_id, rec.get("thread_id"))
    if url:
        embed.add_field(name="Thread", value=url, inline=False)
    state = {
        "STALE": "No longer needed — the agent responded or the request was closed.",
        "HANDLED": f"Handled: {nudge.get('handled_action')} by <@{nudge.get('handled_by_discord_user_id')}> "
                   f"{_when(nudge.get('handled_at'), tz)}.",
    }.get(nudge["status"])
    if state:
        embed.add_field(name="Status", value=state, inline=False)
    embed.set_footer(text=f"Overdue nudge {nudge['seq']} · {nudge['nudge_id']}")
    return embed


def nudge_view(nudge: dict, thread_url: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    done = nudge["status"] in ("HANDLED", "STALE")
    for action in ("rem", "crem", "snz"):
        view.add_item(NudgeButton(action, nudge["nudge_id"], disabled=done))
    if thread_url:
        view.add_item(discord.ui.Button(label="Open thread", style=discord.ButtonStyle.link, url=thread_url))
    return view


def reminder_embed(event: dict, lead_name: str, text: str, tz) -> discord.Embed:
    if event["actor_role"] == "BOT":
        embed = discord.Embed(title="Reminder", description=text, color=discord.Color.orange())
        embed.add_field(name="Sent", value=_when(event["occurred_at"], tz), inline=False)
        embed.set_footer(text=f"Automatic overdue reminder · {event['event_id']}")
        return embed
    embed = discord.Embed(title="Reminder from your team lead", description=text, color=discord.Color.orange())
    embed.set_author(name=f"{lead_name} — team lead")
    embed.add_field(name="Sent", value=_when(event["occurred_at"], tz), inline=False)
    embed.set_footer(text=f"Sent at the team lead's request · {event['event_id']}")
    return embed


def notice_embed(rec: dict, status: str, tz) -> discord.Embed:
    if status == "CANCELLED":
        text = (f"Operations cancelled this reachout {_when(rec.get('cancelled_at'), tz)}. No response is needed. "
                "Earlier messages and responses are kept.")
    else:
        text = f"Operations marked this reachout resolved {_when(rec.get('resolved_at'), tz)}."
    embed = discord.Embed(title=status.title(), description=text, color=discord.Color.light_grey())
    embed.set_footer(text=f"Reachout {status.lower()} · {status.lower()}-{rec['reachout_id']}")
    return embed


# --- error helper --------------------------------------------------------------------------

async def _reply(interaction: discord.Interaction, message: str):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def _guarded(interaction: discord.Interaction, kind: str, coro):
    try:
        await coro
    except (PermissionError, WorkflowError, ValueError) as exc:
        await _reply(interaction, str(exc))
    except Exception as exc:
        log.exception("Reachout interaction %s failed", kind)
        await _reply(interaction, "That action could not be completed. Nothing new was saved unless you saw a confirmation.")
        await interaction.client.reachouts.alert(f"interaction_{kind}_failed", f"interaction {interaction.id}", exc)


def _service(interaction):
    return interaction.client.reachouts


# --- agent controls ------------------------------------------------------------------------

class AgentRespondButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"ro:agent:(?P<rid>{RO})"):
    def __init__(self, reachout_id: str, label: str = "Respond", disabled: bool = False):
        style = discord.ButtonStyle.success if label == "Acknowledge & respond" else discord.ButtonStyle.primary
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"ro:agent:{reachout_id}", disabled=disabled))
        self.reachout_id = reachout_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"])

    async def callback(self, interaction: discord.Interaction):
        await _guarded(interaction, "agent_open", self._open(interaction))

    async def _open(self, interaction):
        # Opening the form records nothing; only a saved submission counts.
        form = _service(interaction).agent_form(self.reachout_id, interaction.user.id)
        await interaction.response.send_modal(AgentResponseModal(self.reachout_id, form))


class AgentResponseModal(discord.ui.Modal):
    def __init__(self, reachout_id: str, form: dict):
        super().__init__(title=form["title"], timeout=1800)
        self.reachout_id = reachout_id
        self.obligation_id = form["obligation_id"]
        self.add_item(discord.ui.TextDisplay(form["notice"]))
        self.text = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=form["text_required"], max_length=MAX_RESPONSE,
            placeholder="Type your response" if form["text_required"] else "Optional",
        )
        self.add_item(discord.ui.Label(text=form["label"], description=form["description"], component=self.text))
        self.images = discord.ui.FileUpload(required=False, min_values=0, max_values=4)
        self.add_item(discord.ui.Label(text="Images (optional)", description="Up to 4 images, 8 MB each.",
                                       component=self.images))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = str(self.text.value or "")
        try:
            response = await _service(interaction).submit_agent(interaction, self.reachout_id, self.obligation_id, text,
                                                                list(self.images.values))
        except StaleForm as exc:
            await interaction.followup.send(
                f"{exc} Nothing was saved. Please open the form again.\n\nYour text, so you can copy it:\n>>> {clip(text, 1700)}"
                if text else f"{exc} Nothing was saved. Please open the form again.", ephemeral=True,
            )
            return
        except (PermissionError, WorkflowError, ValueError) as exc:
            await interaction.followup.send(f"Not saved: {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("Could not save reachout submission")
            await interaction.followup.send(
                "Your submission was NOT saved because of a technical problem. Please try again in a few minutes.",
                ephemeral=True,
            )
            await _service(interaction).alert("agent_submission_failed", self.reachout_id, exc)
            return
        kind = "Acknowledgment" if response["acknowledgment"] else "Response"
        await interaction.followup.send(
            f"{kind} saved ({response['response_id']}). It will appear in the reachout thread and your team lead "
            "will be notified.", ephemeral=True,
        )


# --- team-lead controls --------------------------------------------------------------------

class LeadReplyButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"ro:(?P<mode>lr|lrr):(?P<rsp>{RSP})"):
    def __init__(self, response_id: str, requires_response: bool):
        super().__init__(discord.ui.Button(
            label="Reply — response required" if requires_response else "Reply",
            style=discord.ButtonStyle.primary,
            custom_id=f"ro:{'lrr' if requires_response else 'lr'}:{response_id}",
        ))
        self.response_id = response_id
        self.requires_response = requires_response

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rsp"], match["mode"] == "lrr")

    async def callback(self, interaction):
        await _guarded(interaction, "lead_reply_open", self._open(interaction))

    async def _open(self, interaction):
        response = _service(interaction).lead_context(self.response_id, interaction.user.id, self.requires_response)
        await interaction.response.send_modal(LeadReplyModal(self.response_id, self.requires_response, response))


class LeadReplyModal(discord.ui.Modal):
    def __init__(self, response_id: str, requires_response: bool, response: dict):
        super().__init__(title="Reply — response required" if requires_response else "Reply to agent", timeout=1800)
        self.response_id = response_id
        self.requires_response = requires_response
        agent_text = response["text"] or "(Acknowledgment only — no written response.)"
        notice = f"**Agent wrote:**\n>>> {clip(agent_text, 1500)}"
        if requires_response:
            notice += ("\n\nThe agent will be asked for a written response with a new deadline. "
                       "This replaces any other open request on this reachout.")
        self.add_item(discord.ui.TextDisplay(notice))
        self.text = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=True, max_length=MAX_RESPONSE)
        self.add_item(discord.ui.Label(text="Your reply", description="Posted in the agent's thread under your name.",
                                       component=self.text))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            reply = await _service(interaction).submit_lead_reply(
                interaction, self.response_id, self.requires_response, str(self.text.value or ""),
            )
        except (PermissionError, WorkflowError, ValueError) as exc:
            await interaction.followup.send(f"Not saved: {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("Could not save lead reply")
            await interaction.followup.send("Your reply was NOT saved. Please try again in a few minutes.", ephemeral=True)
            await _service(interaction).alert("lead_reply_failed", self.response_id, exc)
            return
        extra = " A written response was requested from the agent." if self.requires_response else ""
        await interaction.followup.send(f"Reply saved ({reply['response_id']}) and queued for the thread.{extra}", ephemeral=True)
        await _service(interaction).refresh_lead_dm(interaction.message, self.response_id)


class LeadAckButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"ro:lack:(?P<rsp>{RSP})"):
    def __init__(self, response_id: str, disabled: bool = False):
        super().__init__(discord.ui.Button(
            label="Acknowledged" if disabled else "Acknowledge", style=discord.ButtonStyle.success,
            custom_id=f"ro:lack:{response_id}", disabled=disabled,
        ))
        self.response_id = response_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rsp"])

    async def callback(self, interaction):
        await _guarded(interaction, "lead_ack", self._ack(interaction))

    async def _ack(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        response, created = await _service(interaction).lead_acknowledge(interaction, self.response_id)
        if created:
            await interaction.followup.send(
                "Marked this response reviewed. The acknowledgment is posted in the thread. "
                "This does not resolve the reachout.", ephemeral=True,
            )
        else:
            await interaction.followup.send("This response was already acknowledged.", ephemeral=True)
        await _service(interaction).refresh_lead_dm(interaction.message, self.response_id)


class NudgeButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"ro:(?P<action>rem|crem|snz):(?P<nudge>{NDG})"):
    LABELS = {"rem": "Send reminder", "crem": "Custom reminder", "snz": "Snooze"}

    def __init__(self, action: str, nudge_id: str, disabled: bool = False):
        super().__init__(discord.ui.Button(
            label=self.LABELS[action],
            style=discord.ButtonStyle.secondary if action == "snz" else discord.ButtonStyle.primary,
            custom_id=f"ro:{action}:{nudge_id}", disabled=disabled,
        ))
        self.action = action
        self.nudge_id = nudge_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["nudge"])

    async def callback(self, interaction):
        await _guarded(interaction, f"nudge_{self.action}", self._run(interaction))

    async def _run(self, interaction):
        service = _service(interaction)
        if self.action == "crem":
            service.nudge_context(self.nudge_id, interaction.user.id)
            await interaction.response.send_modal(CustomReminderModal(self.nudge_id))
        elif self.action == "snz":
            service.nudge_context(self.nudge_id, interaction.user.id)
            await interaction.response.send_message(
                "Snooze the next overdue nudge until when? The due date and overdue status do not change.",
                view=SnoozeView(self.nudge_id, interaction.user.id), ephemeral=True,
            )
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await service.remind(interaction, self.nudge_id, None)
            await interaction.followup.send("Standard reminder queued for the agent's thread.", ephemeral=True)


class CustomReminderModal(discord.ui.Modal, title="Custom reminder"):
    def __init__(self, nudge_id: str):
        super().__init__(timeout=1800)
        self.nudge_id = nudge_id
        self.add_item(discord.ui.TextDisplay("This is posted in the agent's thread and tags only that agent."))
        self.text = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=True, max_length=MAX_REMINDER)
        self.add_item(discord.ui.Label(text="Reminder", component=self.text))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = str(self.text.value or "").strip()
        try:
            await _service(interaction).remind(interaction, self.nudge_id, text)
        except (PermissionError, WorkflowError, ValueError) as exc:
            await interaction.followup.send(f"Not sent: {exc}\n\nYour text:\n>>> {clip(text, 1000)}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("Could not save custom reminder")
            await interaction.followup.send("The reminder was NOT saved. Please try again.", ephemeral=True)
            await _service(interaction).alert("custom_reminder_failed", self.nudge_id, exc)
            return
        await interaction.followup.send("Custom reminder queued for the agent's thread.", ephemeral=True)


class SnoozeView(discord.ui.View):
    def __init__(self, nudge_id: str, owner_id: int):
        super().__init__(timeout=600)
        self.nudge_id = nudge_id
        self.owner_id = owner_id
        select = discord.ui.Select(
            placeholder="Snooze until…",
            options=[discord.SelectOption(label=label, value=key) for key, label in SNOOZE_OPTIONS.items()],
        )
        select.callback = self.choose
        self.add_item(select)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.cancel
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This control belongs to another user.", ephemeral=True)
        return False

    async def cancel(self, interaction: discord.Interaction):
        """Back out of snoozing; the nudge's buttons stay usable."""
        await interaction.response.edit_message(content="Snooze cancelled. Nothing changed.", view=None)

    async def choose(self, interaction: discord.Interaction):
        option = interaction.data["values"][0]

        async def run():
            until = await _service(interaction).snooze(interaction, self.nudge_id, option)
            await interaction.response.edit_message(
                content=f"Snoozed. The next nudge is scheduled for {discord_time(until)}.", view=None,
            )

        await _guarded(interaction, "snooze", run())


# --- registration and admin commands -------------------------------------------------------

def register_reachouts(bot, service):
    bot.add_dynamic_items(AgentRespondButton, LeadReplyButton, LeadAckButton, NudgeButton)

    @bot.tree.command(name="reachout_status", description="Show one reachout's workflow state")
    async def reachout_status(interaction: discord.Interaction, reachout_id: str):
        if not is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.send_message(service.status_text(reachout_id.strip()), ephemeral=True,
                                                allowed_mentions=NO_MENTIONS)

    @bot.tree.command(name="reachout_poll", description="Send the ticked rows in Reachout Queue now")
    async def reachout_poll(interaction: discord.Interaction):
        if not has_workflow_access(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            stats = await service.tick()
        except Exception as exc:
            await interaction.followup.send(f"Poll failed: {type(exc).__name__}: {clip(str(exc), 300)}", ephemeral=True)
            return
        await interaction.followup.send(f"Poll complete: {stats}", ephemeral=True)

    @bot.tree.command(name="reachout_sync", description="Refresh the Reachout Sheets tabs from the database. Sends nothing.")
    async def reachout_sync(interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        stats = await service.sync_sheets()
        await interaction.followup.send(f"Sync complete: {stats}", ephemeral=True)

    @bot.tree.command(name="reachout_retry_failed", description="Retry reachout deliveries that failed permanently")
    async def reachout_retry_failed(interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        count = service.repo.rearm_failed_jobs(discord.utils.utcnow())
        service.kick()
        await interaction.response.send_message(f"Re-queued {count} failed job(s).", ephemeral=True)
