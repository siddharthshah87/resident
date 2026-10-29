#!/bin/bash
# Download the pinned agentd runtime (Claude Agent SDK + deps) as Python 3.13 x86-64 wheels for the image.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
W=mkosi.extra/usr/lib/resident/wheels
PIP=${PIP:-pip3}
mkdir -p "$W"
"$PIP" download --disable-pip-version-check --only-binary=:all: --python-version 3.13 --implementation cp --abi cp313 \
  --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 --platform any \
  -d "$W" "claude-agent-sdk==0.2.174"
echo "staged $(ls "$W" | wc -l) wheels in $W"
