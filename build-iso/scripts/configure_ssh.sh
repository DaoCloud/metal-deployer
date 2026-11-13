#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: configure_ssh.sh must be run as root. Try: sudo $0" >&2
    exit 1
fi

KEYS_FILE="${SSH_AUTHORIZED_KEYS_FILE:-/opt/resource/config/ssh_authorized_keys}"
USERS="${SSH_AUTHORIZED_KEY_USERS:-admin root}"

install_keys_for_user() {
    local user="$1"
    local home

    if [ "$user" = "root" ]; then
        home="/root"
    else
        home=$(getent passwd "$user" | cut -d: -f6 || true)
    fi

    [ -n "$home" ] || return 0
    [ -d "$home" ] || return 0

    install -d -m 0700 "${home}/.ssh"
    touch "${home}/.ssh/authorized_keys"
    chmod 0600 "${home}/.ssh/authorized_keys"

    if [ -f "$KEYS_FILE" ]; then
        while IFS= read -r key; do
            case "$key" in
                ""|\#*) continue ;;
            esac
            grep -qxF "$key" "${home}/.ssh/authorized_keys" || echo "$key" >> "${home}/.ssh/authorized_keys"
        done < "$KEYS_FILE"
    fi

    chown -R "${user}:${user}" "${home}/.ssh" 2>/dev/null || chown -R root:root "${home}/.ssh"
}

for user in $USERS; do
    install_keys_for_user "$user"
done

# sshd keeps the first value it reads and includes sshd_config.d/*.conf at the
# top of sshd_config, so this drop-in wins over later settings: root logs in
# with keys only, never with a password.
SSHD_DROPIN="${SSHD_DROPIN:-/etc/ssh/sshd_config.d/10-metal-deployer.conf}"
install -d -m 0755 "$(dirname "$SSHD_DROPIN")"
cat > "$SSHD_DROPIN" <<'EOF'
# Managed by metal-deployer (configure_ssh.sh)
PermitRootLogin prohibit-password
EOF
chmod 0644 "$SSHD_DROPIN"
if command -v sshd >/dev/null 2>&1; then
    # Ubuntu 24.04 socket-activates ssh; sshd -t needs this dir before the service first starts.
    install -d -m 0755 /run/sshd
    if ! sshd -t; then
        echo "Warning: sshd config check failed; removing ${SSHD_DROPIN}" >&2
        rm -f "$SSHD_DROPIN"
    fi
fi
systemctl try-reload-or-restart ssh.service 2>/dev/null || true

if [ "${GENERATE_NODE_SSH_KEY:-false}" = "true" ] && [ ! -f /root/.ssh/id_ed25519 ]; then
    install -d -m 0700 /root/.ssh
    ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -C "root@$(hostname)-$(date +%Y%m%d%H%M%S)"
    chmod 0600 /root/.ssh/id_ed25519
    chmod 0644 /root/.ssh/id_ed25519.pub
fi

if [ -f "$KEYS_FILE" ]; then
    echo "Installed SSH authorized_keys from ${KEYS_FILE} for: ${USERS}"
else
    echo "No SSH authorized_keys file found at ${KEYS_FILE}; password SSH remains available if enabled in user-data."
fi
