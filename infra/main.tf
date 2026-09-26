terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  type    = string
  default = "ap-southeast-2"
}

variable "bucket_name" {
  type    = string
  default = "trading-data-centheos"
}

variable "collector_sg_id" {
  type        = string
  default     = ""
  description = "Security group of collector EC2 (for ElastiCache ingress)."
}

variable "vpc_id" {
  type    = string
  default = ""
}

variable "private_subnet_ids" {
  type    = list(string)
  default = []
}

# ---------------------------------------------------------------------------
# S3 lifecycle — expire noncurrent versions; abort incomplete multipart
# ---------------------------------------------------------------------------

resource "aws_s3_bucket_lifecycle_configuration" "trading_data" {
  bucket = var.bucket_name

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter { prefix = "" }

    noncurrent_version_expiration {
      noncurrent_days = 14
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---------------------------------------------------------------------------
# ElastiCache Redis (optional until VPC/subnet vars are set)
# ---------------------------------------------------------------------------

resource "aws_security_group" "elasticache" {
  count       = var.vpc_id != "" ? 1 : 0
  name        = "trading-md-redis"
  description = "ElastiCache Redis for market-data streams"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Redis from collector"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = var.collector_sg_id != "" ? [var.collector_sg_id] : []
    cidr_blocks     = var.collector_sg_id == "" ? ["10.0.0.0/8"] : []
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_elasticache_subnet_group" "md" {
  count      = length(var.private_subnet_ids) > 0 ? 1 : 0
  name       = "trading-md-redis"
  subnet_ids = var.private_subnet_ids
}

resource "aws_elasticache_cluster" "md" {
  count                = length(var.private_subnet_ids) > 0 && var.vpc_id != "" ? 1 : 0
  cluster_id           = "trading-md"
  engine               = "redis"
  node_type            = "cache.t4g.small"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.md[0].name
  security_group_ids   = [aws_security_group.elasticache[0].id]
}

# CloudWatch alarms tick-pipeline-stale and tick-disk-low are created only by
# scripts/create_cloudwatch_alarm.sh. That script sets the live Host dimension
# and the SNS actions. Declaring the same alarm name here would upsert over
# those settings (wrong Host, or paging actions dropped).
#
# Forget any copy already in state without destroying it, so apply does not
# delete the alarm the script is paging on. The script's next run reconciles
# the configuration in place.
removed {
  from = aws_cloudwatch_metric_alarm.tick_pipeline_stale

  lifecycle {
    destroy = false
  }
}

output "redis_endpoint" {
  value = try(aws_elasticache_cluster.md[0].cache_nodes[0].address, null)
}

output "notes" {
  value = <<-EOT
    IAM: scope TradingCollectorRole S3 to arn:aws:s3:::${var.bucket_name} and /*.
    Set REDIS_URL=redis://<redis_endpoint>:6379 on the collector host.
    Strategy/BookMap EC2s are out of scope for this stack.
  EOT
}
