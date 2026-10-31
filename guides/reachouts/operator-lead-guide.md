# Agent reachouts — operator and team-lead guide

## Reachout types

| Type (`reachout_type`) | Acknowledgment | Written response | Agent button | Overdue reminder |
|---|---|---|---|---|
| Weekly performance | None | None | None — the card is informational and has no thread | None |
| Agent behaviors | Required | Required | **Acknowledge & respond**, later **Add response** | Yes |
| Opt outs | Required | Required | As above | Yes |
| Flagged messages | Required | Required | As above | Yes |
| Concerning | Required | Required | As above | Yes |

The card shows only the title, the message (and any `reference_urls`), and the line "Respond with the button below, not in the thread. Typing is disabled." Weekly
performance cards have no footer.

## Sending reachouts (operators)

`Reachout Queue` only holds what still has to be sent:

1. Add a row and fill `agent_email`, `reachout_type`, `title`, and `message_body`. Optionally fill
   `reference_urls` (shown to the agent as Link 1, Link 2, …) and `due_at_override`
   (`YYYY-MM-DD HH:MM` in the Config timezone).
2. Finish editing. What is in the row when it is sent is exactly what the agent sees.
3. Tick `send`. Tick as many rows as you want to send together.
4. Run `/reachout_poll` in Discord. Ticked rows are sent only then; the bot never reads the sheet
   on its own. Each row's `processing_status` becomes `SENT`.
5. Delete `SENT` rows whenever you like. Everything about them, including every reply, is in
   **Reachout History**.

The bot never rewrites your text and never adds assessments of its own.

| `processing_status` | Meaning | What to do |
|---|---|---|
| (blank) | Not sent yet | Tick `send` and run `/reachout_poll`. |
| `WAITING` | Has an ID but `send` was unticked | Tick it and run `/reachout_poll`. |
| `SENDING` | Being delivered; retried automatically | Wait. If `status_detail` says Retrying for over an hour, tell an admin. |
| `SENT` | Delivered | Delete the row whenever you like. |
| `ERROR` | Not sent. Your input, the agent, the channel, or a copied row is the problem | Read `status_detail`, fix it, run `/reachout_poll` again. |

To correct something already sent, send a new reachout that refers to it. Sent reachouts cannot be
cancelled or resolved from the sheet; to withdraw one, delete its Discord message by hand.

## Following up (operators and leads)

Use **Reachout History**: one row per sent reachout with its title, message, status, the agent's
replies and the team lead's reply. `needs_tl_reply` is `TRUE` while the agent's latest reply has not
been replied to or acknowledged by the lead. Filter on `status` = `OVERDUE` or `AWAITING_TL` for
what needs attention.

### Reassigning a lead

Change the agent's `Team Lead Email` in `Discord Access` (the new lead must have completed Verify Work Email). Then run `/reachout_poll`. On that poll:

- The new lead gets access to the agent's channel and the old lead's access is removed.
- The old lead's buttons stop working ("You are not the currently assigned team lead").
- Unreviewed agent responses are re-sent to the new lead.

Historical records keep the original lead as author or reviewer.

## What the agent sees

- A post in their private channel `reachout-<first name>`, mentioning them, containing:
  - the type and title, and your content and links;
  - whether acknowledgment and written text are required;
  - the due date (shown in their local time by Discord);
  - the button.
- A thread attached to that post. Agents cannot type in the channel or the thread. They can only
  use buttons.
- **Acknowledge & respond** opens a form stating: *"Submitting this form records that you received
  and reviewed this reachout. It does not necessarily mean you agree with its contents."*
  - The text box is optional, or required when a written explanation was requested.
  - Opening the form records nothing. Only a confirmed submission counts, and the agent sees
    "Acknowledgment saved (RSP-…)". If saving fails, they are told it was NOT saved.
- Each submission is posted in the thread by the bot, labelled with the agent's name, the time, and
  the text, or "Acknowledgment only — no written response."
- **Add response** sends further responses at any time. Each is a separate record and notifies the
  lead.

## Team-lead workflow

### When an agent responds

You get a DM showing:
- the agent;
- the reachout type and title;
- an excerpt of the original;
- their response, with the time;
- a thread link.

Buttons:

- **Reply** opens a form. Your reply is posted in the agent's thread under your name, and the agent
  is mentioned. It marks *that* response as reviewed (`REPLIED`). It does not resolve the reachout
  and does not start a deadline.
- **Reply — response required** works the same way, and also asks the agent for a written response
  due seven days after it is posted. It replaces any other open request on that reachout.
  This is the only way a Weekly performance reachout gets a deadline.
- **Acknowledge** marks *that one* response reviewed and posts "Response reviewed … It does not
  resolve the reachout." in the thread. Later responses still need their own review.
- **Open thread** jumps to the thread.

Messages you type directly in the channel or thread are **not recorded** by the bot and do not
count as a reply, review, or reminder. Use the buttons for anything that should be tracked.

### When a required item is overdue

Seven calendar days after delivery (or at `due_at_override`), the bot posts the standard reminder
(`reminder_template`) in the reachout thread, mentioning only that agent. Team leads are not
DMed. It happens once, unless an admin sets `nudge_repeat_business_days` above 0. Nothing is posted
if the agent has already responded or the reachout was cancelled or resolved.

Overdue DMs sent to leads before this change keep working buttons (**Send reminder**, **Custom
reminder**, **Snooze**).

## Deadlines (seven days)

- A required item is due seven calendar days after delivery, at the same time of day. Weekends and
  holidays count. **Reply — response required** requests are due seven days after they are posted.

`due_at_override` sets an exact time instead. If it is not after the actual delivery time, the
default deadline is used and `status_detail` says so.
