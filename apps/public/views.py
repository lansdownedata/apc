from django.shortcuts import redirect, render

from .forms import SERVICE_TYPE_CHOICES, BookingRequestForm
from .services import create_lead_from_booking


def home(request):
    return render(request, "public/home.html")


def bookings(request):
    if request.method == "POST":
        form = BookingRequestForm(request.POST)
        if form.is_valid():
            create_lead_from_booking(form.cleaned_data)
            return redirect("public:booking_thanks")
    else:
        form = BookingRequestForm()
    return render(
        request, "public/bookings.html", {"form": form, "service_options": SERVICE_TYPE_CHOICES}
    )


def booking_thanks(request):
    return render(request, "public/booking_thanks.html")
