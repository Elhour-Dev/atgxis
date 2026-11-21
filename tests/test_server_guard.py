"""The review page answers only this PC's own page: not other websites, not other local ports."""
import http.client
import threading

import pytest

from autoclip import review
from autoclip.config import Config


@pytest.fixture
def port(tmp_path):
    srv = review.QuietServer(("127.0.0.1", 0), review.make_handler(review.Store(tmp_path, Config())))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def ask(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        r.read()
        return r.status
    finally:
        c.close()


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1.evil.example:{port}", "127.0.0.1:1", ""])
def test_get_from_a_foreign_host_is_refused(port, host):
    assert ask(port, "GET", "/api/settings", {"Host": host.format(port=port)}) == 403


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LOCALHOST:{port}"])
def test_get_from_this_pc_works(port, host):
    assert ask(port, "GET", "/api/settings", {"Host": host.format(port=port)}) == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost:1", "http://127.0.0.1:1", "null", "https://127.0.0.1:{port}"])
def test_post_from_another_origin_is_refused(port, origin):
    h = {"Origin": origin.format(port=port), "Content-Type": "application/json"}
    assert ask(port, "POST", "/api/status", h, b"{}") == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:{port}", "http://localhost:{port}"])
def test_post_from_the_page_itself_is_allowed(port, origin):
    h = {"Origin": origin.format(port=port), "Content-Type": "application/json"}
    assert ask(port, "POST", "/api/status", h, b"{}") == 400                                    # reached the handler: no such clip


def test_post_with_a_foreign_host_is_refused(port):
    assert ask(port, "POST", "/api/status", {"Host": "evil.example"}, b"{}") == 403


@pytest.mark.parametrize("length", ["-1", "abc", str(10 ** 30)])
def test_bad_content_length_is_a_clean_error(port, length):
    assert ask(port, "POST", "/api/status", {"Content-Length": length}) == 400


def test_json_that_is_not_an_object_is_a_clean_error(port):
    assert ask(port, "POST", "/api/status", {"Content-Type": "application/json"}, b"[]") == 400


def test_too_deeply_nested_json_is_a_clean_error(port):
    assert ask(port, "POST", "/api/status", {"Content-Type": "application/json"}, b"[" * 200000) == 400


def test_port_80_accepts_the_host_browsers_send():
    class Fake:
        server = type("S", (), {"server_address": ("127.0.0.1", 80)})()
        headers = {"Host": "127.0.0.1", "Origin": "http://127.0.0.1"}
    handler = review.make_handler(None)
    assert handler._from_this_page(Fake())
