from django.urls import path
from . import views
from django.contrib.auth.views import LoginView

urlpatterns = [
    path("", LoginView.as_view(template_name="accounts/login.html"), name="login"),  # Root goes to login
    path("dashboard/", views.dashboard, name="dashboard"),
    path("cards/", views.cards_dashboard, name="cards"),
    path("cards/", views.cards_dashboard, name="cards_dashboard"),
    path("deals/", views.perks_dashboard, name="deals"),
    path("goals/", views.spending_dashboard, name="goals"),
    path('cards/delete/<int:card_id>/', views.delete_card, name='delete_card'),
    path("cards/add/", views.add_card, name="add_card"),
]
