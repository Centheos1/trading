# Collector infrastructure (Terraform)

Provisions:

- S3 lifecycle (noncurrent version expiry, abort incomplete multipart)
- Optional ElastiCache Redis (`cache.t4g.small`) when `vpc_id` + `private_subnet_ids` are set
- CloudWatch `tick-pipeline-stale` alarm on `Trading/Pipeline` / `PipelineHealthy`

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
