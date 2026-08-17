#!/usr/bin/env bash
# Static regression guard for F-113: validate labels before the Docker stage.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/cx-distro/rebuild/rebuild-v67.sh"

bash -n "${SCRIPT}"
rg -q 'LABEL="\$\{LABEL:-v6\.7\}"' "${SCRIPT}"
rg -q '\[\[ ! "\$LABEL" =~ \^v\[0-9\]\+\(\\\.\[0-9\]\+\)\{0,2\}\[a-z\]\?\$ \]\]' "${SCRIPT}"
rg -q 'LABEL must match \^v\[0-9\]\+' "${SCRIPT}"

echo "PASS: rebuild wrapper validates labels before Docker work."
