"""
PayPal integration boundary.

Only this package imports ``pay_pal_server_sdk``. The rest of the app talks to ``gateway`` and receives
plain dataclasses or a single ``PayPalError``.
"""
