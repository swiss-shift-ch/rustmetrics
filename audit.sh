#!/bin/bash
# Server-side audit helper.
set +e

echo "============ SYSTEM ============"
hostnamectl --static
uname -srm
uptime -p

echo ""
echo "============ SSH ============"
grep -hiE '^(PermitRootLogin|PasswordAuthentication|PermitEmptyPasswords|ChallengeResponseAuthentication|KbdInteractiveAuthentication|MaxAuthTries|X11Forwarding|PubkeyAuthentication|AllowUsers|AllowGroups|Port)' \
  /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null | sort -u

echo ""
echo "============ UFW ============"
ufw status verbose 2>&1 | head -20

echo ""
echo "============ fail2ban ============"
fail2ban-client status 2>&1
echo "--- sshd jail ---"
fail2ban-client status sshd 2>&1 | head -10

echo ""
echo "============ Listening ports ============"
ss -tlnp | awk 'NR==1 || /LISTEN/'

echo ""
echo "============ Rustmetrics-Dateien Permissions ============"
ls -la /opt/rustmetrics/.env /opt/rustmetrics/rustmetrics.py /opt/rustmetrics/static/
stat -c "%a %n" /opt/rustmetrics/.env

echo ""
echo "============ Postgres Auth ============"
sudo -u postgres psql -tAc "SELECT rolname, rolcanlogin, rolsuper FROM pg_roles WHERE rolname IN ('rustmetrics','postgres')"
echo "--- pg_hba ---"
grep -vE '^\s*#|^\s*$' /etc/postgresql/*/main/pg_hba.conf

echo ""
echo "============ Systemd-Unit-Hardening ============"
systemctl cat rustmetrics 2>&1 | grep -E 'User=|Group=|NoNewPriv|Protect|PrivateTmp|PrivateDevices|Restrict|MemoryHigh|MemoryMax|LockPersonality|ReadOnlyPaths|ReadWritePaths'

echo ""
echo "============ Caddy-Block für rustmetrics.eu ============"
awk '/^rustmetrics.eu/,/^\}/' /etc/caddy/Caddyfile

echo ""
echo "============ Apt-Updates verfügbar ============"
apt list --upgradable 2>/dev/null | grep -i security | head -10
echo "(total upgradable: $(apt list --upgradable 2>/dev/null | grep -v Listing | wc -l) packages)"

echo ""
echo "============ Auth-Log Auffälligkeiten (letzte 100 Failed) ============"
journalctl --since "24 hours ago" | grep -iE "failed password|invalid user" | wc -l
echo "Failed attempts in last 24h"
