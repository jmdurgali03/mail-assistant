#!/usr/bin/env bash
# Synthetic checks with a clean HOME; not a filesystem/network sandbox.
set -euo pipefail
mail_check_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mail_check_home="$(mktemp -d)"
trap 'rm -rf -- "$mail_check_home"' EXIT
cd -- "$mail_check_root"
mail_check_python="$(command -v python3)"
mail_check_node="$(command -v node)"
mail_check_path="$(dirname -- "$mail_check_python"):$(dirname -- "$mail_check_node"):/usr/bin:/bin"
env -i HOME="$mail_check_home" PATH="$mail_check_path" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$mail_check_root/src:$mail_check_root/tests" \
  "$mail_check_python" -m unittest discover -s tests -v
env -i HOME="$mail_check_home" PATH="$mail_check_path" LANG=C.UTF-8 \
  "$mail_check_node" --test plugins/telegram-plugin/test_plugin.mjs plugins/actions-plugin/test_plugin.mjs
env -i HOME="$mail_check_home" PATH="$mail_check_path" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$mail_check_root/src:$mail_check_root/tests" \
  "$mail_check_python" src/demo.py
