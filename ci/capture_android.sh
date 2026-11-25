#!/usr/bin/env bash
# 在 GitHub Actions 的 Android 模拟器（KVM）上捕获 riff Flutter 测试 App 的
# Capture v1 原生样本。
#
# 每个样本集启动一个全新的 capture_proxy.py：
#   android-gzip-install  1.0.0 gzip 首次安装
#   android-gzip-update   1.1.0 gzip 覆盖安装
#   android-none-install  1.0.0 未压缩首次安装
#   android-none-update   1.1.0 未压缩覆盖安装
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PACKAGE="com.example.riff.stub.android"
HOST="http://10.0.2.2:18080"
PORT=18080
mkdir -p samples evidence

echo "== Android 设备信息 =="
adb devices
{
  echo "ro.build.version.release=$(adb shell getprop ro.build.version.release | tr -d '\r')"
  echo "ro.build.version.sdk=$(adb shell getprop ro.build.version.sdk | tr -d '\r')"
  echo "ro.product.cpu.abi=$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
  echo "ro.product.model=$(adb shell getprop ro.product.model | tr -d '\r')"
} | tee evidence/android-device.txt

build_apk() {
  local name="$1" version="$2" build="$3" compression="$4"
  echo "== 构建 $name（version=$version build=$build compression=$compression）=="
  flutter build apk --debug \
    --build-name="$version" --build-number="$build" \
    --dart-define=CAPTURE_HOST="$HOST" \
    --dart-define=COMPRESSION="$compression" || return 1
  cp build/app/outputs/flutter-apk/app-debug.apk "/tmp/probe-$name.apk"
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
  local label="$1" apk="$2" transport="$3" fresh="$4"
  local log="samples/$label.proxy.log"
  echo "== 捕获 $label（transport=$transport fresh=$fresh）=="
  if [ "$fresh" = "fresh" ]; then
    adb uninstall "$PACKAGE" >/dev/null 2>&1 || true
  fi
  python3 capture_proxy.py \
    --out-dir "samples/$label" \
    --platform android \
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

  if ! adb install -r "$apk" >/dev/null; then
    echo "安装 $apk 失败" >&2
    kill -TERM "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
    return 1
  fi
  adb shell am start -W -n "$PACKAGE/.MainActivity" | tail -5
  # App 内的确定性序列约 50 秒；留足重试窗口。
  sleep 75
  adb shell input keyevent KEYCODE_HOME
  sleep 15
  adb shell am force-stop "$PACKAGE" >/dev/null 2>&1 || true
  kill -TERM "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid"
  local status=$?
  cat "$log"
  return $status
}

failures=0
build_apk gzip-1.0.0 1.0.0 1 gzip || failures=1
build_apk gzip-1.1.0 1.1.0 2 gzip || failures=1
build_apk none-1.0.0 1.0.0 1 none || failures=1
build_apk none-1.1.0 1.1.0 2 none || failures=1

if [ "$failures" -eq 0 ]; then
  run_capture android-gzip-install /tmp/probe-gzip-1.0.0.apk gzip fresh || failures=1
  run_capture android-gzip-update /tmp/probe-gzip-1.1.0.apk gzip keep || failures=1
  adb uninstall "$PACKAGE" >/dev/null 2>&1 || true
  run_capture android-none-install /tmp/probe-none-1.0.0.apk none fresh || failures=1
  run_capture android-none-update /tmp/probe-none-1.1.0.apk none keep || failures=1
fi

echo "== 记录 posthog-android 实际解析版本 =="
(
  cd android
  ./gradlew -q app:dependencies --configuration debugRuntimeClasspath 2>/dev/null \
    | grep -i "com\.posthog" | sort -u | head -20
) | tee evidence/android-posthog-dependencies.txt

exit "$failures"
