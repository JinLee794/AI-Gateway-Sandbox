#!/bin/bash
# Installs the Hyperlight executor (infra/hyperlight/executor.py) as a systemd service. Run by the VM's CustomScript
# extension (infra/hyperlight.bicep); __EXECUTOR_B64__ is replaced with the base64 of executor.py at deployment.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if [ ! -e /dev/kvm ]; then
  echo "no /dev/kvm: this VM size or security type has no nested virtualization; Hyperlight cannot run" >&2
  exit 1
fi

apt-get update -q
apt-get install -yq python3-venv
id hl >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hl
mkdir -p /opt/hyperlight
[ -x /opt/hyperlight/venv/bin/python ] || python3 -m venv /opt/hyperlight/venv
/opt/hyperlight/venv/bin/pip install -q --upgrade "hyperlight-sandbox>=0.8,<0.9" "hyperlight-sandbox-backend-wasm>=0.8,<0.9" \
  "hyperlight-sandbox-python-guest>=0.8,<0.9"
echo '__EXECUTOR_B64__' | base64 -d > /opt/hyperlight/executor.py

cat > /etc/systemd/system/hyperlight-executor.service <<'EOF'
[Unit]
Description=Hyperlight executor for the AI Gateway Sandbox (Code mode scenario)
After=network-online.target

[Service]
User=hl
Group=hl
SupplementaryGroups=kvm
StateDirectory=hyperlight
Environment=HOME=/var/lib/hyperlight PORT=8080
ExecStart=/opt/hyperlight/venv/bin/python /opt/hyperlight/executor.py
Restart=always
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hyperlight-executor
systemctl restart hyperlight-executor
sleep 5
curl -fsS http://127.0.0.1:8080/health
