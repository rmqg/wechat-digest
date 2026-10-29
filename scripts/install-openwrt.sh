#!/bin/sh
set -eu

usage() {
    printf '%s\n' 'Usage: install-openwrt.sh [--root PATH] [--dry-run | --apply]' 'Default: dry-run; --root is a staging filesystem root (default /).'
}

ROOT=/
MODE=dry-run
while [ "$#" -gt 0 ]; do
    case "$1" in
        --root)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            ROOT=$2
            shift 2
            ;;
        --dry-run)
            MODE=dry-run
            shift
            ;;
        --apply)
            MODE=apply
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done
case "$ROOT" in
    /*) ;;
    *) printf '%s\n' '--root must be an absolute filesystem path' >&2; exit 2 ;;
esac
case "$ROOT" in
    *'
'*) printf '%s\n' '--root must not contain a newline' >&2; exit 2 ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
INITD_DIR=${ROOT%/}/etc/init.d
CRON_FILE=${ROOT%/}/etc/crontabs/root
CRON_JOB='* * * * * cd /opt/wechat-digest && /usr/bin/python3 deployment/daily-job.py >> runtime/digest/daily-job.log 2>&1'

for template in wechat-image-origin.initd wechat-summary-commands.initd wechat-images.initd; do
    [ -f "$PROJECT_DIR/deployment/$template" ] || { printf 'Missing service template: %s\n' "$template" >&2; exit 1; }
done
printf '%s\n' "Mode: $MODE" "Filesystem root: $ROOT" "Install services to: $INITD_DIR" "Cron file: $CRON_FILE"
for service in wechat-image-origin wechat-summary-commands wechat-images; do
    printf '  %s -> %s/%s\n' "$service" "$INITD_DIR" "$service"
done
printf '  minute cron -> %s\n' "$CRON_JOB"
[ "$MODE" = apply ] || exit 0

[ -d "$ROOT" ] || { printf '%s\n' 'Target root does not exist; refusing to create an arbitrary filesystem root.' >&2; exit 1; }
mkdir -p "$INITD_DIR" "$(dirname "$CRON_FILE")"
for service in wechat-image-origin wechat-summary-commands wechat-images; do
    destination=$INITD_DIR/$service
    [ ! -L "$destination" ] || { printf 'Refusing symlink destination: %s\n' "$destination" >&2; exit 1; }
    case "$service" in
        wechat-image-origin) source=$PROJECT_DIR/deployment/wechat-image-origin.initd ;;
        wechat-summary-commands) source=$PROJECT_DIR/deployment/wechat-summary-commands.initd ;;
        wechat-images) source=$PROJECT_DIR/deployment/wechat-images.initd ;;
    esac
    install -m 0755 "$source" "$destination"
done

[ ! -L "$CRON_FILE" ] || { printf 'Refusing symlink cron file: %s\n' "$CRON_FILE" >&2; exit 1; }
if [ -e "$CRON_FILE" ] && grep -Fqx -- "$CRON_JOB" "$CRON_FILE"; then
    printf '%s\n' 'Minute cron entry already present; leaving cron file unchanged.'
else
    temporary=$CRON_FILE.tmp.$$
    trap 'rm -f "$temporary"' EXIT HUP INT TERM
    if [ -f "$CRON_FILE" ]; then
        cat "$CRON_FILE" > "$temporary"
    else
        : > "$temporary"
    fi
    [ ! -s "$temporary" ] || [ "$(tail -c 1 "$temporary" | wc -l | tr -d ' ')" -eq 1 ] || printf '\n' >> "$temporary"
    printf '%s\n' "$CRON_JOB" >> "$temporary"
    chmod 0600 "$temporary"
    mv "$temporary" "$CRON_FILE"
    trap - EXIT HUP INT TERM
    printf '%s\n' 'Added minute cron entry without changing unrelated lines.'
fi
printf '%s\n' 'Installed service scripts. Enable/start only after project configuration and runtime are ready:'
printf '%s\n' '  /etc/init.d/wechat-image-origin enable && /etc/init.d/wechat-image-origin start'
printf '%s\n' '  /etc/init.d/wechat-summary-commands enable && /etc/init.d/wechat-summary-commands start'
printf '%s\n' '  /etc/init.d/wechat-images enable && /etc/init.d/wechat-images start'
