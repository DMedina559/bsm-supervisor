"""Verify local authentication, semantic readiness, and HTTP isolation."""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from bsm_supervisor import runtime
from bsm_supervisor.config import Config


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(
            root / "state",
            root / "envs",
            root / "data",
            api_token_file=root / "token",
        )
        self.config.config_dir.mkdir(parents=True)
        self.config.api_token_file.write_text("readiness-secret")
        self.config.api_token_file.chmod(0o600)
        self.record = {
            "pid": 99,
            "instance_id": "runtime-id",
            "token": "shutdown-secret",
            "host": "127.0.0.1",
            "port": 11325,
            "config_dir": str(self.config.config_dir),
        }

    def healthy(self):
        return {
            "ready": True,
            "health": "healthy",
            "lifecycle": "ready",
            "checks": {
                name: {"healthy": True} for name in ("lifecycle", "database", "state")
            },
        }

    @patch("bsm_supervisor.runtime.SystemdService")
    @patch("bsm_supervisor.runtime.control_record")
    @patch("bsm_supervisor.runtime.request")
    def test_readiness_checks_health_and_exact_running_version(
        self, request, record, service
    ):
        service.return_value.status.return_value = {"ActiveState": "active"}
        record.return_value = self.record
        request.side_effect = [self.healthy(), {"info": {"app_version": "4.0.0b7"}}]
        result = runtime.ready(self.config, "4.0.0b7")
        self.assertTrue(result["ready"])
        self.assertEqual(
            request.call_args_list[0].kwargs, {"token": "readiness-secret"}
        )
        self.assertEqual(request.call_args_list[1].kwargs, {})

    @patch("bsm_supervisor.runtime.SystemdService")
    @patch("bsm_supervisor.runtime.control_record")
    @patch("bsm_supervisor.runtime.request")
    def test_http_200_degraded_and_wrong_version_are_not_ready(
        self, request, record, service
    ):
        service.return_value.status.return_value = {"ActiveState": "active"}
        record.return_value = self.record
        health = self.healthy()
        health["health"] = "degraded"
        request.return_value = health
        with self.assertRaisesRegex(runtime.RuntimeErrorBase, "healthy"):
            runtime.ready(self.config, "4.0.0b7")
        request.side_effect = [self.healthy(), {"info": {"app_version": "4.0.0b6"}}]
        with self.assertRaisesRegex(runtime.RuntimeErrorBase, "version"):
            runtime.ready(self.config, "4.0.0b7")

    @patch("bsm_supervisor.runtime.SystemdService")
    @patch("bsm_supervisor.runtime.control_record")
    @patch("bsm_supervisor.runtime.request")
    def test_local_readiness_needs_no_user_token(self, request, record, service):
        from dataclasses import replace

        config = replace(self.config, api_token_file=None)
        service.return_value.status.return_value = {"ActiveState": "active"}
        record.return_value = {**self.record, "readiness_token": "local-readiness"}
        request.return_value = {
            **self.healthy(),
            "instance_id": self.record["instance_id"],
            "pid": self.record["pid"],
            "version": "4.0.0b7",
        }
        self.assertTrue(runtime.ready(config, "4.0.0b7")["ready"])
        request.assert_called_once_with(
            record.return_value, "/api/health", readiness=True
        )
        request.return_value["instance_id"] = "different"
        with self.assertRaisesRegex(runtime.RuntimeErrorBase, "different"):
            runtime.ready(config, "4.0.0b7")

    @patch("bsm_supervisor.runtime.SystemdService")
    @patch("bsm_supervisor.runtime.control_record")
    @patch("bsm_supervisor.runtime.request")
    def test_rejected_local_credential_never_falls_back_to_user_token(
        self, request, record, service
    ):
        service.return_value.status.return_value = {"ActiveState": "active"}
        record.return_value = {**self.record, "readiness_token": "local-readiness"}
        request.side_effect = runtime.AuthenticationError("rejected")
        with self.assertRaises(runtime.AuthenticationError):
            runtime.ready(self.config, "4.0.0b7")
        request.assert_called_once()

    @patch("bsm_supervisor.runtime.ready")
    def test_invalid_credentials_fail_without_startup_retry(self, ready):
        ready.side_effect = runtime.AuthenticationError("credential rejected")
        with self.assertRaises(runtime.AuthenticationError):
            runtime.wait_ready(self.config, "4.0.0b7")
        ready.assert_called_once()

    def test_token_file_must_be_private_and_never_appears_in_error(self):
        self.config.api_token_file.chmod(0o644)
        with self.assertRaises(runtime.AuthenticationError) as failure:
            runtime.bearer(self.config)
        self.assertNotIn("readiness-secret", str(failure.exception))
        self.config.api_token_file.unlink()
        other = Path(self.temp.name) / "other"
        other.write_text("secret")
        self.config.api_token_file.symlink_to(other)
        with self.assertRaises(runtime.AuthenticationError):
            runtime.bearer(self.config)

    def test_record_rejects_pid_generation_and_nonlocal_address(self):
        boot = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        self.record["created_at"] = boot + 100 / os.sysconf("SC_CLK_TCK")
        path = self.config.config_dir / "web_server.control.json"
        path.write_text(json.dumps(self.record))
        path.chmod(0o600)
        command = (
            b"python\0-m\0bedrock_server_manager\0--config-dir\0"
            + str(self.config.config_dir).encode()
            + b"\0"
        )
        with (
            patch("bsm_supervisor.runtime.process_generation", return_value="100"),
            patch("bsm_supervisor.runtime.process_owner", return_value=os.geteuid()),
            patch("bsm_supervisor.runtime.Path.read_bytes", return_value=command),
        ):
            runtime.control_record(self.config, {"MainPID": "99"})
            with patch(
                "bsm_supervisor.runtime.process_owner", return_value=os.geteuid() + 1
            ):
                with self.assertRaisesRegex(runtime.RuntimeErrorBase, "owner"):
                    runtime.control_record(self.config, {"MainPID": "99"})
            path.chmod(0o644)
            with self.assertRaisesRegex(runtime.RuntimeErrorBase, "permissions"):
                runtime.control_record(self.config, {"MainPID": "99"})
            path.chmod(0o600)
            with self.assertRaisesRegex(runtime.RuntimeErrorBase, "PID"):
                runtime.control_record(self.config, {"MainPID": "98"})
            self.record["created_at"] = 0
            path.write_text(json.dumps(self.record))
            with self.assertRaisesRegex(runtime.RuntimeErrorBase, "generation"):
                runtime.control_record(self.config, {"MainPID": "99"})
            self.record["created_at"] = float("nan")
            path.write_text(json.dumps(self.record))
            with self.assertRaisesRegex(runtime.RuntimeErrorBase, "generation"):
                runtime.control_record(self.config, {"MainPID": "99"})
            self.record["created_at"] = boot + 100 / os.sysconf("SC_CLK_TCK")
            self.record["host"] = "8.8.8.8"
            path.write_text(json.dumps(self.record))
            with self.assertRaisesRegex(runtime.RuntimeErrorBase, "loopback"):
                runtime.control_record(self.config, {"MainPID": "99"})

    @patch("bsm_supervisor.runtime.SystemdService")
    @patch("bsm_supervisor.runtime.control_record")
    @patch("bsm_supervisor.runtime.request")
    def test_shutdown_requires_matching_receipt(self, request, record, service):
        service.return_value.status.return_value = {"ActiveState": "active"}
        record.return_value = self.record
        request.return_value = {
            "status": "accepted",
            "phase": "stopping",
            "pid": 100,
            "instance_id": "different-runtime",
        }
        with self.assertRaisesRegex(runtime.RuntimeErrorBase, "different"):
            runtime.shutdown(self.config)
        self.assertEqual(request.call_args.kwargs, {"shutdown": True})

    def test_real_http_redirect_does_not_forward_credentials(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/forbidden")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            record = {**self.record, "port": server.server_port}
            with self.assertRaises(runtime.RuntimeErrorBase) as failure:
                runtime.request(record, "/redirect", token="never-forward-me")
            self.assertEqual(seen, ["/redirect"])
            self.assertNotIn("never-forward-me", str(failure.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
