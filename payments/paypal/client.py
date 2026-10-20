from __future__ import annotations

import atexit
import threading

from django.conf import settings
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import ClientCredentials

_client: PayPalServerSdkClient | None = None
_lock = threading.Lock()


def get_client() -> PayPalServerSdkClient:
    """
    Return the process-wide PayPal client, built lazily on first use.

    Building it on first use (rather than at import) keeps the connection pool out of any parent process
    a WSGI server forks workers from. The client is thread-safe and caches its OAuth token, so it is shared.
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = PayPalServerSdkClient(
                    oauth2=ClientCredentials(
                        client_id=settings.PAYPAL_CLIENT_ID,
                        client_secret=settings.PAYPAL_CLIENT_SECRET,
                    ),
                    # None selects the SDK's default (sandbox) address; an override is used verbatim and
                    # also moves the token request, which the SDK derives from the same base address.
                    base_url=settings.PAYPAL_BASE_URL,
                    timeout=settings.PAYPAL_TIMEOUT,
                    # Default retry policy kept: only GET/HEAD/PUT/OPTIONS are retried, never a POST.
                )
                atexit.register(_client.close)
    return _client


def set_client(client: PayPalServerSdkClient | None) -> None:
    """Replace the shared client (tests inject one wired to a stub transport)."""
    global _client
    with _lock:
        _client = client
