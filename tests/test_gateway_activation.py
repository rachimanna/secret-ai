import os
import unittest
from unittest.mock import patch

import bot


class GatewayActivationTest(unittest.TestCase):
    def test_retained_oxion_key_alone_does_not_enable_paused_trial(self):
        with patch.dict(os.environ,{"OXIONAI_API_KEY":"test-key"},clear=True):
            self.assertFalse(bot.gateway_enabled("oxionai"))

    def test_switch_off_wins_over_a_configured_key(self):
        with patch.dict(os.environ,{"OXIONAI_API_KEY":"test-key","OXIONAI_ENABLED":"0"},clear=True):
            self.assertFalse(bot.gateway_enabled("oxionai"))

    def test_trial_requires_explicit_opt_in_and_nonempty_key(self):
        with patch.dict(os.environ,{"OXIONAI_API_KEY":"test-key","OXIONAI_ENABLED":"1"},clear=True):
            self.assertTrue(bot.gateway_enabled("oxionai"))
        with patch.dict(os.environ,{"OXIONAI_API_KEY":"  ","OXIONAI_ENABLED":"1"},clear=True):
            self.assertFalse(bot.gateway_enabled("oxionai"))

    def test_legacy_gateway_activation_is_unchanged(self):
        with patch.dict(os.environ,{"REFORMBOSS_API_KEY":"configured","ANYMODEL_API_KEY":"configured"},clear=True):
            self.assertTrue(bot.gateway_enabled("reformboss"))
            self.assertTrue(bot.gateway_enabled("anymodel"))
            self.assertFalse(bot.gateway_enabled("oxionai"))
            self.assertFalse(bot.gateway_enabled("nvidia"))


if __name__ == "__main__":
    unittest.main()
