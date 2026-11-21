#!/usr/bin/env bash
# Automatisierter Bootstrap des Single-Node-K3s-Messclusters (K3s, ArgoCD, Prometheus)
set -euo pipefail
exec > >(tee -a /var/log/bootstrap-cluster.log) 2>&1

K3S_VERSION="v1.37.2+k3s1"
ARGOCD_CHART_VERSION="10.18.2"
PROMETHEUS_CHART_VERSION="29.48.0"

echo "=== [1/7] Swapfile (4 GiB) + vm.swappiness=1 ==="
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi
sysctl -w vm.swappiness=1
echo "vm.swappiness=1" > /etc/sysctl.d/99-swappiness.conf

echo "=== [2/7] K3s ${K3S_VERSION} installieren ==="
TOKEN_IMDS=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
PUBLIC_IPV4=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN_IMDS" http://169.254.169.254/latest/meta-data/public-ipv4 || true)

curl -sfL https://get.k3s.io | \
  INSTALL_K3S_VERSION="$K3S_VERSION" \
  INSTALL_K3S_EXEC="--disable traefik --disable metrics-server --tls-san ${PUBLIC_IPV4:-127.0.0.1} --write-kubeconfig-mode 644 --kubelet-arg fail-swap-on=false" \
  sh -

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo "=== [3/7] Kubeconfig fuer ubuntu einrichten ==="
mkdir -p /home/ubuntu/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube
echo "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml" >> /home/ubuntu/.bashrc

echo "=== [4/7] Warte auf K3s Node Readiness ==="
until kubectl get nodes | grep -q " Ready"; do
  sleep 3
done

echo "=== [5/7] Helm v3 installieren ==="
curl -fsSL -o /tmp/get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3
chmod 700 /tmp/get_helm.sh
/tmp/get_helm.sh
rm -f /tmp/get_helm.sh

echo "=== [6/7] Namespaces und Helm-Repos anlegen ==="
kubectl create namespace argocd || true
kubectl create namespace monitoring || true
kubectl create namespace workloads || true

helm repo add argo https://argoproj.github.io/argo-helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

echo "=== [7/7] ArgoCD (${ARGOCD_CHART_VERSION}), Prometheus (${PROMETHEUS_CHART_VERSION}) & Baseline-App ==="
helm install argo-cd argo/argo-cd \
  --version "$ARGOCD_CHART_VERSION" \
  --namespace argocd \
  --values /opt/helm-values/values-argocd.yaml \
  --wait --timeout 5m

helm install prometheus prometheus-community/prometheus \
  --version "$PROMETHEUS_CHART_VERSION" \
  --namespace monitoring \
  --values /opt/helm-values/values-prometheus.yaml \
  --wait --timeout 5m

until kubectl get crd applications.argoproj.io >/dev/null 2>&1; do
  sleep 2
done
kubectl apply -f /opt/k8s/application-baseline.yaml

echo "Bootstrap abgeschlossen: $(date -u '+%Y-%m-%dT%H:%M:%SZ')" > /var/log/bootstrap-complete.log
