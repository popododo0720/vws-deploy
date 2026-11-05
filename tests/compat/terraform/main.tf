terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.72.0"
    }
  }
}

variable "run_id" {
  type = string
}

variable "region" {
  type = string
}

variable "ec2_endpoint" {
  type = string
}

variable "s3_endpoint" {
  type = string
}

variable "image_id" {
  type = string
}

variable "instance_type" {
  type = string
}

provider "aws" {
  region                      = var.region
  max_retries                 = 5
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  s3_use_path_style           = true

  endpoints {
    ec2 = var.ec2_endpoint
    s3  = var.s3_endpoint
    sts = var.ec2_endpoint
    iam = var.ec2_endpoint
  }

  default_tags {
    tags = {
      "vws-compat" = var.run_id
    }
  }
}

resource "aws_vpc" "this" {
  cidr_block = "10.231.0.0/16"
}

resource "aws_subnet" "this" {
  vpc_id     = aws_vpc.this.id
  cidr_block = "10.231.1.0/24"
}

resource "aws_security_group" "this" {
  name        = var.run_id
  description = "vws compat"
  vpc_id      = aws_vpc.this.id

  ingress {
    protocol    = "tcp"
    from_port   = 22
    to_port     = 22
    cidr_blocks = ["10.231.0.0/16"]
  }
}

resource "aws_instance" "this" {
  ami                    = var.image_id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.this.id
  vpc_security_group_ids = [aws_security_group.this.id]
}

resource "aws_ebs_volume" "this" {
  availability_zone = aws_instance.this.availability_zone
  size              = 1
}

resource "aws_volume_attachment" "this" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.this.id
  instance_id = aws_instance.this.id
}

resource "aws_eip" "this" {
  domain = "vpc"
}

resource "aws_eip_association" "this" {
  allocation_id = aws_eip.this.id
  instance_id   = aws_instance.this.id
}

resource "aws_s3_bucket" "this" {
  bucket        = var.run_id
  force_destroy = true
}

resource "aws_s3_object" "this" {
  bucket  = aws_s3_bucket.this.id
  key     = "compat.txt"
  content = "vws compat ${var.run_id}"
}
