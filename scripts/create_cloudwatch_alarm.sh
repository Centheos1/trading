#!/usr/bin/env bash
# One-shot: create the SNS topic + CloudWatch alarm that pages when the tick
# pipeline stops producing fresh Parquet — the "real paging" backstop for the
# 2026-06 freezes that went unnoticed for ~10 days.
#
# It wires up the metric emitted hourly by scripts/pipeline_health.sh
# (PipelineHealthy = 1 healthy / 0 unhealthy in namespace $CLOUDWATCH_NAMESPACE).
# The alarm fires when:
#   * PipelineHealthy reports 0 (mirror stale / collector down / S3 sync dead), OR
#   * the metric goes MISSING (the box itself is down and stopped emitting) —
#     `--treat-missing-data breaching` makes silence page rather than hide.
#
# Idempotent: re-running updates the topic/subscription/alarm in place.
#
# Usage:
#   ALERT_EMAIL=you@example.com ./scripts/create_cloudwatch_alarm.sh
#
# Env (all optional except ALERT_EMAIL on first run):
#   ALERT_EMAIL           email to subscribe to the SNS topic (confirm via link)
#   AWS_REGION            region for the alarm/topic (default: from AWS config)
#   CLOUDWATCH_NAMESPACE  metric namespace (default: Trading/Pipeline)
#   SNS_TOPIC_NAME        topic name (default: trading-pipeline-alarms)
#   ALARM_NAME            alarm name (default: tick-pipeline-stale)
#   HOST_DIMENSION        Host dimension value (default: this host's hostname)
#   EVAL_PERIODS          consecutive bad hours before paging (default: 2)

set -euo pipefail

CLOUDWATCH_NAMESPACE="${CLOUDWATCH_NAMESPACE:-Trading/Pipeline}"
SNS_TOPIC_NAME="${SNS_TOPIC_NAME:-trading-pipeline-alarms}"
ALARM_NAME="${ALARM_NAME:-tick-pipeline-stale}"
HOST_DIMENSION="${HOST_DIMENSION:-$(hostname)}"
EVAL_PERIODS="${EVAL_PERIODS:-2}"

region_args=()
[ -n "${AWS_REGION:-}" ] && region_args=(--region "${AWS_REGION}")

command -v aws >/dev/null 2>&1 || { echo "aws CLI not found" >&2; exit 1; }

echo "Creating/looking up SNS topic ${SNS_TOPIC_NAME}..."
TOPIC_ARN="$(aws sns create-topic --name "${SNS_TOPIC_NAME}" \
    "${region_args[@]}" --query TopicArn --output text)"
echo "  topic: ${TOPIC_ARN}"

if [ -n "${ALERT_EMAIL:-}" ]; then
    echo "Subscribing ${ALERT_EMAIL} (check your inbox to CONFIRM)..."
    aws sns subscribe --topic-arn "${TOPIC_ARN}" \
        --protocol email --notification-endpoint "${ALERT_EMAIL}" \
        "${region_args[@]}" >/dev/null
else
    echo "  (no ALERT_EMAIL given — skipping subscription; the alarm still" \
         "publishes to the topic)"
fi

echo "Creating/updating alarm ${ALARM_NAME}..."
# PipelineHealthy is a 1/0 gauge emitted hourly (period 3600s). Alarm when the
# average over EVAL_PERIODS hours drops below 1 (i.e. any 0 or missing sample).
aws cloudwatch put-metric-alarm \
    --alarm-name "${ALARM_NAME}" \
    --alarm-description "Tick pipeline unhealthy or silent (stale Parquet / collector down / box down)" \
    --namespace "${CLOUDWATCH_NAMESPACE}" \
    --metric-name PipelineHealthy \
    --dimensions "Name=Host,Value=${HOST_DIMENSION}" \
    --statistic Minimum \
    --period 3600 \
    --evaluation-periods "${EVAL_PERIODS}" \
    --threshold 1 \
    --comparison-operator LessThanThreshold \
    --treat-missing-data breaching \
    --alarm-actions "${TOPIC_ARN}" \
    --ok-actions "${TOPIC_ARN}" \
    "${region_args[@]}"

cat <<EOF

Done.
  Alarm:     ${ALARM_NAME}
  Topic:     ${TOPIC_ARN}
  Metric:    ${CLOUDWATCH_NAMESPACE}/PipelineHealthy (Host=${HOST_DIMENSION})
  Fires when: PipelineHealthy < 1 for ${EVAL_PERIODS}h OR the metric goes missing.

Next steps:
  1. Confirm the SNS email subscription (link in your inbox), if you set ALERT_EMAIL.
  2. Ensure scripts/pipeline_health.sh runs hourly with CLOUDWATCH_NAMESPACE=${CLOUDWATCH_NAMESPACE}
     exported (add it to .env). setup_ec2.sh installs the cron.
  3. The EC2 instance role needs: cloudwatch:PutMetricData and (for this script)
     sns:CreateTopic, sns:Subscribe, cloudwatch:PutMetricAlarm.
EOF
