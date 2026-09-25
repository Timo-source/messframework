# Ermittelt dynamisch deine reale öffentliche WAN-IP beim terraform apply
data "http" "my_public_ip" {
  url = "https://checkip.amazonaws.com"
}

locals {
  detected_ip  = chomp(data.http.my_public_ip.response_body)
  allowed_cidr = var.override_allowed_cidr != null ? var.override_allowed_cidr : "${local.detected_ip}/32"
}

resource "aws_security_group" "k3s_node_sg" {
  name        = "messframework-node-sg"
  description = "Security Group fuer K3s Mess-Node (Restriktiv auf eigene Public IP)"
  vpc_id      = aws_vpc.main.id

  # SSH-Zugriff
  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [local.allowed_cidr]
  }

  # K3s Kubernetes API Server
  ingress {
    description = "Kubernetes API"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = [local.allowed_cidr]
  }

  # HTTP / HTTPS
  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [local.allowed_cidr]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [local.allowed_cidr]
  }

  # K8s NodePort Range (z. B. Grafana 30000, ArgoCD UI 30080)
  ingress {
    description = "K8s NodePort Services"
    from_port   = 30000
    to_port     = 32767
    protocol    = "tcp"
    cidr_blocks = [local.allowed_cidr]
  }

  # Vollständiger Egress (für GitHub Sync, Paket-Downloads, Helm Repositories)
  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = {
    Name = "messframework-node-sg"
  }
}
