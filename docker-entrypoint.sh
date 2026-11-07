#!/bin/sh
# Runs as root: try to mount the NAS share over the downloads dir, then drop
# to appuser for the web server. If the NAS is unreachable the server still
# starts (the UI flags local-only mode) and a background loop keeps retrying
# the mount until it succeeds.
set -u

MOUNT_POINT=/app/webui/downloads
CREDENTIALS_FILE=/run/nas-credentials
RETRY_SECONDS="${NAS_RETRY_SECONDS:-60}"

is_mounted() {
    awk -v mp="$MOUNT_POINT" '$2 == mp && ($3 == "cifs" || $3 == "smb3") { found = 1 } END { exit !found }' /proc/mounts
}

try_mount() {
    # Credentials go via a root-only file rather than -o password=..., so the
    # password never shows up in `ps` output visible to appuser.
    timeout 15 mount -t cifs "//${NAS_HOST}/${NAS_SHARE}" "$MOUNT_POINT" \
        -o "credentials=${CREDENTIALS_FILE},vers=3.0,uid=1000,gid=1000,file_mode=0664,dir_mode=0775,rw,soft" \
        2>&1
}

if [ -n "${NAS_HOST:-}" ]; then
    (umask 077 && printf 'username=%s\npassword=%s\n' "${NAS_USER:-}" "${NAS_PASS:-}" > "$CREDENTIALS_FILE")

    if try_mount; then
        echo "NAS mounted: //${NAS_HOST}/${NAS_SHARE} -> ${MOUNT_POINT}"
    else
        echo "WARNING: could not mount //${NAS_HOST}/${NAS_SHARE}; running in local-only mode, retrying every ${RETRY_SECONDS}s"
    fi

    (
        while sleep "$RETRY_SECONDS"; do
            if ! is_mounted && try_mount >/dev/null; then
                echo "NAS mounted: //${NAS_HOST}/${NAS_SHARE} -> ${MOUNT_POINT}"
            fi
        done
    ) &
fi

# NAS_HOST stays set so the server knows to expect a NAS; the credentials are
# stripped so they aren't readable from the server process's environment.
exec env -u NAS_USER -u NAS_PASS \
    setpriv --reuid=appuser --regid=appuser --init-groups \
    python3 /app/webui/server.py
