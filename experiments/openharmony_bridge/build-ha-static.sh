#!/bin/sh
set -eu
# 单独输出静态 musl ARM64 程序，不替换当前运行中的 Ubuntu 构建。
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export POC_ZIG_TARGET=aarch64-linux-musl
cmake -S "$root" -B "$root/build-ha-static-arm64" \
  -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
  -DCMAKE_C_COMPILER="$root/zig-cc.sh" -DCMAKE_EXE_LINKER_FLAGS=-static \
  -DPOC_AILIFE_LEGACY_DISCOVERY=ON -DPOC_LAN_PORT=15684 -DPOC_LOG_LEVEL=4
cmake --build "$root/build-ha-static-arm64" -j4
