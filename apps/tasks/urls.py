from django.urls import path

from . import views

urlpatterns = [
    path("<int:pk>/complete/", views.task_complete, name="task_complete"),
    path("<int:pk>/skip/", views.task_skip, name="task_skip"),
    path("<int:pk>/reopen/", views.task_reopen, name="task_reopen"),
    path("<int:pk>/reassign/", views.task_reassign, name="task_reassign"),
]
