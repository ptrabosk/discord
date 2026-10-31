# Agent reachouts — acceptance-test checklist

**Automated** = covered by `tests/test_reachouts.py`, which uses an in-memory sheet and fake Discord
objects. **Manual** = run it in the test Discord server against a test spreadsheet, with test mode
on.

## Setup for manual checks

1. Test spreadsheet created with `python main.py reachout-sheet-setup`. `REACHOUT_SHEET_ID` points
   at it, and `REACHOUT_CHANNEL_CATEGORY_ID` is a private test category.
2. `REACHOUT_TEST_MODE=true`, with `REACHOUT_TEST_ALLOWED_USER_IDS` = test agent A, test lead L,
   and test lead L2. None of them are server administrators. A is verified and Active in the roster.
3. `Reachout Agents`: `TEST-A` → A's email, the roster's `Team Lead Email` = L's email, `active` TRUE. Wait for
   `directory_status` = `OK`.

## Checklist

| # | Setup | Action | Expected | Automated |
|---|---|---|---|---|
| 1 | Template created | Inspect the eight sample rows | All unticked for `example.agent@example.invalid`. Ticking one and running `/reachout_poll` gives `ERROR` "…can never be sent". | `TemplateSetupTests`, `test_draft_rows_never_get_an_id_or_send` |
| 2 | — | Add a full Agent behaviors row for `TEST-A`, tick `send`, wait two automatic polls | No ID, nothing posted | `test_ticked_rows_wait_for_reachout_poll` |
| 3 | Row from 2 | Run `/reachout_poll` | One post in `reachout-<A's first name>` mentioning A, with a thread; the card shows only title, message and footer. `processing_status` `SENT`. Reachout History gets a row, due 7 days later. | `test_ready_row_sends_once…`, `HistoryTests` |
| 4 | Sent row | Sort the sheet, insert a row above it, and edit the title | No new post. Same `reachout_id`, still `SENT`. | yes |
| 5 | — | Ticked row missing `title`; ticked row with `reachout_type` "Coaching"; `/reachout_poll` | `ERROR` with an exact message; nothing posted | `test_invalid_duplicate_and_unknown_rows_never_post`, `test_actionable_errors` |
| 6 | Sent row | Copy the whole row, including `reachout_id`, to a new row | Both show `ERROR` "…is on 2 rows", nothing posted. Delete the copy and the original returns to `SENT`. | yes |
| 7 | 15 ticked rows | Run `/reachout_poll` once | All 15 are sent | `test_reachout_poll_sends_every_ticked_row_beyond_the_batch_size` |
| 8 | Sent rows | Delete them while `/reachout_poll` runs | Status never lands on another row; History unaffected | `test_deleting_rows_mid_poll_never_writes_to_the_wrong_row` |
| 9 | Row queued but not yet sent (stop the bot first) | Edit `message_body`, start the bot | `ERROR` (held: edited after release), nothing posted | `test_edits_after_release_hold_the_send` |
| 10 | Delivered reachout | As A, try typing in the channel and in the thread | Discord does not allow it | Manual |
| 11 | Delivered reachout | As L2, or any other non-admin, click **Acknowledge & respond** | Cannot see the channel, or gets "Only the agent…" | `test_wrong_users…` |
| 12 | Delivered Agent behaviors reachout | As A, open the form and close it without submitting | `ack_status` stays `PENDING` | `test_opening_the_form_is_not_acknowledgment` |
| 13 | Same | As A, submit with text (and optionally up to 4 images) | "Acknowledgment saved". Thread post shows A, the time, and "Acknowledgment only". `ack_status` `ACKNOWLEDGED`. L gets a DM with Reply / Reply — response required / Acknowledge / Open thread. A new row in `Reachout Responses`. | `test_submission_reaches_thread_lead_and_sheets_once` |
| 14 | Opt outs | Submit an empty form | The form requires text, and the server rejects empty text. Submitting with text sets `written_response_status` `RECEIVED`. | `test_acknowledgment_always_requires_written_text` |
| 15 | Flagged messages | Same as 13 | Same as 13 | Manual (same code path as 13; acknowledgment rule covered by `test_three_mandatory_categories…`) |
| 16 | Weekly performance | Send it | Card has no buttons, no footer and no thread; no `due_at`, no reminder | `test_weekly_performance_has_no_buttons_footer_or_thread` |
| 17 | Weekly performance | A clicks **Respond**, submits; then **Add response**, submits | Two separate responses and two lead DMs | yes |
| 18 | Lead DM from 17 | L clicks **Acknowledge** on the first DM | First response `ACKNOWLEDGED`, second still `UNREVIEWED`. "Response reviewed … does not resolve" in the thread. `resolution_status` still `OPEN`. | `test_lead_ack_and_reply_apply_to_one_response…` |
| 19 | Lead DM | L clicks **Reply**, writes text | Reply in the thread under L's name; the response becomes `REPLIED`; no new obligation | yes |
| 20 | Lead DM | L clicks **Reply — response required** | Thread post with "Your written response is due …" and a Respond button. New `FOLLOW_UP_RESPONSE` obligation `PENDING`. A's next written submission satisfies it. | `test_requesting_a_response_creates_one_new_obligation` |
| 21 | — | Deadline | Required items are due 7 calendar days after delivery | `test_ready_row_sends_once…` |
| 22 | Agent behaviors, unacknowledged, past due (set `due_at_override` a few minutes ahead) | Wait one poll | **One** standard reminder in the thread mentioning only A. L gets no DM. | `test_overdue_items_get_one_standard_reminder_in_the_thread_and_no_lead_dm` |
| 23 | Overdue DM sent to L before this change | L clicks **Send reminder** twice | One reminder in the thread mentioning only A. Second click: "Already handled". | `test_reminder_rechecks_state…` |
| 24 | Same | A responds, then L clicks **Custom reminder** | "No longer needed"; nothing posted | `test_agent_response_makes_reminder_actions_stale` |
| 25 | Same | L clicks **Snooze** → 1 hour | `due_at` unchanged; one automatic reminder in the thread an hour later | `test_snooze_defers…` |
| 26 | Restart | `sudo systemctl restart offsight-bot.service`, then click old buttons | All buttons still work. Pending deliveries and due nudges resume. | `RestartTests`, manual |
| 27 | Crash after post | (Automated only) | No duplicate post; the record adopts the existing message | `test_a_crash_after_discord_accepted_the_post_does_not_repost` |
| 28 | L has DMs closed, no fallback | A submits | `lead_notify_status` `FAILED`, error alert, response still saved and posted | `test_closed_dms_without_a_safe_fallback…` |
| 29 | L DMs closed, `fallback_channel_id` visible only to L | A submits | DM posted in the fallback channel mentioning L; `FALLBACK_SENT` | `test_closed_dms_use_a_restricted_fallback_channel` |
| 30 | Reassign | Change A's `Team Lead Email` in Discord Access from L to L2 | L loses channel access, L2 gains it. L's old buttons say "not currently assigned". | `test_lead_reassignment…` |
| 31 | Reachout cancelled before the checkbox queue | Open it | Button disabled, no reminders, responses kept; History `status` `CANCELLED` | `test_reachouts_cancelled_before_the_checkbox_queue…`, `test_cancelled_reachouts_get_no_reminders` |
| 32 | Reachout resolved before the checkbox queue | A clicks Add response | Allowed; no new required responses | `test_reachouts_resolved_before_the_checkbox_queue…` |
| 33 | Sent | Delete the queue row, wait, then add a row with only its `reachout_id` | Workflow continues, the `QUEUE_ROW_MISSING` event is logged, and status is restored | `test_row_deletion_keeps_workflow_state` |
| 34 | Test mode with B not allowlisted | Ticked row for agent B, `/reachout_poll` | `ERROR` "Test mode…", nothing posted | `test_test_mode_blocks…` |
| 35 | Channel shared with a third member | Ticked row, `/reachout_poll` | `ERROR` "visible to 1 other member(s)", nothing posted | `test_unsafe_channel_audience_blocks_delivery` |
| 36 | Discord rejects a post (e.g. revoke the bot's Send in the category) | Ticked row, `/reachout_poll` | `SENDING` with "Retrying …" in `status_detail`. Restore the permission: delivered once. | `test_a_definite_discord_refusal…`, manual |
| 37 | Message body containing `@everyone` and a role mention | Ticked row, `/reachout_poll` | Delivered as text; nobody but A is pinged | Covered by `allowed_mentions` assertions; manual |
| 38 | Existing features | `/workflow` submission; Verify Work Email; `/access_status` | Unchanged | Existing `tests/test_forms.py`, `tests/test_onboarding.py`; manual |

Run the automated suite in a development environment, never in the production `.venv`:

```bash
python3 -m venv /tmp/opsbot-dev && /tmp/opsbot-dev/bin/pip install -r requirements-dev.txt
/tmp/opsbot-dev/bin/python -m pytest -q
```
