"""Hardware-free tests for automatic PicoXTools Sniffer startup."""
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import struct
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

MODULE = Path(__file__).resolve().parents[1] / "x240c_i2c_logger.py"
spec = importlib.util.spec_from_file_location("x240c_setup_tests", MODULE)
logger = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = logger
spec.loader.exec_module(logger)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response = b'{"result":0,"sniffer":0}'
        self.status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((self.path, self.headers, json.loads(body)))
                self.send_response(owner.status)
                self.end_headers()
                self.wfile.write(owner.response)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.ws = Mock()
        self.ws.recv.return_value = b""
        self.connect = Mock(return_value=self.ws)
        self.websocket = types.SimpleNamespace(
            create_connection=self.connect,
            WebSocketTimeoutException=TimeoutError,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def capture(self, **kwargs):
        with patch.dict(sys.modules, {"websocket": self.websocket}):
            return list(logger.iter_picotools_frames(self.host, 0.1, False, **kwargs))

    def test_setup_before_capture_and_decode(self):
        def connect(*args, **kwargs):
            self.assertEqual(len(self.requests), 1)
            return self.ws

        self.connect.side_effect = connect
        words = (0x4000006C, 0x04, 0xD3, 0x02, 0xC0000000)
        packet = struct.pack("<H5I", 22, *words)
        self.ws.recv.side_effect = [packet, b""]
        frames = self.capture()
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/api/setup?type=i2c")
        self.assertEqual(headers["Content-Type"], "text/plain;charset=UTF-8")
        self.assertEqual(body, {
            "clk_pin": 9, "sda_pin": 8, "clock": 100000,
            "i2c_type": 2, "slave_addr": 49,
        })
        self.connect.assert_called_once_with(
            f"ws://{self.host}/ws/i2c", timeout=0.1,
            origin=f"http://{self.host}", http_proxy_host=None, http_proxy_port=None,
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].address, 0x6C)
        self.assertEqual(frames[0].data, [0x04, 0xD3, 0x02])
        self.ws.close.assert_called_once()

    def test_failed_setup_prevents_websocket_connection(self):
        for response in (b'{"result":-1}', b'{}', b'null', b'{"result":true}',
                         b'{"result":"0"}', b'not JSON'):
            with self.subTest(response=response):
                self.response = response
                with self.assertRaisesRegex(RuntimeError, "--no-picotools-setup"):
                    self.capture()
                self.connect.assert_not_called()

    def test_http_failure_prevents_capture(self):
        self.status = 404
        with self.assertRaisesRegex(RuntimeError, "404"):
            self.capture()
        self.connect.assert_not_called()

    def test_manual_mode_sends_no_setup_request(self):
        self.assertEqual(self.capture(auto_setup=False), [])
        self.assertEqual(self.requests, [])
        self.connect.assert_called_once()
        self.ws.close.assert_called_once()

    def test_socket_is_closed_if_receiving_fails(self):
        self.ws.recv.side_effect = OSError("device disconnected")
        with self.assertRaisesRegex(OSError, "disconnected"):
            self.capture()
        self.ws.close.assert_called_once()

    def test_timeout_then_frame(self):
        self.ws.recv.side_effect = [TimeoutError(), "debug text", None]
        self.assertEqual(self.capture(), [])
        self.ws.close.assert_called_once()

    def test_websocket_urls_and_cli(self):
        self.assertEqual(logger.picotools_urls("192.168.33.1/"),
                         ("ws://192.168.33.1/ws/i2c", "http://192.168.33.1"))
        self.assertEqual(logger.picotools_urls("wss://device:8443/custom"),
                         ("wss://device:8443/custom", "https://device:8443"))
        args = logger.build_argparser().parse_args(["--picotools", "--no-picotools-setup"])
        self.assertEqual(args.picotools, "192.168.33.1")
        self.assertTrue(args.no_picotools_setup)


if __name__ == "__main__":
    unittest.main()
