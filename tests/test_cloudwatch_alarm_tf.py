"""Pipeline CloudWatch alarms have a single writer.

scripts/create_cloudwatch_alarm.sh creates tick-pipeline-stale and
tick-disk-low with the live Host dimension and SNS actions. Terraform must
not declare those alarm names: put-metric-alarm and aws_cloudwatch_metric_alarm
both upsert by name, so whichever runs last overwrites Host and can drop paging.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "infra" / "main.tf").read_text()
HEALTH = (ROOT / "scripts" / "pipeline_health.sh").read_text()
CREATE = (ROOT / "scripts" / "create_cloudwatch_alarm.sh").read_text()


def _hcl_without_comments(text: str) -> str:
    """Drop full-line and trailing comments so docs may name the alarm."""
    lines = []
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        lines.append(code)
    return "\n".join(lines)


class TestCloudwatchAlarmSingleOwner(unittest.TestCase):
    def test_health_script_emits_host_dimension(self) -> None:
        self.assertIn('--dimensions "Host=$(hostname)"', HEALTH)

    def test_create_script_owns_pipeline_alarm(self) -> None:
        self.assertIn('ALARM_NAME="${ALARM_NAME:-tick-pipeline-stale}"', CREATE)
        self.assertIn('DISK_ALARM_NAME="${DISK_ALARM_NAME:-tick-disk-low}"', CREATE)
        self.assertIn("Name=Host,Value=${HOST_DIMENSION}", CREATE)
        self.assertIn('HOST_DIMENSION="${HOST_DIMENSION:-$(hostname)}"', CREATE)
        self.assertIn("--alarm-actions", CREATE)
        self.assertIn("--treat-missing-data breaching", CREATE)

    def test_terraform_does_not_declare_the_same_alarm_names(self) -> None:
        """A second resource with these names silently replaces the script's alarm."""
        config = _hcl_without_comments(TF)
        self.assertIsNone(
            re.search(r'resource\s+"aws_cloudwatch_metric_alarm"', config),
            "infra/main.tf must not declare CloudWatch alarms; "
            "scripts/create_cloudwatch_alarm.sh is the sole writer",
        )
        self.assertNotIn("alarm_name", config)
        self.assertNotIn("tick-pipeline-stale", config)
        self.assertNotIn("tick-disk-low", config)

    def test_terraform_forgets_imported_alarm_without_destroying_it(self) -> None:
        """Apply must drop the old address from state and leave the live alarm."""
        self.assertRegex(
            TF,
            re.compile(
                r"removed\s*\{[^}]*from\s*=\s*aws_cloudwatch_metric_alarm\.tick_pipeline_stale"
                r".*?destroy\s*=\s*false",
                re.S,
            ),
        )


if __name__ == "__main__":
    unittest.main()
