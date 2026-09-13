"""Sequential, paced requests with bounded retries. Never cache failed responses."""

import time
import logging
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

LOG = logging.getLogger(__name__)


class SourceError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, email: str = ""):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "pubtracker/0.1.2" + (f" (mailto:{email})" if email else "")
        self.last_request: dict[str, float] = {}

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        parsed_url = urlparse(url)
        host = parsed_url.hostname or ""
        target = host + parsed_url.path  # Omit query parameters, which can contain credentials.
        interval = 3.1 if "arxiv.org" in host else 0.4 if "ncbi.nlm.nih.gov" in host else 1.0
        for attempt in range(3):
            time.sleep(max(0, self.last_request.get(host, 0) + interval - time.monotonic()))
            self.last_request[host] = time.monotonic()
            retry_after = 0.0
            try:
                response = self.session.get(url, params=params, timeout=(10, 45))
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                response = exc.response
                status = response.status_code if response is not None else None
                reason = f"HTTP {status}" if status is not None else type(exc).__name__
                if response is not None and response.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise SourceError(f"{target}: {reason}") from exc
                if response is not None and response.headers.get("Retry-After"):
                    value = response.headers["Retry-After"]
                    try:
                        retry_after = float(value)
                    except ValueError:
                        try:
                            retry_after = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError):
                            pass
                if attempt == 2 or retry_after > 60:
                    raise SourceError(f"{target}: request failed after {attempt + 1} attempt(s): {reason}") from exc
                # A 429 needs a meaningful cooldown, even without Retry-After.
                delay = max(retry_after, 30 * 2 ** attempt if status == 429 else 2 ** (attempt + 1))
                LOG.warning("%s: %s; retrying in %s seconds (attempt %s/3)", target, reason, delay, attempt + 2)
                time.sleep(delay)
        raise AssertionError("unreachable")

    def close(self):
        self.session.close()
