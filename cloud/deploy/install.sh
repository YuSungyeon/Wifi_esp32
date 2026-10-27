#!/usr/bin/env bash
# EC2(Ubuntu 24.04 / Debian 12)에 클라우드를 설치하거나 갱신한다 — 다시 돌려도 된다 (계획 §5.2).
# 맥에서 파일을 /tmp/meshsense 로 올린 뒤:
#   ssh -i <키.pem> ubuntu@<IP> 'sudo bash /tmp/meshsense/cloud/deploy/install.sh'   (Debian 이면 admin@)
set -euo pipefail
SRC=/tmp/meshsense
APP=/opt/meshsense

apt-get update -q
apt-get install -y -q python3-venv caddy
id meshsense >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin meshsense
mkdir -p "$APP" /var/lib/meshsense /etc/meshsense
cp -r "$SRC/cloud" "$SRC/realtime" "$APP/"
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q -r "$APP/cloud/requirements.txt"
chown -R meshsense: /var/lib/meshsense

# 비밀 값 파일은 처음 한 번만 만든다 — 다시 돌려도 채워 둔 키를 덮지 않는다
if [ ! -f /etc/meshsense/cloud.env ]; then
  cp "$APP/cloud/deploy/cloud.env.example" /etc/meshsense/cloud.env
  sed -i "s/^MESHSENSE_SECRET=$/MESHSENSE_SECRET=$(openssl rand -hex 32)/" /etc/meshsense/cloud.env
  chmod 600 /etc/meshsense/cloud.env
fi

cp "$APP/cloud/deploy/meshsense-cloud.service" /etc/systemd/system/
cp "$APP/cloud/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable meshsense-cloud
systemctl restart meshsense-cloud
systemctl reload caddy || systemctl restart caddy
sleep 2
systemctl --no-pager --lines=5 status meshsense-cloud
