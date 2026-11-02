import uuid
from decimal import Decimal

from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone


class Genre(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class Language(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class CastMember(models.Model):
    name = models.CharField(max_length=100)
    role = models.CharField(max_length=100, help_text="e.g. Actor, Director, Producer")
    image = models.ImageField(upload_to='cast/', blank=True, null=True)

    def __str__(self):
        return f"{self.name} ({self.role})"


class Movie(models.Model):
    AGE_CERT_CHOICES = [
        ('U', 'U - Universal'),
        ('UA', 'UA - Parental Guidance'),
        ('A', 'A - Adults Only'),
        ('S', 'S - Restricted'),
    ]
    name = models.CharField(max_length=255)
    image = models.ImageField(upload_to='movies/')
    rating = models.DecimalField(max_digits=3, decimal_places=1)
    cast = models.TextField(help_text="simple text cast overview (e.g., 'Actor A, Actor B')")
    description = models.TextField(blank=True, null=True)

    duration_minutes = models.PositiveIntegerField(default=120)
    release_date = models.DateField(null=True, blank=True)
    age_certification = models.CharField(max_length=5, choices=AGE_CERT_CHOICES, default='UA')

    genres = models.ManyToManyField(Genre, related_name='movies', blank=True)
    languages = models.ManyToManyField(Language, related_name='movies', blank=True)
    cast_members = models.ManyToManyField(CastMember, related_name='movies', blank=True)

    youtube_trailer_id = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="YouTube Video ID (e.g. for https://youtube.com/watch?v=XYZ123, enter XYZ123)"
    )
    is_trending = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['release_date'], name='movie_release_idx'),
            models.Index(fields=['rating'], name='movie_rating_idx'),
        ]

    def __str__(self):
        return self.name

    @property
    def average_rating(self):
        """Average of user reviews. Uses the prefetch cache when available (zero extra queries)."""
        reviews = list(self.reviews.all())
        if reviews:
            return round(sum(r.rating for r in reviews) / len(reviews), 1)
        return 0.0


class MoviePoster(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='extra_posters')
    image = models.ImageField(upload_to='movies/posters/')

    def __str__(self):
        return f"Poster for {self.movie.name}"


class Theater(models.Model):
    """Really a 'show': a theater + movie + time."""
    name = models.CharField(max_length=255)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    time = models.DateTimeField()
    city = models.CharField(max_length=100, default='Hyderabad')
    screen = models.CharField(max_length=50, default='Screen 1')
    price = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('150.00'))

    class Meta:
        indexes = [
            models.Index(fields=['movie', 'time'], name='th_movie_time_idx'),
            models.Index(fields=['city', 'name'], name='th_city_name_idx'),
        ]

    def __str__(self):
        return f'{self.name} - {self.movie.name} - {self.time}'


class Seat(models.Model):
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE)
    seat_number = models.CharField(max_length=10)
    is_booked = models.BooleanField(default=False)

    # Temporary reservation fields
    reserved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reserved_seats')
    reserved_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['theater', 'is_booked'], name='seat_theater_booked_idx')]

    @property
    def is_currently_reserved(self):
        """True if the seat is held and the reservation has not expired."""
        return bool(self.reserved_until and self.reserved_until > timezone.now())

    def is_reserved_by(self, user):
        return self.is_currently_reserved and self.reserved_by == user

    def __str__(self):
        return f'{self.seat_number} in {self.theater.name}'


class Booking(models.Model):
    STATUS_CHOICES = [('confirmed', 'Confirmed'), ('cancelled', 'Cancelled'), ('refunded', 'Refunded')]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE)
    booked_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='confirmed')
    amount = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    payment = models.ForeignKey('Payment', null=True, blank=True, on_delete=models.SET_NULL, related_name='bookings')
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['booked_at'], name='bk_time_idx'),
            # covering index: revenue SUM can be answered from the index alone
            models.Index(fields=['status', 'booked_at', 'amount'], name='bk_status_time_amt_idx'),
            models.Index(fields=['theater', 'booked_at'], name='bk_theater_time_idx'),
            models.Index(fields=['movie', 'status'], name='bk_movie_status_idx'),
            models.Index(fields=['user', 'booked_at'], name='bk_user_time_idx'),
        ]
        constraints = [
            # DB-level guarantee: a seat can never have two confirmed bookings
            models.UniqueConstraint(fields=['seat'], condition=models.Q(status='confirmed'),
                                    name='uniq_confirmed_booking_per_seat'),
        ]

    def __str__(self):
        return f'Booking by {self.user.username} for {self.seat.seat_number} at {self.theater.name}'


def make_ticket_token():
    return uuid.uuid4().hex


class Payment(models.Model):
    STATUS_CHOICES = [
        ('created', 'Created'), ('success', 'Success'), ('failed', 'Failed'),
        ('cancelled', 'Cancelled'), ('refund_pending', 'Refund pending'), ('refunded', 'Refunded'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments')
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='payments')
    seat_ids = models.CharField(max_length=255)            # "12,13,14" (sorted)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    razorpay_order_id = models.CharField(max_length=100, unique=True)
    razorpay_payment_id = models.CharField(max_length=100, unique=True, null=True, blank=True)  # transaction ID
    razorpay_signature = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='created')
    failure_reason = models.CharField(max_length=255, blank=True)
    ticket_token = models.CharField(max_length=32, unique=True, default=make_ticket_token, editable=False)
    email_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'created_at'], name='pay_user_time_idx'),
            models.Index(fields=['status', 'created_at'], name='pay_status_time_idx'),
        ]

    @property
    def seat_id_list(self):
        return [int(x) for x in self.seat_ids.split(',') if x]

    @property
    def booking_ref(self):
        return f'BMS{self.pk:06d}'

    def __str__(self):
        return f'{self.booking_ref} {self.status}'


class RecentlyViewed(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='recently_viewed')
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('user', 'movie')
        indexes = [models.Index(fields=['user', 'viewed_at'], name='rv_user_time_idx')]


class Review(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    rating = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField()
    is_verified_viewer = models.BooleanField(default=False)
    is_reported = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('movie', 'user')

    def __str__(self):
        return f"{self.user.username} - {self.movie.name} ({self.rating}/5)"