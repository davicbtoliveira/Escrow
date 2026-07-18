from __future__ import annotations

from django.urls import path

from escrow.identity import views

urlpatterns = [
    path("csrf/", views.csrf_token, name="csrf-token"),
    path("register/", views.register, name="register"),
    path("login/", views.sign_in, name="login"),
    path("logout/", views.sign_out, name="logout"),
    path("password-recovery/", views.password_recovery, name="password-recovery"),
    path(
        "password-recovery/confirm/",
        views.confirm_password_recovery,
        name="password-recovery-confirm",
    ),
]
