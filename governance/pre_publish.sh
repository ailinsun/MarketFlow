#!/bin/sh
# Run the standalone publication checks from any working directory.
case "${1-}" in
  -h|--help) echo 'Usage: sh governance/pre_publish.sh'; exit 0 ;;
esac
set -eu
cd "$(dirname "$0")/.."
make check
make test
