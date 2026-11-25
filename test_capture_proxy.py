"""capture_proxy 的单元测试（在伪 HTTP 层之上驱动，不依赖 Android/iOS）。

测试从“抓包记录器”这个 seam 驱动：输入是 HTTP 头与原始字节，输出是状态码、
响应头、响应体以及落盘文件和 manifest。HTTP server 只做薄封装，另有单独测试。
"""

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import capture_proxy

CREATED_AT = "2026-11-25T01:01:02.000Z"
NS = "com.example.riff.stub.android"


def event(name, uuid, distinct_id="stub-a", timestamp="2026-11-25T01:01:00.000Z", **properties):
    props = {
        "$lib": "posthog-flutter",
        "$lib_version": "6.0.0",
        "$app_namespace": NS,
        "$app_version": "1.0.0",
        "$app_build": "1",
        "user_generation": 1,
    }
    props.update(properties)
    return {
        "event": name,
        "uuid": uuid,
        "distinct_id": distinct_id,
        "timestamp": timestamp,
        "properties": props,
    }


def batch(events, created_at=CREATED_AT):
    return {"created_at": created_at, "batch": events}


def headers_for(request_id, attempt, timestamp, content_encoding="gzip", authorization="Bearer synthetic-token"):
    headers = {
        "Content-Type": "application/json",
        "Authorization": authorization,
        "PostHog-Sdk-Info": "posthog-flutter/6.0.0",
        "PostHog-Request-Id": request_id,
        "PostHog-Attempt": str(attempt),
        "PostHog-Request-Timestamp": timestamp,
    }
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    return headers


LIFECYCLE = event("Application Opened", "01a21038-60e0-7000-8000-000000000001")
BUSINESS_ONE = event(
    "onboarding_login_entry",
    "01a21038-61a8-7000-8000-000000000002",
    action="selected",
    method="guest",
    has_previous_account=False,
    terms_accepted=True,
)
BUSINESS_TWO = event(
    "onboarding_login_result",
    "01a21038-6270-7000-8000-000000000003",
    oauthType="guest",
    is_new_register=True,
    result="entered_home",
)


class CaptureRecorderTest(unittest.TestCase):
    def make_recorder(self, transport="gzip", namespace=NS, platform="android"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recorder = capture_proxy.CaptureRecorder(
            out_dir=Path(tmp.name),
            platform=platform,
            transport=transport,
            expected_namespace=namespace,
            sample_set=f"{platform}-{transport}",
            require_identity=False,
        )
        return recorder, Path(tmp.name)

    def send(self, recorder, payload, request_id="rid-1", attempt=1, timestamp="2026-11-25T01:01:03.000Z", encoding="gzip"):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        if encoding == "gzip":
            raw = gzip.compress(raw)
        elif encoding is not None:
            raise AssertionError(f"未知编码 {encoding}")
        return recorder.handle(headers_for(request_id, attempt, timestamp, encoding), raw)

    def test_gzip_批次保存原始字节并注入_ok_retry(self):
        recorder, out = self.make_recorder()
        response = self.send(recorder, batch([LIFECYCLE, BUSINESS_ONE, BUSINESS_TWO]))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["PostHog-Request-Id"], "rid-1")
        results = response.body["results"]
        self.assertEqual(results[BUSINESS_ONE["uuid"]], {"result": "ok"})
        self.assertEqual(results[BUSINESS_TWO["uuid"]], {"result": "retry", "details": "not_persisted"})
        self.assertEqual(results[LIFECYCLE["uuid"]], {"result": "ok"})

        record = recorder.requests[0]
        body_path = out / record["bodyFile"]
        self.assertTrue(body_path.name.endswith(".json.gz"))
        raw = body_path.read_bytes()
        self.assertEqual(raw[:2], b"\x1f\x8b")
        self.assertEqual(record["bodySha256"], hashlib.sha256(raw).hexdigest())
        decoded = json.loads((out / record["decodedFile"]).read_text())
        self.assertEqual(len(decoded["batch"]), 3)
        self.assertEqual(record["contentEncoding"], "gzip")

    def test_授权头在记录中脱敏(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE]))
        record = recorder.requests[0]
        self.assertEqual(record["headers"]["authorization"], "<redacted>")
        self.assertNotIn("synthetic-token", json.dumps(record))

    def test_重试请求只含待重试_uuid_时全部_ok_且断言通过(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE, BUSINESS_ONE, BUSINESS_TWO]))
        response = self.send(
            recorder,
            batch([BUSINESS_TWO]),
            attempt=2,
            timestamp="2026-11-25T01:01:08.000Z",
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, {"results": {BUSINESS_TWO["uuid"]: {"result": "ok"}}})

        manifest = recorder.build_manifest()
        self.assertTrue(manifest["ok"], manifest["assertions"])
        names = {item["name"] for item in manifest["assertions"] if item["ok"]}
        expected = {
            "business_uuids_share_batch",
            "retry_only_requested_uuid",
            "request_id_preserved",
            "attempt_incremented",
            "request_timestamp_updated",
            "created_at_preserved",
            "event_payload_preserved",
            "retried_uuid_acked",
            "gzip_transport",
            "app_namespace_matches",
            "authorization_redacted",
        }
        self.assertTrue(expected.issubset(names), expected - names)

    def test_重试请求携带额外_uuid_时断言失败(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE, BUSINESS_ONE, BUSINESS_TWO]))
        self.send(recorder, batch([BUSINESS_ONE, BUSINESS_TWO]), attempt=2)
        manifest = recorder.build_manifest()
        self.assertFalse(manifest["ok"])
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"]}
        self.assertIn("retry_only_requested_uuid", failed)

    def test_重试请求未保留请求_id_时断言失败(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE, BUSINESS_ONE, BUSINESS_TWO]))
        self.send(recorder, batch([BUSINESS_TWO]), request_id="rid-2", attempt=2)
        manifest = recorder.build_manifest()
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"]}
        self.assertIn("request_id_preserved", failed)

    def test_重试请求未递增尝试次数时断言失败(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE, BUSINESS_ONE, BUSINESS_TWO]))
        self.send(recorder, batch([BUSINESS_TWO]), attempt=1)
        manifest = recorder.build_manifest()
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"]}
        self.assertIn("attempt_incremented", failed)

    def test_重试请求变更_created_at_时断言失败(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE, BUSINESS_ONE, BUSINESS_TWO]))
        self.send(recorder, batch([BUSINESS_TWO], created_at="2026-11-25T01:02:00.000Z"), attempt=2)
        manifest = recorder.build_manifest()
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"]}
        self.assertIn("created_at_preserved", failed)

    def test_重试请求变更事件内容时断言失败(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE, BUSINESS_ONE, BUSINESS_TWO]))
        changed = json.loads(json.dumps(BUSINESS_TWO))
        changed["properties"]["result"] = "failed"
        self.send(recorder, batch([changed]), attempt=2)
        manifest = recorder.build_manifest()
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"]}
        self.assertIn("event_payload_preserved", failed)

    def test_未压缩请求不写_gzip_文件(self):
        recorder, out = self.make_recorder(transport="none")
        response = self.send(recorder, batch([LIFECYCLE]), encoding=None)
        self.assertEqual(response.status, 200)
        record = recorder.requests[0]
        self.assertEqual(record["contentEncoding"], "none")
        self.assertTrue((out / record["bodyFile"]).name.endswith(".json"))
        manifest = recorder.build_manifest()
        matched = {item["name"]: item["ok"] for item in manifest["assertions"]}
        self.assertTrue(matched["uncompressed_transport"])

    def test_未压缩传输收到_gzip_时断言失败(self):
        recorder, _ = self.make_recorder(transport="none")
        self.send(recorder, batch([LIFECYCLE]), encoding="gzip")
        manifest = recorder.build_manifest()
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"]}
        self.assertIn("uncompressed_transport", failed)

    def test_gzip_传输收到未压缩请求时断言失败(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE]), encoding=None)
        manifest = recorder.build_manifest()
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"]}
        self.assertIn("gzip_transport", failed)

    def test_命名空间不符时断言失败(self):
        recorder, _ = self.make_recorder(namespace="com.example.riff.stub.android")
        other = event("Application Opened", "01a21038-60e0-7000-8000-0000000000aa", **{"$app_namespace": "com.example.riff.stub.ios"})
        self.send(recorder, batch([other]))
        manifest = recorder.build_manifest()
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"]}
        self.assertIn("app_namespace_matches", failed)

    def test_manifest_记录事件名与属性类型(self):
        recorder, _ = self.make_recorder()
        self.send(recorder, batch([LIFECYCLE, BUSINESS_ONE, BUSINESS_TWO]))
        manifest = recorder.build_manifest()
        self.assertEqual(manifest["evidenceKind"], "test-app")
        self.assertEqual(manifest["platform"], "android")
        self.assertEqual(manifest["transport"], "gzip")
        self.assertIn("Application Opened", manifest["eventNames"])
        self.assertIn("onboarding_login_entry", manifest["eventNames"])
        entry = next(item for item in manifest["events"] if item["name"] == "onboarding_login_entry")
        self.assertEqual(entry["propertyTypes"]["action"], "string")
        self.assertEqual(entry["propertyTypes"]["has_previous_account"], "boolean")
        self.assertEqual(entry["propertyTypes"]["user_generation"], "integer")

    def test_identify_携带_set_与_set_once_时断言通过(self):
        recorder, _ = self.make_recorder()
        recorder.require_identity = True
        identify = event("$identify", "01a21038-7000-7000-8000-000000000004", distinct_id="stub-a")
        identify["properties"]["$set"] = {"membership": "plus"}
        identify["properties"]["$set_once"] = {"channel": "organic"}
        self.send(recorder, batch([LIFECYCLE, identify, BUSINESS_ONE, BUSINESS_TWO]))
        manifest = recorder.build_manifest()
        matched = {item["name"]: item["ok"] for item in manifest["assertions"]}
        self.assertTrue(matched["identity_observed"])

    def test_缺少_identify_时_required_断言失败(self):
        recorder, _ = self.make_recorder()
        recorder.require_identity = True
        self.send(recorder, batch([LIFECYCLE]))
        manifest = recorder.build_manifest()
        failed = {item["name"] for item in manifest["assertions"] if not item["ok"] and item["required"]}
        self.assertIn("identity_observed", failed)

    def test_同批重复_uuid_只回执一次且带_duplicates_dropped(self):
        recorder, _ = self.make_recorder()
        response = self.send(recorder, batch([BUSINESS_ONE, BUSINESS_TWO, BUSINESS_ONE]))
        results = response.body["results"]
        self.assertEqual(list(results).count(BUSINESS_ONE["uuid"]), 1)
        self.assertEqual(results[BUSINESS_ONE["uuid"]]["duplicates_dropped"], 1)

    def test_坏_gzip_返回请求级错误(self):
        recorder, _ = self.make_recorder()
        response = recorder.handle(headers_for("rid-1", 1, "2026-11-25T01:01:03.000Z"), b"not gzip")
        self.assertEqual(response.status, 400)
        self.assertIn("error", response.body)

    def test_空_batch_返回请求级错误(self):
        recorder, _ = self.make_recorder()
        response = self.send(recorder, batch([]))
        self.assertEqual(response.status, 400)

    def test_缺少请求_id_时回显头为空(self):
        recorder, out = self.make_recorder()
        headers = headers_for("ignored", 1, "2026-11-25T01:01:03.000Z")
        headers.pop("PostHog-Request-Id")
        response = recorder.handle(headers, gzip.compress(json.dumps(batch([LIFECYCLE])).encode()))
        self.assertEqual(response.status, 200)
        self.assertNotIn("PostHog-Request-Id", response.headers)


class CaptureProxyHttpTest(unittest.TestCase):
    def test_http_层回显请求_id_并保存_gzip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proxy = capture_proxy.CaptureProxy(
            out_dir=Path(tmp.name),
            platform="ios",
            transport="gzip",
            expected_namespace="com.example.riff.stub.ios",
            sample_set="ios-gzip",
        )
        proxy.start("127.0.0.1", 0)
        self.addCleanup(proxy.stop)
        payload = batch([LIFECYCLE])
        raw = gzip.compress(json.dumps(payload).encode())
        status, resp_headers, body = capture_proxy.post_to_proxy(
            f"http://127.0.0.1:{proxy.port}/i/v1/analytics/events",
            headers_for("rid-http", 1, "2026-11-25T01:01:03.000Z"),
            raw,
        )
        self.assertEqual(status, 200)
        self.assertEqual(resp_headers["posthog-request-id"], "rid-http")
        self.assertIn(LIFECYCLE["uuid"], body["results"])
        manifest = proxy.recorder.build_manifest()
        self.assertEqual(manifest["requestCount"], 1)


if __name__ == "__main__":
    unittest.main()
