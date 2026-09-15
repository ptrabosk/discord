import unittest
from unittest.mock import AsyncMock, Mock, patch

from bot.guild_authorization import load_authorized_guild_id
from bot.onboarding import CodeModal, EmailModal, VERIFICATION_FAILED_MESSAGE


def make_interaction():
    interaction = Mock()
    interaction.guild_id = 123456789
    interaction.user.id = 42
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


class ModalAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict("os.environ", {"DISCORD_GUILD_ID": "123456789"}):
            load_authorized_guild_id()

    async def test_email_modal_defers_before_returning_validation_error(self):
        modal = EmailModal(Mock())
        modal.email._value = "invalid-email"
        interaction = make_interaction()

        await modal.on_submit(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.edit_original_response.assert_awaited_once_with(
            content=VERIFICATION_FAILED_MESSAGE,
            view=None,
        )

    @patch("bot.onboarding.get_code", return_value=None)
    async def test_code_modal_defers_before_reporting_missing_request(self, _get_code):
        modal = CodeModal(Mock())
        interaction = make_interaction()

        await modal.on_submit(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.edit_original_response.assert_awaited_once_with(
            content="No active verification request. Start again.",
            view=None,
        )


if __name__ == "__main__":
    unittest.main()
