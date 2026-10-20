import logging
import traceback

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .paypal.gateway import PayPalError
from .services import PaymentFlowError

logger = logging.getLogger("payments.api")


def payments_exception_handler(exc, context):
    """
    Error boundary for the payment endpoints.

    Business and provider failures become a JSON body with a message written by this app. Anything else
    becomes a generic 500 regardless of DEBUG: no traceback, exception type, secret or upstream body ever
    reaches the response. The log gets the stack frames but not the exception message, which could carry
    request data (card details) for some exception types.
    """
    if isinstance(exc, PaymentFlowError):
        body = {"error": exc.message}
        if exc.code:
            body["code"] = exc.code
        return Response(body, status=exc.status_code)
    if isinstance(exc, PayPalError):
        return Response({"error": exc.message, "code": "provider_error"}, status=exc.status_code)

    response = exception_handler(exc, context)
    if response is not None:
        return response

    view = context.get("view")
    logger.error(
        "Unhandled %s in %s\n%s",
        type(exc).__name__,
        type(view).__name__ if view else "view",
        "".join(traceback.format_tb(exc.__traceback__)),
    )
    return Response(
        {"error": "An internal error occurred."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
    )


class PaymentsAPIMixin:
    """Route a view's exceptions through ``payments_exception_handler``."""

    def get_exception_handler(self):
        return payments_exception_handler
