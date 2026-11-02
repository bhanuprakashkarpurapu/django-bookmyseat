from django.urls import path
from . import views

urlpatterns = [
    path('', views.movie_list, name='movie_list'),
    path('<int:movie_id>/', views.movie_detail, name='movie_detail'),
    path('<int:movie_id>/add_review/', views.add_review, name='add_review'),
    path('review/<int:review_id>/report/', views.report_review, name='report_review'),
    path('<int:movie_id>/theaters/', views.theater, name='theater_list'),
    path('theater/<int:theater_id>/seats/book/', views.book_Seats, name='book_seats'),

    # Task 3: payments
    path('theater/<int:theater_id>/checkout/', views.checkout, name='checkout'),
    path('payment/verify/', views.payment_verify, name='payment_verify'),
    path('payment/cancel/', views.payment_abort, {'kind': 'cancelled'}, name='payment_cancel'),
    path('payment/failed/', views.payment_abort, {'kind': 'failed'}, name='payment_failed'),
    path('payment/mock/', views.mock_payment, name='mock_payment'),
    path('payment/<int:payment_id>/retry/', views.retry_payment, name='payment_retry'),
    path('payment/webhook/', views.razorpay_webhook, name='razorpay_webhook'),

    # Task 6: tickets
    path('ticket/<int:payment_id>/download/', views.download_ticket, name='ticket_download'),
    path('ticket/verify/<str:token>/', views.ticket_verify, name='ticket_verify'),
]