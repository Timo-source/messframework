output "instance_public_ip" {
  description = "Oeffentliche IP-Adresse der K3s EC2-Instanz"
  value       = aws_instance.k3s_node.public_ip
}

output "ssh_login_command" {
  description = "Direkter SSH-Verbindungsbefehl"
  value       = "ssh -i ~/.ssh/id_ed25519 ubuntu@${aws_instance.k3s_node.public_ip}"
}

output "bootstrap_status_command" {
  description = "Befehl zum Live-Verfolgen des Bootstrap-Fortschritts"
  value       = "ssh -i ~/.ssh/id_ed25519 ubuntu@${aws_instance.k3s_node.public_ip} \"tail -f /var/log/bootstrap-cluster.log\""
}

output "argocd_ui_url" {
  description = "URL zur ArgoCD Web-Oberflaeche"
  value       = "http://${aws_instance.k3s_node.public_ip}:30080"
}

output "argocd_initial_password_command" {
  description = "Befehl zum Auslesen des initialen ArgoCD Admin-Passworts"
  value       = "ssh -i ~/.ssh/id_ed25519 ubuntu@${aws_instance.k3s_node.public_ip} \"sudo kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo\""
}

output "prometheus_ui_url" {
  description = "URL zur Prometheus Web-Oberflaeche"
  value       = "http://${aws_instance.k3s_node.public_ip}:30090"
}
