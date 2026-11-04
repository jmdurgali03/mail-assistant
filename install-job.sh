#!/usr/bin/env bash
# Install files ONLY; never reload/start/enable a scheduler.
set -euo pipefail
umask 077
mail_source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mail_units_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p -- "$mail_units_dir"
for mail_unit in bichi-mail.service bichi-mail.timer; do
  if [[ -e "$mail_units_dir/$mail_unit" || -L "$mail_units_dir/$mail_unit" ]]; then
    printf 'Refusing to overwrite existing unit: %s\n' "$mail_units_dir/$mail_unit" >&2
    exit 1
  fi
done
install -m 0600 -- "$mail_source_dir/systemd/bichi-mail.service" "$mail_units_dir/bichi-mail.service"
install -m 0600 -- "$mail_source_dir/systemd/bichi-mail.timer" "$mail_units_dir/bichi-mail.timer"
printf 'Unit files installed only. Timer NOT enabled or started. No configuration changed.\n'
