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

variable "image_id" {
  type = string
}

variable "instance_type" {
  type = string
}

variable "live_run_id" {
  type    = string
  default = ""
}

provider "aws" {
  region                      = var.region
  max_retries                 = 5
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true

  endpoints {
    ec2 = var.ec2_endpoint
    sts = var.ec2_endpoint
    iam = var.ec2_endpoint
  }

  default_tags {
    tags = merge({
      "vws-compat" = var.run_id
    }, var.live_run_id == "" ? {} : { "vws-live-run" = var.live_run_id })
  }
}

resource "aws_instance" "this" {
  ami           = var.image_id
  instance_type = var.instance_type
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
