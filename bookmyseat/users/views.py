from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from .forms import UserRegistrationForm, UserUpdateForm
from django.contrib.auth.decorators import login_required
from movies.models import Movie, Booking, Payment


def home(request):
    movies = Movie.objects.all()
    return render(request, 'users/home.html', {'movies': movies})


def register(request):
    if request.method == 'POST':
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data['username']
            password = form.cleaned_data['password1']
            user = authenticate(username=username, password=password)
            login(request, user)
            return redirect('profile')
    else:
        form = UserRegistrationForm()
    return render(request, 'users/register.html', {'form': form})


def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('/')
    else:
        form = AuthenticationForm()
    return render(request, 'users/login.html', {'form': form})


@login_required
def profile(request):
    bookings = (Booking.objects.filter(user=request.user)
                .select_related('movie', 'theater', 'seat').order_by('-booked_at'))
    payments = (Payment.objects.filter(user=request.user)
                .select_related('theater__movie').prefetch_related('bookings__seat')
                .order_by('-created_at')[:50])
    if request.method == 'POST':
        u_form = UserUpdateForm(request.POST, instance=request.user)
        if u_form.is_valid():
            u_form.save()
            return redirect('profile')
    else:
        u_form = UserUpdateForm(instance=request.user)
    return render(request, 'users/profile.html',
                  {'u_form': u_form, 'bookings': bookings, 'payments': payments})


@login_required
def reset_password(request):
    if request.method == 'POST':
        form = PasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            form.save()
            return redirect('login')
    else:
        form = PasswordChangeForm(user=request.user)
    return render(request, 'users/reset_password.html', {'form': form})