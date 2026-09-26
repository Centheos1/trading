# Collector infrastructure (Terraform)

Provisions:

- S3 lifecycle (noncurrent version expiry, abort incomplete multipart)
- Optional ElastiCache Redis (`cache.t4g.small`) when `vpc_id` + `private_subnet_ids` are set

CloudWatch alarms `tick-pipeline-stale` and `tick-disk-low` are **not** in this
stack. `scripts/create_cloudwatch_alarm.sh` is the only writer: it sets `Host`
from the collector hostname and attaches the SNS actions. A Terraform resource
with the same alarm name would overwrite that configuration. If state still
contains `aws_cloudwatch_metric_alarm.tick_pipeline_stale`, the `removed` block
in `main.tf` drops it from state without deleting the live alarm.

## Apply

```bash
cd infra
terraform init
terraform plan -var='vpc_id=...' -var='private_subnet_ids=["subnet-a","subnet-b"]' \
  -var='collector_sg_id=sg-...'
terraform apply   # only when plan is non-empty; gated in GitHub Actions
```

GitHub Actions: `.github/workflows/deploy-infra.yml` runs only on `infra/**` changes
and requires the `production` environment approval.

Local `terraform apply` writes `terraform.tfstate` here. That file, `.terraform/`,
and `*.tfvars` are gitignored. Commit `.terraform.lock.hcl` after `terraform init`
so provider versions stay reproducible. GitHub Infra CD still needs a remote
backend before it can share this state.
