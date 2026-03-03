"""
apis/base.py
Base API client all service wrappers inherit from.
Handles retries, rate limiting, timeout, and error reporting.
"""

import time
import os
import httpx
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class APIError(Exception):
    def __init__(self, api_name: str, status_code: int, message: str):
        self.api_name = api_name
        self.status_code = status_code
        self.message = message
        super().__init__(f"[{api_name}] HTTP {status_code}: {message}")


class RateLimitError(APIError):
    pass


class BaseAPIClient:

    API_NAME = "base"
    BASE_URL = ""
    ENV_KEY = ""

    def __init__(self):
        self.api_key = os.getenv(self.ENV_KEY, "")
        if not self.api_key:
            raise EnvironmentError(
                f"Missing API key: {self.ENV_KEY}\n"
                f"Add it to your .env file."
            )
        self.client = httpx.Client(timeout=120.0)

    def _headers(self) -> dict:
        """Override in subclasses to provide auth headers."""
        return {}

    def _request(
        self,
        method: str,
        endpoint: str,
        retries: int = 3,
        retry_delay: float = 2.0,
        **kwargs
    ) -> httpx.Response:
        url = f"{self.BASE_URL}{endpoint}"
        headers = {**self._headers(), **kwargs.pop("headers", {})}

        for attempt in range(retries):
            try:
                response = self.client.request(
                    method, url, headers=headers, **kwargs
                )

                if response.status_code == 429:
                    wait = retry_delay * (2 ** attempt)
                    print(f"  ⏳ Rate limited by {self.API_NAME}, waiting {wait}s...")
                    time.sleep(wait)
                    continue

                if response.status_code >= 500:
                    if attempt < retries - 1:
                        time.sleep(retry_delay)
                        continue
                    raise APIError(self.API_NAME, response.status_code, response.text[:200])

                if response.status_code >= 400:
                    raise APIError(self.API_NAME, response.status_code, response.text[:200])

                return response

            except httpx.TimeoutException:
                if attempt < retries - 1:
                    print(f"  ⏳ Timeout on {self.API_NAME}, retrying...")
                    time.sleep(retry_delay)
                    continue
                raise APIError(self.API_NAME, 0, "Request timed out after all retries")

        raise APIError(self.API_NAME, 429, "Rate limit exceeded after all retries")

    def get(self, endpoint: str, **kwargs) -> httpx.Response:
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs) -> httpx.Response:
        return self._request("POST", endpoint, **kwargs)

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
