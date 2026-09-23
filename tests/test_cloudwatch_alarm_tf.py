"""Terraform alarm must watch the same Host dimension pipeline_health emits."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "infra" / "main.tf").read_text()
HEALTH = (ROOT / "scripts" / "pipeline_health.sh").read_text()
CREATE = (ROOT / "scripts" / "create_cloudwatch_alarm.sh").read_text()


class TestCloudwatchAlarmTf(unittest.TestCase):
    def test_health_script_emits_host_dimension(self) -> None:
        self.assertIn('--dimensions "Host=$(hostname)"', HEALTH)

    def test_create_script_filters_host_dimension(self) -> None:
        self.assertIn("Name=Host,Value=${HOST_DIMENSION}", CREATE)

    def test_terraform_alarm_filters_host_dimension(self) -> None:
        """A dimension-less alarm never sees Host-tagged PipelineHealthy points."""
        self.assertIn("alarm_host_dimension", TF)
        self.assertRegex(
            TF,
            re.compile(r'dimensions\s*=\s*\{[^}]*Host\s*=\s*var\.alarm_host_dimension', re.S),
        )
        self.assertIn('default     = "ip-172-31-10-149"', TF)


if __name__ == "__main__":
    unittest.main()
