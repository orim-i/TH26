"""core URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.contrib.auth.views import LoginView
from rest_framework.authtoken.views import obtain_auth_token # <-- NEW
from apps.pages.views import register, index

urlpatterns = [
    path('', LoginView.as_view(template_name='accounts/login.html'), name='login'),  # Root goes to login
    path('accounts/login/', LoginView.as_view(template_name='accounts/login.html'), name='login'),
    path('accounts/register/', register, name='register'),  # Register URL
    path('accounts/', include('django.contrib.auth.urls')),
    path('dashboard/', index, name='dashboard'),  # <-- Add this line
    path('wallet/', include('wallet.urls')),
    path('', include('apps.pages.urls')),
    path("", include("apps.dyn_dt.urls")),
    path("", include("apps.dyn_api.urls")),
    path('charts/', include('apps.charts.urls')),
    path("", include('admin_datta.urls')),
    path("admin/", admin.site.urls),
]

# Lazy-load on routing is needed
# During the first build, API is not yet generated
try:
    urlpatterns.append( path("api/"      , include("api.urls"))    )
    urlpatterns.append( path("login/jwt/", view=obtain_auth_token) )
except:
    pass
