import requests
import pytest

from pubtracker.http import HttpClient, SourceError


def response(status=200, retry_after=None):
    value = requests.Response()
    value.status_code = status
    value.url = "https://example.com"
    if retry_after:
        value.headers["Retry-After"] = retry_after
    return value


@pytest.mark.parametrize("host,minimum", [("eutils.ncbi.nlm.nih.gov", 0.4), ("export.arxiv.org", 3.1), ("api.biorxiv.org", 1.0)])
def test_rate_limits(monkeypatch, host, minimum):
    elapsed, calls = [100.0], []
    monkeypatch.setattr("pubtracker.http.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr("pubtracker.http.time.sleep", lambda duration: elapsed.__setitem__(0, elapsed[0] + duration))
    client = HttpClient()
    def get(*args, **kwargs):
        calls.append(elapsed[0])
        assert kwargs["timeout"] == (10, 45)
        return response()
    monkeypatch.setattr(client.session, "get", get)
    client.get(f"https://{host}/test")
    client.get(f"https://{host}/test")
    assert calls[1] - calls[0] >= minimum - 0.000001
    client.close()


def test_retry_after_and_bounded_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr("pubtracker.http.time.sleep", sleeps.append)
    client = HttpClient()
    replies = iter([response(429, "5"), response(503), response()])
    monkeypatch.setattr(client.session, "get", lambda *a, **k: next(replies))
    assert client.get("https://example.com/test").status_code == 200
    assert 30 in sleeps and 4 in sleeps
    monkeypatch.setattr(client.session, "get", lambda *a, **k: response(429, "120"))
    with pytest.raises(SourceError, match="1 attempt"):
        client.get("https://example.com/test")
    client.close()


@pytest.mark.parametrize("status,delays", [(429, (30, 60)), (503, (2, 4))])
def test_retry_exhaustion_preserves_status_and_logs_safe_context(monkeypatch, caplog, status, delays):
    sleeps, calls = [], []
    monkeypatch.setattr("pubtracker.http.time.sleep", sleeps.append)
    client = HttpClient()

    def get(*args, **kwargs):
        calls.append(args)
        return response(status)

    monkeypatch.setattr(client.session, "get", get)
    with pytest.raises(SourceError, match=f"3 attempt.*HTTP {status}") as error:
        client.get("https://example.com/api/query?token=hidden", {"api_key": "secret"})
    assert len(calls) == 3
    assert all(delay in sleeps for delay in delays)
    assert "example.com/api/query" in str(error.value)
    assert f"HTTP {status}" in caplog.text
    for secret in ("hidden", "secret"):
        assert secret not in str(error.value) + caplog.text
    client.close()


def test_permanent_http_error_is_not_retried(monkeypatch):
    client = HttpClient()
    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        return response(404)

    monkeypatch.setattr(client.session, "get", get)
    with pytest.raises(SourceError, match="HTTP 404"):
        client.get("https://example.com/test")
    assert len(calls) == 1
    client.close()


def test_rate_limit_honors_retry_after_longer_than_default(monkeypatch):
    sleeps = []
    monkeypatch.setattr("pubtracker.http.time.sleep", sleeps.append)
    client = HttpClient()
    replies = iter([response(429, "45"), response()])
    monkeypatch.setattr(client.session, "get", lambda *a, **k: next(replies))
    assert client.get("https://example.com/test").status_code == 200
    assert 45 in sleeps
    client.close()


def test_timeout_remains_visible_after_retries(monkeypatch):
    monkeypatch.setattr("pubtracker.http.time.sleep", lambda _: None)
    client = HttpClient()

    def get(*args, **kwargs):
        raise requests.ReadTimeout()

    monkeypatch.setattr(client.session, "get", get)
    with pytest.raises(SourceError, match="3 attempt.*ReadTimeout"):
        client.get("https://example.com/test")
    client.close()
