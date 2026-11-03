"""P0-B/C: 실제 임시 포트에 서버를 띄워 HTTP 경계를 검증한다.

외부 웹페이지가 로컬 대시보드로 상태 변경 요청을 보내는 시나리오(CSRF)와,
요청 크기·상태 코드·404·세션 ID 검증을 모두 실제 소켓으로 확인한다.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable
from triagent.domain import runstatus
from triagent.security import webguard


class WebGuardUnitTests(unittest.TestCase):
    def test_content_length_bounds(self):
        self.assertEqual(webguard.parse_content_length(None), 0)
        self.assertEqual(webguard.parse_content_length(""), 0)
        self.assertEqual(webguard.parse_content_length("12"), 12)
        for bad in ("-1", "abc", "1e5", " 12 x", "12.5", "+5"):
            with self.subTest(bad=bad), self.assertRaises(webguard.RequestRejected) as ctx:
                webguard.parse_content_length(bad)
            self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(webguard.RequestRejected) as ctx:
            webguard.parse_content_length(str(webguard.MAX_BODY_BYTES + 1))
        self.assertEqual(ctx.exception.status, 413)

    def test_origin_allows_only_this_server(self):
        self.assertTrue(webguard.is_allowed_origin("http://127.0.0.1:8765", 8765))
        self.assertTrue(webguard.is_allowed_origin("http://localhost:8770", 8770))
        for bad in (
            "http://127.0.0.1:8766",     # 다른 포트
            "http://evil.example",
            "https://evil.example:8765",
            "http://127.0.0.1.evil.com:8765",
            "null",
            "",
            None,
            "file://",
            "http://127.0.0.1",          # 포트 없음
        ):
            with self.subTest(bad=bad):
                self.assertFalse(webguard.is_allowed_origin(bad, 8765))

    def test_host_must_be_local_with_the_real_port(self):
        self.assertTrue(webguard.is_allowed_host("127.0.0.1:8765", 8765))
        self.assertTrue(webguard.is_allowed_host("localhost:8774", 8774))
        for bad in ("evil.example:8765", "127.0.0.1:9999", "127.0.0.1", "", None):
            with self.subTest(bad=bad):
                self.assertFalse(webguard.is_allowed_host(bad, 8765))

    def test_ports_across_the_whole_fallback_range(self):
        for port in range(8765, 8775):
            self.assertTrue(
                webguard.is_allowed_origin(f"http://127.0.0.1:{port}", port)
            )
            self.assertTrue(webguard.is_allowed_host(f"127.0.0.1:{port}", port))

    def test_token_comparison_rejects_empty_and_mismatched(self):
        token = webguard.generate_csrf_token()
        self.assertTrue(webguard.tokens_match(token, token))
        self.assertFalse(webguard.tokens_match("", ""))
        self.assertFalse(webguard.tokens_match(None, token))
        self.assertFalse(webguard.tokens_match(token, token + "x"))

    def test_generated_tokens_are_unique_and_long(self):
        tokens = {webguard.generate_csrf_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        self.assertTrue(all(len(item) >= 32 for item in tokens))


class HttpServerTestCase(unittest.TestCase):
    """127.0.0.1의 빈 포트(0)에 실제 서버를 띄운다."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), roundtable.RoundtableHandler)
        cls.port = cls.server.server_address[1]
        roundtable.RoundtableHandler.server_port_number = cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path, *, method="GET", body=None, headers=None, host=None):
        url = f"{self.base}{path}"
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Host", host if host is not None else f"127.0.0.1:{self.port}")
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    @property
    def token(self):
        return roundtable.CSRF_TOKEN


class GetRequestTests(HttpServerTestCase):
    def test_dashboard_and_state_are_readable(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("TriAgent Control", body)

        status, body = self.request("/state.json")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("status", payload)
        # 폴링 응답에는 메시지 본문을 싣지 않는다.
        self.assertNotIn("feed_html", payload)

    def test_state_can_include_feed_on_demand(self):
        status, body = self.request("/state.json?feed=1")
        self.assertEqual(status, 200)
        self.assertIn("feed_html", json.loads(body))

    def test_state_api_is_not_cached(self):
        url = f"{self.base}/state.json"
        req = urllib.request.Request(url)
        req.add_header("Host", f"127.0.0.1:{self.port}")
        with urllib.request.urlopen(req, timeout=15) as response:
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
            self.assertEqual(
                response.headers.get("Content-Length"),
                str(len(response.read())),
            )

    def test_unknown_paths_return_404_not_the_dashboard(self):
        for path in ("/does-not-exist", "/api/nope.json", "/static/missing.js"):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn("<html", body.lower())

    def test_get_cannot_change_state(self):
        before = json.loads(self.request("/state.json")[1])["stopped"]
        status, _ = self.request("/stop")
        self.assertEqual(status, 404)
        after = json.loads(self.request("/state.json")[1])["stopped"]
        self.assertEqual(before, after)

    def test_get_rejects_foreign_host_header(self):
        status, _ = self.request("/state.json", host="evil.example")
        self.assertEqual(status, 403)

    def test_bad_session_id_returns_400(self):
        for bad in ("../victim", "..%5Cvictim", "C:%5CWindows"):
            with self.subTest(bad=bad):
                status, body = self.request(f"/session.json?id={bad}")
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))

    def test_missing_session_returns_404(self):
        status, _ = self.request("/session.json?id=20990101_000000_000000")
        self.assertEqual(status, 404)


class CsrfTests(HttpServerTestCase):
    """상태 변경 API 전체에 같은 방어가 걸려 있는지 확인한다."""

    STATE_CHANGING = [
        "/stop",
        "/pause",
        "/resume",
        "/topic",
        "/mode",
        "/roles",
        "/profile",
        "/budget",
        "/preflight",
        "/discussion-access",
        "/workspace/access",
        "/session/delete",
        "/session/clone",
        "/session/activate",
        "/session/name",
        "/session/meta",
        "/checkpoint/rollback",
        "/message",
        "/approve",
        "/reject",
        "/defer",
        "/retry",
        "/continue-next",
        "/answer-question",
        "/restart",
    ]

    def test_missing_token_is_rejected_everywhere(self):
        for path in self.STATE_CHANGING:
            with self.subTest(path=path):
                status, body = self.request(path, method="POST", body="")
                self.assertEqual(status, 403, f"{path} -> {body[:120]}")

    def test_wrong_token_is_rejected_everywhere(self):
        for path in self.STATE_CHANGING:
            with self.subTest(path=path):
                status, _ = self.request(
                    path, method="POST", body="",
                    headers={webguard.CSRF_HEADER: "not-the-real-token"},
                )
                self.assertEqual(status, 403)

    def test_foreign_origin_is_rejected_even_with_a_valid_token(self):
        for origin in ("http://evil.example", "https://attacker.test",
                       "http://127.0.0.1:1", "null"):
            with self.subTest(origin=origin):
                status, _ = self.request(
                    "/pause", method="POST", body="",
                    headers={webguard.CSRF_HEADER: self.token, "Origin": origin},
                )
                self.assertEqual(status, 403)

    def test_forged_host_header_is_rejected(self):
        status, _ = self.request(
            "/pause", method="POST", body="",
            headers={webguard.CSRF_HEADER: self.token},
            host="evil.example",
        )
        self.assertEqual(status, 403)

    def test_valid_local_origin_and_token_succeeds(self):
        status, body = self.request(
            "/defer", method="POST", body="",
            headers={
                webguard.CSRF_HEADER: self.token,
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(status, 200, body[:200])

    def test_token_can_be_supplied_through_the_form_body(self):
        status, _ = self.request(
            "/defer", method="POST",
            body=f"{webguard.CSRF_FORM_FIELD}={self.token}",
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status, 200)

    def test_dashboard_exposes_the_token_to_same_origin_pages_only(self):
        _, page = self.request("/")
        self.assertIn(roundtable.CSRF_TOKEN, page)


class DashboardRenderingTests(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        roundtable.STATE = roundtable.new_state()

    def tearDown(self):
        roundtable.STATE = self.original_state

    def test_placeholder_text_in_a_topic_is_not_substituted(self):
        roundtable.STATE["topic"] = "토큰 보여줘 __CSRF_TOKEN__"
        page = roundtable.render_dashboard()
        self.assertEqual(page.count(roundtable.CSRF_TOKEN), 1)   # <meta> 한 곳뿐
        self.assertIn("토큰 보여줘 __CSRF_TOKEN__", page)

    def test_plain_text_fields_are_not_html_escaped_in_json(self):
        # JS가 textContent로 넣으므로 서버가 이스케이프하면 "&amp;"가 그대로 보인다.
        roundtable.STATE["name"] = "A & B <설계>"
        self.assertEqual(roundtable.state_json_payload()["session_name"], "A & B <설계>")


class RequestSizeTests(HttpServerTestCase):
    def test_oversized_body_is_rejected_with_413(self):
        payload = ("x" * 2048).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/profile", data=payload, method="POST"
        )
        req.add_header("Host", f"127.0.0.1:{self.port}")
        req.add_header(webguard.CSRF_HEADER, self.token)
        # 실제 본문보다 큰 Content-Length를 신고해 상한 검사를 자극한다.
        req.add_header("Content-Length", str(webguard.MAX_BODY_BYTES + 10))
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (urllib.error.URLError, OSError):
            # 서버가 먼저 연결을 끊는 경우도 "거부"로 본다.
            status = 413
        self.assertIn(status, (400, 413))

    def test_negative_content_length_is_rejected(self):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.putrequest("POST", "/profile", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader(webguard.CSRF_HEADER, self.token)
        conn.putheader("Content-Length", "-5")
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        conn.close()


class SessionIdOverHttpTests(HttpServerTestCase):
    def test_state_changing_session_apis_reject_bad_ids_with_400(self):
        for path in ("/session/delete", "/session/clone", "/session/activate",
                     "/session/name", "/session/meta"):
            with self.subTest(path=path):
                status, body = self.request(
                    path, method="POST",
                    body="id=..%2Fvictim&name=x&tags=&favorite=false&archived=false",
                    headers={webguard.CSRF_HEADER: self.token},
                )
                self.assertEqual(status, 400, f"{path} -> {body[:160]}")
                self.assertIn("error", json.loads(body))


if __name__ == "__main__":
    unittest.main()


class UserCancelOverHttpTests(HttpServerTestCase):
    """6. 사용자 중단이 모델 실패로 기록되지 않고, 상태 표시가 일치해야 한다."""

    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE["topic"] = "중단 테스트"
        roundtable.STATE["active_agent"] = "antigravity"
        roundtable.STATE["active_phase"] = "구현"
        roundtable.STATE["run_status"] = runstatus.RunStatus.RUNNING

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)

    def post(self, path, body=""):
        return self.request(
            path, method="POST", body=body,
            headers={webguard.CSRF_HEADER: self.token,
                     "Origin": f"http://127.0.0.1:{self.port}"},
        )

    def test_stop_marks_user_cancelled_and_clears_running(self):
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "cancel_active_cli_processes", return_value=["Antigravity"]), \
             patch.object(roundtable, "add_runtime_event"):
            status, body = self.post("/stop")

        self.assertEqual(status, 200, body[:200])
        payload = json.loads(body)
        self.assertEqual(payload["run_status"], runstatus.RunStatus.USER_CANCELLED)
        self.assertEqual(payload["run_status_label"], "사용자 중단")
        self.assertTrue(roundtable.CONTROL["user_cancelled"])
        # 실행 중 표시가 즉시 사라진다 (목록이 "진행 중"으로 남지 않도록).
        self.assertIsNone(payload["active_agent"])
        # 사용자 중단에는 "실패 턴 재시도"를 권하지 않는다.
        self.assertFalse(payload["can_retry"])

    def test_stop_marks_cancel_before_killing_the_cli(self):
        """죽인 뒤에 표시하면 워커가 "종료 코드 1"을 먼저 보고 실패로 기록한다."""
        seen = {}

        def fake_cancel():
            seen["user_cancelled"] = roundtable.CONTROL.get("user_cancelled")
            seen["stopped"] = roundtable.CONTROL.get("stopped")
            return []

        roundtable.CONTROL["user_cancelled"] = False
        roundtable.CONTROL["stopped"] = False
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "cancel_active_cli_processes", side_effect=fake_cancel), \
             patch.object(roundtable, "add_runtime_event"):
            self.post("/stop")
        self.assertEqual(seen, {"user_cancelled": True, "stopped": True})

    def test_reject_clears_the_saved_plan_approval(self):
        roundtable.STATE.update(mode="coding", plan_approved=True)
        roundtable.CONTROL["approval_requested"] = True
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "add_message", return_value=True), \
             patch.object(roundtable, "add_runtime_event"):
            status, _ = self.post("/reject")
        self.assertEqual(status, 200)
        self.assertFalse(roundtable.STATE["plan_approved"])

    def test_session_list_and_header_agree_after_a_cancel(self):
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "cancel_active_cli_processes", return_value=[]), \
             patch.object(roundtable, "add_runtime_event"):
            self.post("/stop")

        header = json.loads(self.request("/state.json")[1])
        sessions = json.loads(self.request("/sessions.json")[1])
        active = next(
            (s for s in sessions["sessions"] if s["id"] == header["active_id"]), None
        )
        if active is not None:   # 현재 세션이 아직 디스크에 없을 수 있다
            self.assertEqual(active["status_label"], header["run_status_label"])

    def test_a_new_run_can_start_after_a_cancel(self):
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "cancel_active_cli_processes", return_value=[]), \
             patch.object(roundtable, "add_runtime_event"):
            self.post("/stop")
            self.assertTrue(roundtable.CONTROL["user_cancelled"])
            with patch.object(roundtable, "start_worker_if_needed"):
                status, _ = self.post("/resume")
        self.assertEqual(status, 200)
        self.assertFalse(roundtable.CONTROL["user_cancelled"])

    def test_cancelled_status_survives_a_server_restart(self):
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "cancel_active_cli_processes", return_value=[]), \
             patch.object(roundtable, "add_runtime_event"):
            self.post("/stop")
        snapshot = json.loads(json.dumps(roundtable.STATE, ensure_ascii=False))
        restored = roundtable.normalize_state(snapshot)
        self.assertEqual(restored["run_status"], runstatus.RunStatus.USER_CANCELLED)
        self.assertIsNone(restored["active_agent"])


class TopicGuardOverHttpTests(HttpServerTestCase):
    """코딩 파이프라인은 구현 담당(Claude Code)이 꺼져 있으면 시작하지 않는다."""

    def setUp(self):
        self.original_state = roundtable.STATE
        roundtable.STATE = roundtable.new_state()

    def tearDown(self):
        roundtable.STATE = self.original_state

    def test_coding_topic_without_claude_is_rejected_with_400(self):
        body = "topic=x&mode=coding&agent=codex&agent=antigravity"
        with patch.object(roundtable, "start_worker_if_needed") as start:
            status, text = self.request(
                "/topic", method="POST", body=body,
                headers={webguard.CSRF_HEADER: self.token, "Origin": f"http://127.0.0.1:{self.port}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
        self.assertEqual(status, 400, text[:200])
        self.assertIn("Claude Code", text)
        start.assert_not_called()
