import os
import tempfile
import unittest
from unittest.mock import patch

from evoagent.config import Settings, load_dotenv


class DotenvTests(unittest.TestCase):
    def test_canonical_ci_authority_has_distinct_environment_settings(self):
        with patch.dict(os.environ, {
            "EVOAGENT_GITHUB_APP_ID": "1",
            "EVOAGENT_GITHUB_APP_SLUG": "evoagent",
            "EVOAGENT_GITHUB_CI_APP_ID": "42",
            "EVOAGENT_GITHUB_CI_APP_SLUG": "canonical-ci",
        }, clear=True):
            settings = Settings.from_env()

        self.assertEqual("1", settings.github_app_id)
        self.assertEqual("evoagent", settings.github_app_slug)
        self.assertEqual("42", settings.github_ci_app_id)
        self.assertEqual("canonical-ci", settings.github_ci_app_slug)

    def test_loads_valid_assignments_and_quoted_values(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("# comment\n")
            handle.write("export EVOAGENT_LLM_PROVIDER=deepseek\n")
            handle.write('EVOAGENT_DEEPSEEK_API_KEY="test-key"\n')
            handle.write("invalid line\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {}, clear=True):
                load_dotenv([path])
                self.assertEqual("deepseek", os.environ["EVOAGENT_LLM_PROVIDER"])
                self.assertEqual("test-key", os.environ["EVOAGENT_DEEPSEEK_API_KEY"])
        finally:
            os.unlink(path)

    def test_process_environment_has_priority(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("EVOAGENT_LLM_PROVIDER=deepseek\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {"EVOAGENT_LLM_PROVIDER": "custom"}, clear=True):
                load_dotenv([path])
                self.assertEqual("custom", os.environ["EVOAGENT_LLM_PROVIDER"])
        finally:
            os.unlink(path)
