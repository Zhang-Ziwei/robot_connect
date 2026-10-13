"""mock_http_service 对机械臂任务返回动作已完成。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from mock_http.mock_http_service import Handler


class TestMockHttpService(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/arm"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.httpd.server_close()

    def test_post_task_returns_completed(self):
        payload = json.dumps({
            "task": "load_bottle_to_opener",
            "extra_params": {},
        }).encode("utf-8")
        request = Request(self.url, data=payload, method="POST", headers={
            "Content-Type": "application/json",
        })
        with urlopen(request, timeout=2) as response:
            body = json.loads(response.read().decode("utf-8"))
            status = response.status
        self.assertEqual(status, 200)
        self.assertEqual(body, {"success": True})


if __name__ == "__main__":
    unittest.main()
