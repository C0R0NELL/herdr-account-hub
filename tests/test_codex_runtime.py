#!/usr/bin/env python3
"""Codex control transport and optional native machine-wide reload checks."""
import copy
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import threading
import hashlib
import struct
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
import uuid
from unittest.mock import patch
from datetime import datetime, timezone

from tests.test_account_records import login


class _RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="account-hub-runtime-test-")
        self.home = Path(self.temp.name, "shared history")
        self.home.mkdir()
        self.dbhome = Path(self.temp.name, "separate database")
        self.dbhome.mkdir()
        env = {"ACCOUNT_HUB_STATE_DIR": str(Path(self.temp.name, "private")),
               "ACCOUNT_HUB_CONFIG_DIR": self.temp.name,
               "CODEX_HOME": str(self.home), "CODEX_SQLITE_HOME": str(self.dbhome)}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        spec = importlib.util.spec_from_file_location(
            "runtime_switcher", Path(__file__).resolve().parents[1] / "switcher.py")
        self.S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.S)
        self.S.proven_payload = lambda kind, p: p["payload"]
        self.S.renew_profile = lambda kind, p, force=False: p["payload"]
        self.S.notify = lambda *a: None
        self.profiles = []
        for user in ("one", "two", "three"):
            p = self.S.make_profile("codex", user, login(user, user, user))
            self.S.write_profile(p)
            self.profiles.append(p)
        self.thread = {"id": str(uuid.uuid4()), "name": "My custom title",
                       "cwd": self.temp.name, "status": {"type": "idle"}}
        self.gateways = []
        self.launched = []

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

class UnixWebSocketFrames(_RuntimeFixture):
    def transport(self, frames):
        class MemorySocket:
            def __init__(self):
                self.data = bytearray()
                self.sent = []
                self.closed = False
            def settimeout(self, value): pass
            def connect(self, path): pass
            def close(self): self.closed = True
            def shutdown(self, how): pass
            def sendall(self, data):
                self.sent.append(data)
                if data.startswith(b"GET "):
                    key = next(line.split(b": ",1)[1] for line in data.split(b"\r\n")
                               if line.startswith(b"Sec-WebSocket-Key:"))
                    accept = base64.b64encode(hashlib.sha1(
                        key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    self.data.extend(b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
                        + accept + b"\r\n\r\n" + frames)
            def recv(self, size):
                data = bytes(self.data[:min(7,size)])
                del self.data[:len(data)]
                return data
        memory = MemorySocket()
        with patch.object(self.S.socket, "socket", return_value=memory):
            transport = self.S._UnixWebSocket("/fake/control")
        return transport, memory

    def test_fragmented_rpc_with_ping_receives_json_and_sends_masked_pong(self):
        message = b'{"id": 1,"result": {"ok":true}}'
        frames = bytes([1, 10]) + message[:10] + bytes([0x89, 1]) + b"p"
        frames += bytes([0x80, len(message)-10]) + message[10:]
        transport, memory = self.transport(frames)
        self.assertEqual(transport.receive(), {"id":1,"result":{"ok":True}})
        self.assertEqual(memory.sent[-1][0], 0x8A)
        self.assertTrue(memory.sent[-1][1] & 0x80)

    def test_large_json_frame_and_client_mask(self):
        payload = json.dumps({"message": "x" * 200}).encode()
        transport, memory = self.transport(b"\x81\x7e" + struct.pack("!H",len(payload)) + payload)
        self.assertEqual(transport.receive(), {"message": "x"*200})
        transport.send({"test":True})
        frame = memory.sent[-1]
        self.assertTrue(frame[1] & 0x80)
        mask, data = frame[2:6], frame[6:]
        decoded = bytes(value ^ mask[index%4] for index,value in enumerate(data))
        self.assertEqual(json.loads(decoded), {"test":True})

    def test_masked_server_frame_and_oversize_are_refused(self):
        for frame in (b"\x81\x80", b"\x81\x7f" + struct.pack("!Q", 9*1024*1024)):
            transport, _ = self.transport(frame)
            with self.assertRaises(OSError):
                transport.receive()


@unittest.skipUnless(os.environ.get("ACCOUNT_HUB_TEST_REAL_CODEX") == "1",
                     "optional isolated test of the installed Codex binary")
class RealCodexMachine(_RuntimeFixture):
    def test_native_chats_reconnect_globally_with_same_ids_and_titles(self):
        import fcntl
        import pty
        import select
        import termios
        S = self.S
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def handle(self):
                try: super().handle()
                except (BrokenPipeError, ConnectionResetError): pass
            def do_GET(self):
                requests.append(self.path)
                payload = {"accounts": [{"id": "shared-team", "plan_type": "team",
                           "workspace_backend_origin": "https://chatgpt.com",
                           "account_routing_override": "NO_CONSTRAINT"}]} if "accounts/check" in self.path else {}
                self.send_response(200 if "accounts/check" in self.path else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
        backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        (self.home / "config.toml").write_text(
            f'chatgpt_base_url = "http://127.0.0.1:{backend.server_port}"\n'
            f'cli_auth_credentials_store = "file"\n[projects."{self.temp.name}"]\ntrust_level = "trusted"\n')
        settings = self.home / "app-server-daemon/settings.json"
        settings.parent.mkdir(mode=0o700)
        settings.write_text(json.dumps({"updater": {"autoUpdateEnabled": False}, "shutdownGraceSeconds": 0}))
        for profile in self.profiles:
            ident = profile["identity"]
            claims = {"email": ident["email"], "exp": int(time.time()) + 3600,
                      "sid": ident["user_id"],
                      "https://api.openai.com/profile": {"email": ident["email"]},
                      "https://api.openai.com/auth": {
                          "chatgpt_account_id": ident["account_id"],
                          "chatgpt_user_id": ident["user_id"], "chatgpt_plan_type": "team"}}
            encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            auth = profile["payload"]["auth"]
            auth["auth_mode"] = "chatgpt"
            auth["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            auth["tokens"]["access_token"] = auth["tokens"]["id_token"] = "fake." + encoded + ".fake"
            S.write_profile(profile)
        clients, rpc = [], None
        def daemon(action):
            result = S._run([S.CODEX, "app-server", "daemon", action], env=S._codex_environment(), timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr[:400])
            return json.loads(result.stdout)
        def stop_client(client):
            if client.poll() is None:
                client.terminate()
                try: client.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    client.kill(); client.wait(timeout=4)
        def pump(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                for client, master, output in clients:
                    if select.select([master], [], [], .05)[0]:
                        try: chunk = os.read(master, 65536)
                        except OSError: continue
                        output.extend(chunk)
                        if b"\x1b[6n" in chunk: os.write(master, b"\x1b[1;1R")
                        if b"\x1b[c" in chunk: os.write(master, b"\x1b[?1;2c")
        # Dummy credentials only. Every non-local network request is blocked.
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9",
                                     "HTTPS_PROXY": "http://127.0.0.1:9",
                                     "ALL_PROXY": "http://127.0.0.1:9",
                                     "NO_PROXY": "127.0.0.1,localhost"}):
            try:
                S.BACKENDS["codex"].write_live(self.profiles[0]["payload"])
                S._select_profile("codex", self.profiles[0]["id"])
                info = daemon("start")
                rpc = S.CodexRPC(info["socketPath"], S._codex_environment())
                before = []
                for index in range(2):
                    thread = rpc.call("thread/start", {"cwd": self.temp.name, "sandbox": "read-only"})["thread"]
                    rpc.call("thread/name/set", {"threadId": thread["id"], "name": "Fixture title " + str(index)})
                    before.append(rpc.call("thread/read", {"threadId": thread["id"]})["thread"])
                    master, slave = pty.openpty()
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
                    client = subprocess.Popen([S.CODEX, "resume", thread["id"]],
                        stdin=slave, stdout=slave, stderr=slave, cwd=self.temp.name,
                        env=dict(S._codex_environment(), TERM="xterm-256color"), start_new_session=True)
                    os.close(slave)
                    clients.append((client, master, bytearray()))
                pump(8)
                for client, master, output in clients:
                    self.assertIsNone(client.poll())
                    self.assertIn(b"OpenAI Codex", output)
                rpc.close(); rpc = None
                for index, profile in enumerate(self.profiles[1:]):
                    temporary_id = None
                    confirmed = ()
                    if index == 1:
                        info = daemon("version")
                        rpc = S.CodexRPC(info["socketPath"], S._codex_environment())
                        temporary = rpc.call("thread/start", {
                            "cwd": self.temp.name, "sandbox": "read-only", "ephemeral": True})["thread"]
                        self.assertTrue(temporary["ephemeral"])
                        temporary_id = temporary["id"]
                        rpc.close(); rpc = None
                        with self.assertRaises(S.TemporaryChatsError) as blocked:
                            S.switch("codex", profile["id"])
                        confirmed = blocked.exception.thread_ids
                        self.assertIn(temporary_id, confirmed)
                    message = S.switch("codex", profile["id"], discard_temporary=confirmed)
                    self.assertIn("2 chats reloaded", message)
                    info = daemon("version")
                    rpc = S.CodexRPC(info["socketPath"], S._codex_environment())
                    self.assertEqual(rpc.call("account/read", {"refreshToken": False})["account"]["email"],
                                     profile["identity"]["email"])
                    loaded = S.CodexMachine._loaded(rpc)
                    if temporary_id:
                        self.assertNotIn(temporary_id, loaded)
                        self.assertIn("1 temporary chat closed", message)
                    self.assertTrue({t["id"] for t in before}.issubset(loaded))
                    for prior in before:
                        after = rpc.call("thread/read", {"threadId": prior["id"]})["thread"]
                        self.assertEqual({k: after.get(k) for k in ("id", "name", "cwd")},
                                         {k: prior.get(k) for k in ("id", "name", "cwd")})
                    self.assertEqual(S.codex_application_status(), "applied")
                    rpc.close(); rpc = None
                    pump(3)
                    for client, master, output in clients:
                        self.assertIsNone(client.poll(), "native client exited during reload")
                        self.assertIn(b"Reconnect", output, "native client did not enter recovery")
                self.assertTrue(list(self.dbhome.glob("state_*.sqlite")))
                self.assertTrue(any("accounts/check" in path for path in requests))
                self.assertFalse(any("/responses" in path for path in requests), "no inference allowed")
            finally:
                if rpc: rpc.close()
                for client, master, output in clients:
                    stop_client(client); os.close(master)
                daemon("stop")


if __name__ == "__main__":
    unittest.main()
