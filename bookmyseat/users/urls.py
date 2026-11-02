from django.contrib.auth import views as auth_views
from django.urls import path
from .views import register, login_view, profile, reset_password, home
urlpatterns = [
    path('', home, name='home'),
    path('register/', register,name='register'),
    path('login/', login_view, name='login'),
    path('profile/', profile, name='profile'),
    path('reset-password/', reset_password, name='reset_password'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password_reset/', auth_views.PasswordResetView.as_view(), name='password_reset'),
    path('password_reset/done/', auth_views.PasswordResetDoneView.as_view(), name='password_reset_done'),
    path('password-reset-confirm/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('password-reset-complete/', auth_views.PasswordResetCompleteView.as_view(), name='password_reset_complete'),

]