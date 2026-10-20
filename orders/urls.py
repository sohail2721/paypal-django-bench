from django.urls import path

from .views import (
    CancelOrderView,
    FulfilOrderView,
    MyOrdersView,
    OrderInstanceView,
    OrderItemView,
    OrderListView,
    OrderRefundsView,
    PayOrderView,
    PlaceOrderView,
)


urlpatterns = [
    path("i/orders/", OrderListView.as_view(), name="orders"),
    path("i/orders/<uuid:pk>/", OrderInstanceView.as_view(), name="order_detail"),
    path(
        "i/orders/<uuid:order_id>/items/<int:item_id>/",
        OrderItemView.as_view(),
        name="order_item"
    ),
    # PayPal checkout
    path("orders/", PlaceOrderView.as_view(), name="place_order"),
    path("orders/<uuid:pk>/pay/", PayOrderView.as_view(), name="order_pay"),
    path("orders/<uuid:pk>/fulfil/", FulfilOrderView.as_view(), name="order_fulfil"),
    path("orders/<uuid:pk>/cancel/", CancelOrderView.as_view(), name="order_cancel"),
    path("orders/<uuid:pk>/refunds/", OrderRefundsView.as_view(), name="order_refunds"),
    path("my-orders/", MyOrdersView.as_view(), name="my_orders"),
]
