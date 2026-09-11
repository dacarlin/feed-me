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
    assert 5 in sleeps and 4 in sleeps
    monkeypatch.setattr(client.session, "get", lambda *a, **k: response(429, "120"))
    with pytest.raises(SourceError, match="1 attempt"):
        client.get("https://example.com/test")
    client.close()
