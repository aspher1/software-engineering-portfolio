#!/bin/bash
# Restart the PriceStalker stack after a VM replacement/reboot.
# This VM's container runtime can't program nftables, so Docker runs with
# --bridge=none + --iptables=false and every service uses host networking
# (see docker-compose.override.yaml). containerd needs the proxy env to pull.
set -e
cd "$(dirname "$0")"

if ! pgrep -x containerd >/dev/null; then
  echo "starting containerd..."
  setsid nohup containerd > /tmp/containerd.log 2>&1 < /dev/null &
  sleep 3
fi
if ! docker ps >/dev/null 2>&1; then
  echo "starting dockerd..."
  setsid nohup dockerd > /tmp/dockerd.log 2>&1 < /dev/null &
  for i in $(seq 1 30); do docker ps >/dev/null 2>&1 && break; sleep 2; done
fi
docker ps >/dev/null 2>&1 || { echo "dockerd failed to start, see /tmp/dockerd.log"; exit 1; }

echo "starting stack..."
docker compose up -d
sleep 5
docker ps --format '{{.Names}}: {{.Status}}'
echo "dashboard: http://localhost (this VM)"
