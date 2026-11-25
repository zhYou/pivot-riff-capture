#!/usr/bin/env bash
# 在 GitHub Actions 的 macos-latest（iOS 模拟器）上捕获 riff Flutter 测试 App 的
# Capture v1 原生样本。
#
# 每个样本集启动一个全新的 capture_proxy.py：
#   ios-gzip-install  1.0.0 gzip 首次安装
#   ios-gzip-update   1.1.0 gzip 覆盖安装
#   ios-none-install  1.0.0 未压缩首次安装
#   ios-none-update   1.1.0 未压缩覆盖安装
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BUNDLE="com.example.riff.stub.ios"
HOST="http://127.0.0.1:18080"
PORT=18080
mkdir -p samples evidence

echo "== Xcode 与模拟器信息 =="
xcodebuild -version | tee evidence/xcode-version.txt
xcrun simctl list runtimes | tee evidence/ios-simulator-runtimes.txt

# 选一台可用的 iPhone 模拟器。
DEVICE="$(
  python3 - <<'PY'
import json
import subprocess

data = json.loads(
    subprocess.check_output(["xcrun", "simctl", "list", "devices", "available", "-j"])
)
for runtime, devices in sorted(data.get("devices", {}).items()):
    if "iOS" not in runtime:
        continue
    for device in devices:
        if device.get("isAvailable") and device.get("name", "").startswith("iPhone"):
            print(device["udid"])
            raise SystemExit(0)
raise SystemExit(1)
PY
)"
if [ -z "${DEVICE:-}" ]; then
  echo "找不到可用的 iPhone 模拟器" >&2
  xcrun simctl list devices available >&2
  exit 1
fi
echo "使用模拟器 $DEVICE" | tee evidence/ios-simulator-device.txt
xcrun simctl boot "$DEVICE" >/dev/null 2>&1 || true
xcrun simctl bootstatus "$DEVICE" -b || true

flutter config --enable-swift-package-manager >/dev/null 2>&1 || true
flutter precache --ios

build_app() {
  local name="$1" version="$2" build="$3" compression="$4"
  echo "== 构建 $name（version=$version build=$build compression=$compression）=="
  flutter build ios --simulator --debug \
    --build-name="$version" --build-number="$build" \
    --dart-define=CAPTURE_HOST="$HOST" \
    --dart-define=COMPRESSION="$compression" || return 1
  rm -rf "/tmp/probe-$name.app"
  cp -R build/ios/iphonesimulator/Runner.app "/tmp/probe-$name.app"
  find ios -name Package.resolved -print -exec cp {} "evidence/Package-$name.resolved" \; 2>/dev/null || true
}

wait_for_port() {
  local i
  for i in $(seq 1 60); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
      exec 3>&-
      return 0
    fi
    sleep 1
  done
  return 1
}

run_capture() {
  local label="$1" app="$2" transport="$3" fresh="$4"
  local log="samples/$label.proxy.log"
  echo "== 捕获 $label（transport=$transport fresh=$fresh）=="
  if [ "$fresh" = "fresh" ]; then
    xcrun simctl uninstall "$DEVICE" "$BUNDLE" >/dev/null 2>&1 || true
  fi
  python3 capture_proxy.py \
    --out-dir "samples/$label" \
    --platform ios \
    --transport "$transport" \
    --sample-set "$label" \
    --port "$PORT" \
    --duration 150 \
    --require-backgrounded \
    --fail-on-assertion >"$log" 2>&1 &
  local proxy_pid=$!
  if ! wait_for_port; then
    echo "抓包代理未在 60 秒内监听 $PORT" >&2
    kill -TERM "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
    return 1
  fi

  if ! xcrun simctl install "$DEVICE" "$app"; then
    echo "安装 $app 失败" >&2
    kill -TERM "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
    return 1
  fi
  xcrun simctl launch "$DEVICE" "$BUNDLE"
  # App 内的确定性序列约 50 秒；留足重试窗口。
  sleep 75
  # 打开系统设置让 probe 进入后台，触发 Application Backgrounded。
  xcrun simctl launch "$DEVICE" com.apple.Preferences >/dev/null 2>&1 || true
  sleep 15
  xcrun simctl terminate "$DEVICE" "$BUNDLE" >/dev/null 2>&1 || true
  kill -TERM "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid"
  local status=$?
  cat "$log"
  return $status
}

failures=0
build_app gzip-1.0.0 1.0.0 1 gzip || failures=1
build_app gzip-1.1.0 1.1.0 2 gzip || failures=1
build_app none-1.0.0 1.0.0 1 none || failures=1
build_app none-1.1.0 1.1.0 2 none || failures=1

if [ "$failures" -eq 0 ]; then
  run_capture ios-gzip-install /tmp/probe-gzip-1.0.0.app gzip fresh || failures=1
  run_capture ios-gzip-update /tmp/probe-gzip-1.1.0.app gzip keep || failures=1
  xcrun simctl uninstall "$DEVICE" "$BUNDLE" >/dev/null 2>&1 || true
  run_capture ios-none-install /tmp/probe-none-1.0.0.app none fresh || failures=1
  run_capture ios-none-update /tmp/probe-none-1.1.0.app none keep || failures=1
fi

echo "== 记录 posthog-ios 实际解析版本 =="
find ios -name Package.resolved -print -exec tee evidence/ios-package-resolved.json \; 2>/dev/null || true

exit "$failures"
