from django.urls import path

from . import views

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("<int:pk>/send/", views.inbox_send, name="inbox_send"),
    path("<int:pk>/archive/", views.conversation_archive, name="conversation_archive"),
    path("<int:pk>/unarchive/", views.conversation_unarchive, name="conversation_unarchive"),
    path("<int:pk>/create-lead/", views.conversation_create_lead, name="conversation_create_lead"),
]
