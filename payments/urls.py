from django.urls import path

from .views import Payment, PaymentMethodInstanceView, PaymentMethodListView, ReconciliationView


urlpatterns = [
    path("payments/checkout/order/<uuid:pk>/", Payment.as_view(), name='payment'),
    path("payment-methods/", PaymentMethodListView.as_view(), name="payment_methods"),
    path("payment-methods/<uuid:pk>/", PaymentMethodInstanceView.as_view(), name="payment_method_detail"),
    path("reconciliation/", ReconciliationView.as_view(), name="reconciliation"),
]
