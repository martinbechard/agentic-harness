"""The loopback dashboard serves evidence without exposing mutation or cross-origin reads."""

import http.client
import json
import threading
from unittest.mock import Mock

import pytest

from backlog_harness.dashboard import create_dashboard_server


@pytest.fixture
def dashboard():
    collector = Mock(return_value={"items": [{"item_id": "one"}]})
    server = create_dashboard_server(collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, *, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    yield server, collector, request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_dashboard_rejects_nonlocal_binding():
    with pytest.raises(ValueError, match="loopback"):
        create_dashboard_server(dict, host="0.0.0.0")


def test_assets_snapshot_health_and_head(dashboard):
    server, collector, request = dashboard
    for path, kind in [
        ("/", "text/html"),
        ("/dashboard.js", "text/javascript"),
        ("/dashboard.css", "text/css"),
    ]:
        code, headers, body = request(path)
        assert code == 200 and body
        assert headers["Content-Type"].startswith(kind)
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert request(path, method="HEAD")[2] == b""
    code, _, body = request(
        "/api/snapshot", headers={"Origin": f"http://127.0.0.1:{server.server_port}"}
    )
    assert code == 200 and json.loads(body) == collector.return_value
    assert json.loads(request("/api/health")[2]) == {"read_only": True}
    for path in ("/missing", "/api/snapshot?x=1", "http://example.test/api/snapshot"):
        assert request(path, headers={"Host": f"127.0.0.1:{server.server_port}"})[0] == 404


def test_cross_origin_requests_never_read_evidence(dashboard):
    _, collector, request = dashboard
    for headers in (
        {"Host": "example.test"},
        {"Origin": "http://example.test"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        assert request("/api/snapshot", headers=headers)[0] == 403
    collector.assert_not_called()


def test_mutations_are_rejected_and_collector_failures_are_explicit(dashboard):
    _, collector, request = dashboard
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert request("/api/snapshot", method=method)[0] == 405
    collector.assert_not_called()
    for failure in (OSError("missing"), ValueError("invalid"), RuntimeError("unavailable")):
        collector.side_effect = failure
        code, _, body = request("/api/snapshot")
        assert code == 503
        assert json.loads(body) == {"error": "Evidence unavailable"}
