#!/usr/bin/env python3
"""riff Flutter 测试 App 的本地抓包代理。

代理只在本机或 CI 的模拟器网络中监听，对 `POST /i/v1/analytics/events`
执行三件事：

1. 原样保存请求（gzip 或未压缩 JSON）、请求头（`Authorization` 脱敏）、
   解压内容及其 SHA-256；
2. 对同一批里的两个业务事件分别返回 `ok` 和 `retry`，其余事件返回 `ok`；
   重试请求到达时返回 `ok`，并断言“只重发 retry 的 UUID、保留请求 ID、
   递增尝试次数、更新发送时间、批次 `created_at` 与事件内容不变”；
3. 写 `manifest.json`，标明 `evidenceKind: "test-app"`，供
   `scripts/riff-replay-samples.py` 独立重放。

用法见 test/fixtures/riff-flutter-app/README.md。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

CAPTURE_PATH = "/i/v1/analytics/events"
BUSINESS_EVENTS = ("onboarding_login_entry", "onboarding_login_result")
LIFECYCLE_EVENTS = (
    "Application Installed",
    "Application Updated",
    "Application Opened",
    "Application Backgrounded",
)
DEFAULT_NAMESPACE = {
    "android": "com.example.riff.stub.android",
    "ios": "com.example.riff.stub.ios",
}
MAX_BODY_BYTES = 20 * 1024 * 1024
REDACTED = "<redacted>"
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def parse_iso8601(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """小写化并脱敏；只保留可复核的协议头。"""
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        redacted[lowered] = REDACTED if lowered in SENSITIVE_HEADERS else value
    return redacted


class ProxyResponse:
    def __init__(self, status: int, headers: dict[str, str], body: dict[str, Any]):
        self.status = status
        self.headers = headers
        self.body = body


def request_error(status: int, code: str, description: str) -> ProxyResponse:
    return ProxyResponse(
        status,
        {"Content-Type": "application/json"},
        {
            "error": code,
            "error_description": description,
            "error_uri": "https://posthog.com/docs/api/capture-v1#limits-and-validation",
        },
    )


def _event_identity(event: Mapping[str, Any]) -> str:
    return str(event.get("uuid", "")).strip().lower()


def _event_summary(event: Mapping[str, Any], request_index: int) -> dict[str, Any]:
    properties = event.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    return {
        "requestIndex": request_index,
        "name": event.get("event"),
        "uuid": _event_identity(event),
        "distinctId": event.get("distinct_id"),
        "timestamp": event.get("timestamp"),
        "propertyTypes": {key: json_type_name(value) for key, value in properties.items()},
        "appNamespace": properties.get("$app_namespace"),
        "appVersion": properties.get("$app_version"),
        "appBuild": properties.get("$app_build"),
    }


class CaptureRecorder:
    """协议状态机：输入请求头与原始字节，输出响应与落盘文件。"""

    def __init__(
        self,
        out_dir: Path,
        platform: str,
        transport: str,
        expected_namespace: str | None,
        sample_set: str,
        require_backgrounded: bool = False,
        require_identity: bool = True,
        business_events: tuple[str, ...] = BUSINESS_EVENTS,
    ):
        self.out_dir = Path(out_dir)
        self.platform = platform
        self.transport = transport
        self.expected_namespace = expected_namespace or DEFAULT_NAMESPACE.get(platform)
        self.sample_set = sample_set
        self.require_backgrounded = require_backgrounded
        self.require_identity = require_identity
        self.business_events = tuple(business_events)

        self.requests: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.raw_events: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")

        # 逐条回执状态：第二个业务 UUID 只被标记 retry 一次，随后 ack。
        self.business_retry_uuid: str | None = None
        self.pending_retry: dict[str, dict[str, Any]] = {}
        self.retry_acked = False
        self.business_request_index: int | None = None
        self.business_uuids: list[str] = []
        self.retry_request_index: int | None = None
        self.retry_request_uuids: list[str] = []

        for directory in ("http", "body", "decoded"):
            (self.out_dir / directory).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 入口
    def handle(
        self,
        headers: Mapping[str, str],
        raw: bytes,
        path: str = CAPTURE_PATH,
    ) -> ProxyResponse:
        index = len(self.requests) + 1
        recorded_headers = redact_headers(headers)
        content_encoding = (recorded_headers.get("content-encoding") or "").strip().lower()
        record: dict[str, Any] = {
            "index": index,
            "method": "POST",
            "path": path,
            "headers": recorded_headers,
            "contentEncoding": content_encoding or "none",
            "authorizationPresent": "authorization" in recorded_headers,
            "bodyBytes": len(raw),
            "bodySha256": sha256_hex(raw),
            "receivedAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        }

        if len(raw) > MAX_BODY_BYTES:
            record["error"] = "request_too_large"
            self.requests.append(record)
            self._write_request_record(index, record)
            return request_error(413, "request_too_large", f"请求体超过 {MAX_BODY_BYTES} 字节")

        try:
            decoded = self._decode(content_encoding, raw)
        except ValueError as error:
            record["error"] = "invalid_content_encoding"
            record["errorDescription"] = str(error)
            self.requests.append(record)
            self._write_request_record(index, record)
            status, code = (415, "unsupported_encoding") if "不支持" in str(error) else (400, "request_parsing_error")
            return request_error(status, code, str(error))

        try:
            payload = json.loads(decoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            record["error"] = "request_parsing_error"
            record["errorDescription"] = str(error)
            self.requests.append(record)
            self._write_request_record(index, record)
            return request_error(400, "request_parsing_error", f"请求体不是合法 JSON：{error}")

        batch = payload.get("batch") if isinstance(payload, dict) else None
        created_at = payload.get("created_at") if isinstance(payload, dict) else None
        if not isinstance(batch, list) or not batch:
            record["error"] = "empty_batch"
            self.requests.append(record)
            self._write_request_record(index, record)
            return request_error(400, "empty_batch", "batch 必须是非空数组")
        if not isinstance(created_at, str) or not created_at:
            record["error"] = "invalid_batch"
            self.requests.append(record)
            self._write_request_record(index, record)
            return request_error(400, "invalid_batch", "created_at 缺失或不是字符串")

        body_suffix = ".json.gz" if record["contentEncoding"] == "gzip" else ".json"
        body_name = f"body/{index:04d}{body_suffix}"
        decoded_name = f"decoded/{index:04d}.json"
        (self.out_dir / body_name).write_bytes(raw)
        (self.out_dir / decoded_name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        record["bodyFile"] = body_name
        record["decodedFile"] = decoded_name
        record["decodedSha256"] = sha256_hex(decoded)
        record["decodedBytes"] = len(decoded)
        record["createdAt"] = created_at
        record["gzipMagicOk"] = record["contentEncoding"] != "gzip" or raw[:2] == b"\x1f\x8b"

        results, request_events = self._decide(batch, index)
        response_headers = {"Content-Type": "application/json"}
        request_id = recorded_headers.get("posthog-request-id")
        if request_id:
            response_headers["PostHog-Request-Id"] = request_id
        record["events"] = request_events
        record["response"] = {"status": 200, "results": results, "headers": response_headers}
        self.requests.append(record)
        self._write_request_record(index, record)
        return ProxyResponse(200, response_headers, {"results": results})

    def _write_request_record(self, index: int, record: dict[str, Any]) -> None:
        (self.out_dir / "http" / f"{index:04d}.request.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # ------------------------------------------------------------------ 解码
    def _decode(self, content_encoding: str, raw: bytes) -> bytes:
        if content_encoding in ("", "none", "identity"):
            return raw
        if content_encoding == "gzip":
            if raw[:2] != b"\x1f\x8b":
                raise ValueError("Content-Encoding: gzip 但请求体没有 gzip 魔数")
            try:
                return gzip.decompress(raw)
            except OSError as error:
                raise ValueError(f"gzip 解压失败：{error}") from error
        raise ValueError(f"不支持 Content-Encoding: {content_encoding}")

    # ------------------------------------------------------------------ 回执
    def _decide(
        self,
        batch: list[dict[str, Any]],
        request_index: int,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        unique: list[dict[str, Any]] = []
        duplicates: dict[str, int] = {}
        for event in batch:
            if not isinstance(event, dict):
                self.warnings.append(f"请求 {request_index} 含非对象事件，已跳过")
                continue
            uuid = _event_identity(event)
            if not uuid:
                self.warnings.append(f"请求 {request_index} 含缺少 uuid 的事件，已跳过")
                continue
            if uuid in { _event_identity(item) for item in unique }:
                duplicates[uuid] = duplicates.get(uuid, 0) + 1
                continue
            unique.append(event)
            summary = _event_summary(event, request_index)
            self.events.append(summary)
            self.raw_events.append({"summary": summary, "event": event})

        uuid_set = [_event_identity(event) for event in unique]
        is_retry = any(uuid in self.pending_retry for uuid in uuid_set)
        if is_retry and self.retry_request_index is None:
            self.retry_request_index = request_index
            self.retry_request_uuids = uuid_set

        business_in_batch = [
            _event_identity(event)
            for event in unique
            if event.get("event") in self.business_events
        ]
        if (
            self.business_retry_uuid is None
            and len(business_in_batch) >= 2
            and not is_retry
        ):
            self.business_retry_uuid = business_in_batch[1]
        if (
            self.business_request_index is None
            and self.business_retry_uuid is not None
            and self.business_retry_uuid in uuid_set
        ):
            self.business_request_index = request_index
            self.business_uuids = list(dict.fromkeys(business_in_batch))

        results: dict[str, dict[str, Any]] = {}
        for event in unique:
            uuid = _event_identity(event)
            entry: dict[str, Any]
            if uuid == self.business_retry_uuid and uuid not in self.pending_retry and not self.retry_acked:
                self.pending_retry[uuid] = event
                entry = {"result": "retry", "details": "not_persisted"}
            elif uuid in self.pending_retry:
                del self.pending_retry[uuid]
                if uuid == self.business_retry_uuid:
                    self.retry_acked = True
                entry = {"result": "ok"}
            else:
                entry = {"result": "ok"}
            if duplicates.get(uuid):
                entry["duplicates_dropped"] = duplicates[uuid]
            results[uuid] = entry
        return results, [_event_summary(event, request_index) for event in unique]

    # ---------------------------------------------------------------- manifest
    def build_manifest(self) -> dict[str, Any]:
        assertions = self._assertions()
        required_failed = [
            item["name"] for item in assertions if item["required"] and not item["ok"]
        ]
        optional_failed = [
            item["name"] for item in assertions if not item["required"] and not item["ok"]
        ]
        event_names = list(dict.fromkeys(item["name"] for item in self.events))
        lifecycle_observed = [
            name for name in LIFECYCLE_EVENTS if name in event_names
        ]
        identity = {
            "identify": sum(1 for item in self.events if item["name"] == "$identify"),
            "set": sum(1 for item in self.events if item["name"] == "$set"),
            "set_once": sum(1 for item in self.events if item["name"] == "$set_once"),
        }
        return {
            "schemaVersion": 1,
            "evidenceKind": "test-app",
            "sampleSet": self.sample_set,
            "platform": self.platform,
            "transport": self.transport,
            "expectedNamespace": self.expected_namespace,
            "startedAt": self.started_at,
            "finishedAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "requestCount": len(self.requests),
            "requests": self.requests,
            "events": self.events,
            "eventNames": event_names,
            "lifecycleEvents": lifecycle_observed,
            "identityEvents": identity,
            "businessEvents": {
                name: sum(1 for item in self.events if item["name"] == name)
                for name in self.business_events
            },
            "businessRequest": self._request_digest(self.business_request_index),
            "retryRequest": self._request_digest(self.retry_request_index),
            "assertions": assertions,
            "requiredFailed": required_failed,
            "optionalFailed": optional_failed,
            "ok": not required_failed,
            "warnings": self.warnings,
        }

    def _request_digest(self, index: int | None) -> dict[str, Any] | None:
        if index is None:
            return None
        record = self.requests[index - 1]
        return {
            "index": record["index"],
            "requestId": record["headers"].get("posthog-request-id"),
            "attempt": record["headers"].get("posthog-attempt"),
            "requestTimestamp": record["headers"].get("posthog-request-timestamp"),
            "createdAt": record.get("createdAt"),
            "uuids": [item["uuid"] for item in record.get("events", [])],
            "eventNames": [item["name"] for item in record.get("events", [])],
        }

    def _assertions(self) -> list[dict[str, Any]]:
        assertions: list[dict[str, Any]] = []

        def add(name: str, ok: bool, details: str, required: bool = True) -> None:
            assertions.append(
                {"name": name, "ok": bool(ok), "required": required, "details": details}
            )

        business_request = self._request_digest(self.business_request_index)
        retry_request = self._request_digest(self.retry_request_index)
        business_uuid_set = set(self.business_uuids)

        add(
            "business_uuids_share_batch",
            business_request is not None and len(self.business_uuids) >= 2,
            f"两个业务事件同一批：{self.business_uuids}",
        )
        retry_expected = {self.business_retry_uuid} if self.business_retry_uuid else set()
        add(
            "retry_only_requested_uuid",
            retry_request is not None and set(retry_request["uuids"]) == retry_expected,
            f"重试请求 UUID {retry_request['uuids'] if retry_request else None}，预期 {sorted(retry_expected)}",
        )
        add(
            "request_id_preserved",
            bool(
                business_request
                and retry_request
                and business_request["requestId"]
                and business_request["requestId"] == retry_request["requestId"]
            ),
            f"首次 {business_request['requestId'] if business_request else None} → "
            f"重试 {retry_request['requestId'] if retry_request else None}",
        )
        attempt_ok = False
        if business_request and retry_request:
            try:
                attempt_ok = int(retry_request["attempt"]) == int(business_request["attempt"]) + 1
            except (TypeError, ValueError):
                attempt_ok = False
        add(
            "attempt_incremented",
            attempt_ok,
            f"首次 {business_request['attempt'] if business_request else None} → "
            f"重试 {retry_request['attempt'] if retry_request else None}",
        )
        first_ts = parse_iso8601(business_request["requestTimestamp"]) if business_request else None
        retry_ts = parse_iso8601(retry_request["requestTimestamp"]) if retry_request else None
        add(
            "request_timestamp_updated",
            bool(first_ts and retry_ts and retry_ts >= first_ts),
            f"首次 {business_request['requestTimestamp'] if business_request else None} → "
            f"重试 {retry_request['requestTimestamp'] if retry_request else None}",
        )
        add(
            "created_at_preserved",
            bool(
                business_request
                and retry_request
                and business_request["createdAt"]
                and business_request["createdAt"] == retry_request["createdAt"]
            ),
            f"首次 {business_request['createdAt'] if business_request else None} → "
            f"重试 {retry_request['createdAt'] if retry_request else None}",
        )
        add(
            "event_payload_preserved",
            self._retry_payload_preserved(),
            "重试 UUID 的事件名、时间戳、主体与属性与首次一致",
        )
        retry_results = (
            self.requests[self.retry_request_index - 1]["response"]["results"]
            if self.retry_request_index
            else {}
        )
        add(
            "retried_uuid_acked",
            bool(retry_results)
            and all(entry.get("result") == "ok" for entry in retry_results.values()),
            f"重试响应 {retry_results}",
        )

        gzip_records = [record for record in self.requests if record.get("response", {}).get("status") == 200]
        if self.transport == "gzip":
            add(
                "gzip_transport",
                bool(gzip_records)
                and all(
                    record["contentEncoding"] == "gzip" and record.get("gzipMagicOk")
                    for record in gzip_records
                ),
                "每个成功请求都带 Content-Encoding: gzip 且以 gzip 魔数开头",
            )
        else:
            add(
                "uncompressed_transport",
                bool(gzip_records)
                and all(record["contentEncoding"] in ("", "none", "identity") for record in gzip_records),
                f"Content-Encoding 取值 {[record['contentEncoding'] for record in gzip_records]}",
            )

        namespaces = {
            item["appNamespace"] for item in self.events if item["appNamespace"] is not None
        }
        add(
            "app_namespace_matches",
            bool(self.events)
            and bool(namespaces)
            and namespaces == {self.expected_namespace},
            f"观察到 {sorted(namespaces)}，预期 {self.expected_namespace}",
        )
        add(
            "authorization_redacted",
            all(
                record["headers"].get("authorization") == REDACTED
                for record in self.requests
                if record.get("authorizationPresent")
            )
            and any(record.get("authorizationPresent") for record in self.requests),
            "Authorization 在记录中一律为 <redacted>",
        )
        add(
            "backgrounded_observed",
            "Application Backgrounded" in self.lifecycle_events(),
            f"生命周期事件 {self.lifecycle_events()}",
            required=self.require_backgrounded,
        )
        identify = sum(1 for item in self.events if item["name"] == "$identify")
        set_events = sum(1 for item in self.events if item["name"] == "$set")
        set_once_events = sum(1 for item in self.events if item["name"] == "$set_once")
        has_set = set_events > 0 or self._identify_property_count("$set") > 0
        has_set_once = set_once_events > 0 or self._identify_property_count("$set_once") > 0
        add(
            "identity_observed",
            identify > 0 and has_set and has_set_once,
            f"$identify={identify}，$set={set_events}，$set_once={set_once_events}，"
            "并核对 $identify.properties 中的 $set/$set_once",
            required=self.require_identity,
        )
        add(
            "business_events_observed",
            all(
                any(item["name"] == name for item in self.events)
                for name in self.business_events
            ),
            f"业务事件 {self.business_events}",
        )
        return assertions

    def lifecycle_events(self) -> list[str]:
        names = [item["name"] for item in self.events]
        return [name for name in LIFECYCLE_EVENTS if name in names]

    def _identify_property_count(self, key: str) -> int:
        count = 0
        for item in self.raw_events:
            event = item["event"]
            if event.get("event") != "$identify":
                continue
            properties = event.get("properties")
            if isinstance(properties, dict) and isinstance(properties.get(key), dict):
                count += 1
        return count

    def _retry_payload_preserved(self) -> bool:
        if not self.business_retry_uuid or self.retry_request_index is None:
            return False
        first = None
        for item in self.raw_events:
            if item["summary"]["uuid"] == self.business_retry_uuid:
                first = item["event"]
                break
        if first is None:
            return False
        retry = None
        for item in self.raw_events:
            if (
                item["summary"]["uuid"] == self.business_retry_uuid
                and item["summary"]["requestIndex"] == self.retry_request_index
            ):
                retry = item["event"]
                break
        if retry is None:
            return False
        return (
            first.get("event") == retry.get("event")
            and first.get("distinct_id") == retry.get("distinct_id")
            and first.get("timestamp") == retry.get("timestamp")
            and first.get("properties") == retry.get("properties")
        )


class CaptureProxy:
    """把 CaptureRecorder 包装成可启停的 HTTP server。"""

    def __init__(
        self,
        out_dir: Path,
        platform: str,
        transport: str,
        expected_namespace: str | None = None,
        sample_set: str = "",
        require_backgrounded: bool = False,
        require_identity: bool = True,
    ):
        self.recorder = CaptureRecorder(
            out_dir=out_dir,
            platform=platform,
            transport=transport,
            expected_namespace=expected_namespace,
            sample_set=sample_set or f"{platform}-{transport}",
            require_backgrounded=require_backgrounded,
            require_identity=require_identity,
        )
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, host: str = "127.0.0.1", port: int = 8000) -> None:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib 命名
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                headers = {key: value for key, value in self.headers.items()}
                response = proxy.recorder.handle(headers, raw, path=self.path)
                body = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                # 静默：请求事实写入 manifest，stdout 留给 CI 阶段标记。
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def write_manifest(self) -> dict[str, Any]:
        manifest = self.recorder.build_manifest()
        (self.recorder.out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return manifest


def post_to_proxy(
    url: str,
    headers: Mapping[str, str],
    raw: bytes,
    timeout: float = 30,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    request = urllib.request.Request(url, data=raw, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                body,
            )
    except urllib.error.HTTPError as error:
        body = json.loads(error.read().decode("utf-8"))
        return (
            error.code,
            {key.lower(): value for key, value in error.headers.items()},
            body,
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="riff Capture v1 抓包代理")
    parser.add_argument("--out-dir", required=True, help="样本输出目录")
    parser.add_argument("--platform", required=True, choices=["android", "ios"])
    parser.add_argument("--transport", required=True, choices=["gzip", "none"])
    parser.add_argument("--sample-set", default="", help="样本集名称，默认 <platform>-<transport>")
    parser.add_argument("--expected-namespace", default=None, help="预期的 $app_namespace")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--duration", type=int, default=180, help="最长运行秒数")
    parser.add_argument("--require-backgrounded", action="store_true")
    parser.add_argument("--no-require-identity", dest="require_identity", action="store_false")
    parser.add_argument(
        "--fail-on-assertion",
        action="store_true",
        help="必需断言失败时以非零码退出（CI 使用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    sample_set = args.sample_set or f"{args.platform}-{args.transport}"
    proxy = CaptureProxy(
        out_dir=Path(args.out_dir),
        platform=args.platform,
        transport=args.transport,
        expected_namespace=args.expected_namespace,
        sample_set=sample_set,
        require_backgrounded=args.require_backgrounded,
        require_identity=args.require_identity,
    )
    proxy.start(args.host, args.port)
    print(f"RIFF_CAPTURE_PROXY listening on {args.host}:{proxy.port} -> {args.out_dir}", flush=True)

    stop = threading.Event()

    def handle_signal(_signum: int, _frame: Any) -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle_signal)

    deadline = time.monotonic() + args.duration
    while not stop.is_set() and time.monotonic() < deadline:
        stop.wait(timeout=0.5)
    proxy.stop()
    manifest = proxy.write_manifest()
    print(
        f"RIFF_CAPTURE_PROXY manifest={args.out_dir}/manifest.json "
        f"requests={manifest['requestCount']} ok={manifest['ok']} "
        f"requiredFailed={manifest['requiredFailed']} optionalFailed={manifest['optionalFailed']}",
        flush=True,
    )
    if args.fail_on_assertion and not manifest["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
