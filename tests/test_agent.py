"""Tests for cld.agent.launch_agent input validation.

Task composition (merging task file + inline prompt) was moved into the
container entrypoint so the resulting file lands on the agent's VCS change,
not on the host's working copy. Only the host-side validation is unit-tested
here; composition is exercised by integration tests.
"""

from unittest.mock import patch

import pytest

from cld.agent import launch_agent
from cld.config import Config


class TestLaunchAgentValidation:
    def test_neither_task_file_nor_prompt_exits(self):
        with patch("cld.agent.require_docker"):
            with pytest.raises(SystemExit):
                launch_agent(Config())
