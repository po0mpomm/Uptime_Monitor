# Hypothetical AWS deployment sketch — ECS Fargate (api + worker) + RDS + ALB + S3/CloudFront
# NOTE: This is NOT a complete, hardened Terraform configuration.
# Security groups, IAM least-privilege, secrets manager integration,
# and TLS certificates are explicitly out of scope per the assignment brief.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

variable "db_password" {
  description = "RDS Postgres password — supply via TF_VAR_db_password env var, never committed"
  type        = string
  sensitive   = true
}

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name               = "uptime-monitor-vpc"
  cidr               = "10.0.0.0/16"
  azs                = ["us-east-1a", "us-east-1b"]
  public_subnets     = ["10.0.1.0/24", "10.0.2.0/24"]
  private_subnets    = ["10.0.3.0/24", "10.0.4.0/24"]
  enable_nat_gateway = true
}

# ---------------------------------------------------------------------------
# RDS Postgres (~$15/mo on db.t3.micro)
# ---------------------------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name       = "uptime-monitor-db-subnets"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_db_instance" "postgres" {
  identifier           = "uptime-monitor-db"
  engine               = "postgres"
  engine_version       = "16"
  instance_class       = "db.t3.micro"
  allocated_storage    = 20
  db_name              = "uptime"
  username             = "uptime"
  password             = var.db_password
  db_subnet_group_name = aws_db_subnet_group.main.name
  skip_final_snapshot  = true
}

# ---------------------------------------------------------------------------
# ECR + ECS cluster
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "backend" {
  name = "uptime-monitor-backend"
}

resource "aws_ecs_cluster" "main" {
  name = "uptime-monitor"
}

resource "aws_iam_role" "ecs_exec" {
  name = "uptime-monitor-ecs-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_exec_policy" {
  role       = aws_iam_role.ecs_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------------------
# ECS Task: api (HTTP-facing, behind ALB)
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "uptime-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_exec.arn

  container_definitions = jsonencode([{
    name  = "api"
    image = "${aws_ecr_repository.backend.repository_url}:latest"
    command = [
      "uvicorn", "app.main:app",
      "--host", "0.0.0.0", "--port", "8000"
    ]
    portMappings = [{ containerPort = 8000 }]
    environment = [
      {
        name  = "DATABASE_URL"
        value = "postgresql+asyncpg://uptime:${var.db_password}@${aws_db_instance.postgres.endpoint}/uptime"
      }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/uptime-api"
        "awslogs-region"        = "us-east-1"
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
}

# ---------------------------------------------------------------------------
# ECS Task: worker — same image, different command, no ALB target
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "worker" {
  family                   = "uptime-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_exec.arn

  container_definitions = jsonencode([{
    name    = "worker"
    image   = "${aws_ecr_repository.backend.repository_url}:latest"
    command = ["python", "-m", "worker.main"]
    environment = [
      {
        name  = "DATABASE_URL"
        value = "postgresql+asyncpg://uptime:${var.db_password}@${aws_db_instance.postgres.endpoint}/uptime"
      }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/uptime-worker"
        "awslogs-region"        = "us-east-1"
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

# ---------------------------------------------------------------------------
# ECS Services
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "api" {
  name            = "uptime-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = module.vpc.private_subnets
    security_groups = []
  }
}

resource "aws_ecs_service" "worker" {
  name            = "uptime-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = module.vpc.private_subnets
    security_groups = []
  }
  # No load_balancer block — worker is not HTTP-facing
}

# ---------------------------------------------------------------------------
# ALB — fronts only the api service
# ---------------------------------------------------------------------------

resource "aws_lb" "main" {
  name               = "uptime-alb"
  load_balancer_type = "application"
  subnets            = module.vpc.public_subnets
}

resource "aws_lb_target_group" "api" {
  name        = "uptime-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = module.vpc.vpc_id
  target_type = "ip"

  health_check {
    path = "/health"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ---------------------------------------------------------------------------
# Frontend — S3 + CloudFront (Next.js static export)
# If Next.js SSR routes are required in production, use a separate Fargate
# service instead (mirrors the api pattern, same as local docker-compose).
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "frontend" {
  bucket = "uptime-monitor-frontend-${data.aws_caller_identity.current.account_id}"
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"

  origin {
    domain_name = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id   = "S3-frontend"
  }

  default_cache_behavior {
    target_origin_id       = "S3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }
  }

  # SPA-style routing: 404 → /index.html
  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.frontend.domain_name
}

# ---------------------------------------------------------------------------
# Estimated MVP cost: ~$25–40/month
# - RDS db.t3.micro:   ~$15/mo
# - 2x ECS Fargate:    ~$8/mo  (256 vCPU / 512MB each)
# - ALB:               ~$4/mo
# - S3/CloudFront:     ~$1/mo  (at this traffic level)
# ---------------------------------------------------------------------------
