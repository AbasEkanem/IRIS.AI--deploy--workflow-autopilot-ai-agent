"""
Minimal Attio REST API client.

Handles:
- Bearer token auth
- Rate limiting (Attio allows 100 read req/s, 25 write req/s) with basic backoff on 429
- Fetching a record by object + record id
- Updating attributes on a record (used to write enrichment data back)

Docs referenced:
- Auth:        https://docs.attio.com/rest-api/guides/authentication
- Rate limits: Attio returns HTTP 429 + Retry-After header when exceeded
- Records:     https://docs.attio.com/rest-api/endpoint-reference/records
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# Prefer ATTIO_ACCESS_TOKEN (the canonical var used across the app and in .env);
# fall back to the legacy ATTIO_API_KEY name for backward compatibility.
ATTIO_API_KEY = os.environ.get("ATTIO_ACCESS_TOKEN") or os.environ.get("ATTIO_API_KEY")
BASE_URL = "https://api.attio.com/v2"

if not ATTIO_API_KEY:
    raise RuntimeError(
        "No Attio token found. Set ATTIO_ACCESS_TOKEN in .env "
        "(Workspace settings > Developers > + New access token)."
    )


class AttioClient:
    def __init__(self, api_key: str = ATTIO_API_KEY, max_retries: int = 3):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )
        self.max_retries = max_retries

    def _request(self, method: str, path: str, **kwargs):
        url = f"{BASE_URL}{path}"
        for attempt in range(self.max_retries + 1):
            resp = self.session.request(method, url, **kwargs)

            if resp.status_code == 429:
                # Rate limited — respect Retry-After if present, else backoff.
                retry_after = float(resp.headers.get("Retry-After", 1))
                if attempt < self.max_retries:
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()

            if resp.status_code >= 500 and attempt < self.max_retries:
                # Transient server error — small exponential backoff.
                time.sleep(2 ** attempt)
                continue

            resp.raise_for_status()
            return resp.json() if resp.content else None

        return None

    # ---- Records ----

    def get_record(self, object_slug: str, record_id: str) -> dict:
        """Fetch a single record by object type (e.g. 'people', 'companies') and record id."""
        return self._request("GET", f"/objects/{object_slug}/records/{record_id}")

    def update_record(self, object_slug: str, record_id: str, values: dict) -> dict:
        """
        Update attributes on a record.

        `values` should be a dict of attribute_slug -> value, following Attio's
        attribute value format, e.g.:
            {"enrichment_summary": [{"value": "Some enriched text"}]}
        """
        payload = {"data": {"values": values}}
        return self._request(
            "PATCH", f"/objects/{object_slug}/records/{record_id}", json=payload
        )

    def list_records(self, object_slug: str, limit: int = 25) -> dict:
        """List records for an object (useful for testing without a webhook)."""
        return self._request(
            "POST", f"/objects/{object_slug}/records/query", json={"limit": limit}
        )

    # ---- Webhooks (optional convenience, for registering via API instead of the UI) ----

    def list_webhooks(self) -> dict:
        return self._request("GET", "/webhooks")

    def create_webhook(self, target_url: str, event_types: list[str]) -> dict:
        payload = {
            "data": {
                "target_url": target_url,
                "subscriptions": [{"event_type": et} for et in event_types],
            }
        }
        return self._request("POST", "/webhooks", json=payload)