import csv
from datetime import datetime, time, timedelta

from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.http import HttpResponse, StreamingHttpResponse
from django.template import engines
from django.urls import path
from django.utils import timezone

from .models import (Movie, Theater, Seat, Booking, Genre, Language, CastMember,
                     MoviePoster, Review, Payment, RecentlyViewed)


# ----------------------------- analytics -----------------------------
def _d(value, default):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return default


def _bars(rows, key):
    top = max([r[key] or 0 for r in rows], default=0) or 1
    for r in rows:
        r['pct'] = int(100 * (r[key] or 0) / top)
    return rows


def collect(start, end):
    """Every metric is a DB-side aggregate; only small result sets reach Python."""
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(start, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz)
    today0 = timezone.make_aware(datetime.combine(timezone.localdate(), time.min), tz)
    week0 = today0 - timedelta(days=today0.weekday())
    month0, year0 = today0.replace(day=1), today0.replace(month=1, day=1)
    in_range = Q(booked_at__gte=start_dt, booked_at__lt=end_dt)

    # 1 query: daily/weekly/monthly/yearly + selected range, range-limited so the index is used
    revenue = (Booking.objects.filter(status='confirmed', booked_at__gte=min(year0, start_dt))
               .aggregate(daily=Sum('amount', filter=Q(booked_at__gte=today0)),
                          weekly=Sum('amount', filter=Q(booked_at__gte=week0)),
                          monthly=Sum('amount', filter=Q(booked_at__gte=month0)),
                          yearly=Sum('amount', filter=Q(booked_at__gte=year0)),
                          selected=Sum('amount', filter=in_range),
                          selected_bookings=Count('id', filter=in_range)))

    rng = Booking.objects.filter(booked_at__gte=start_dt, booked_at__lt=end_dt)
    ok = rng.filter(status='confirmed')

    trend = _bars(list(ok.annotate(day=TruncDate('booked_at')).values('day')
                       .annotate(bookings=Count('id'), revenue=Sum('amount')).order_by('day')), 'bookings')

    hours = {h: 0 for h in range(24)}
    for r in ok.annotate(hour=ExtractHour('booked_at')).values('hour').annotate(n=Count('id')):
        hours[r['hour']] = r['n']
    peak = _bars([{'hour': f'{h:02d}:00', 'n': n} for h, n in hours.items()], 'n')

    top_movies = _bars(list(ok.values('movie__name').annotate(n=Count('id'), revenue=Sum('amount'))
                            .order_by('-n')[:10]), 'n')
    top_theaters = _bars(list(ok.values('theater__name', 'theater__city')
                              .annotate(n=Count('id'), revenue=Sum('amount')).order_by('-revenue')[:10]), 'revenue')

    occupancy = list(Seat.objects.filter(theater__time__gte=start_dt, theater__time__lt=end_dt)
                     .values('theater__name', 'theater__city')
                     .annotate(total=Count('id'), booked=Count('id', filter=Q(is_booked=True)))
                     .order_by('theater__name'))
    for r in occupancy:
        r['occupancy'] = round(100 * r['booked'] / r['total'], 1) if r['total'] else 0
        r['pct'] = int(r['occupancy'])

    cancel = rng.aggregate(total=Count('id'), cancelled=Count('id', filter=Q(status='cancelled')),
                           refunded=Count('id', filter=Q(status='refunded')),
                           refunded_amount=Sum('amount', filter=Q(status='refunded')))
    cancel['rate'] = round(100 * (cancel['cancelled'] + cancel['refunded']) / cancel['total'], 1) if cancel['total'] else 0
    pay = (Payment.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt)
           .aggregate(total=Count('id'), success=Count('id', filter=Q(status='success')),
                      failed=Count('id', filter=Q(status='failed')),
                      cancelled=Count('id', filter=Q(status='cancelled')),
                      refunded=Count('id', filter=Q(status__in=['refunded', 'refund_pending']))))

    joined = User.objects.filter(date_joined__gte=start_dt, date_joined__lt=end_dt)
    user_growth = _bars(list(joined.annotate(day=TruncDate('date_joined')).values('day')
                             .annotate(n=Count('id')).order_by('day')), 'n')
    users = {'new': joined.count(), 'total': User.objects.count()}

    return dict(revenue=revenue, trend=trend, peak=peak, top_movies=top_movies,
                top_theaters=top_theaters, occupancy=occupancy, cancel=cancel, pay=pay,
                user_growth=user_growth, users=users)


def summary_csv(d, start, end):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="report_{start}_{end}.csv"'
    w = csv.writer(resp)
    for title, mapping in (('Revenue', d['revenue']), ('Cancellations/Refunds', d['cancel']),
                           ('Payments', d['pay']), ('Users', d['users'])):
        w.writerow([title]); [w.writerow([k, v if v is not None else 0]) for k, v in mapping.items()]; w.writerow([])
    for title, rows in (('Booking trend', d['trend']), ('Peak hours', d['peak']), ('Most booked movies', d['top_movies']),
                        ('Top theaters', d['top_theaters']), ('Occupancy', d['occupancy']),
                        ('User growth', d['user_growth'])):
        w.writerow([title])
        if rows:
            keys = [k for k in rows[0] if k != 'pct']
            w.writerow(keys); [w.writerow([r[k] for k in keys]) for r in rows]
        w.writerow([])
    return resp


class _Echo:
    def write(self, value):
        return value


def bookings_csv(start, end):
    """Streams raw rows with .iterator(): constant memory even for 100k+ bookings."""
    tz = timezone.get_current_timezone()
    s = timezone.make_aware(datetime.combine(start, time.min), tz)
    e = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz)
    qs = (Booking.objects.filter(booked_at__gte=s, booked_at__lt=e).order_by('id')
          .values_list('id', 'booked_at', 'user__username', 'movie__name', 'theater__name',
                       'seat__seat_number', 'status', 'amount'))

    def rows():
        wr = csv.writer(_Echo())
        yield wr.writerow(['id', 'booked_at', 'user', 'movie', 'theater', 'seat', 'status', 'amount'])
        for r in qs.iterator(chunk_size=2000):
            yield wr.writerow(r)
    resp = StreamingHttpResponse(rows(), content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="bookings_{start}_{end}.csv"'
    return resp


DASHBOARD_TEMPLATE = """
{% extends "admin/base_site.html" %}
{% block content %}
<style>
 .dg{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
 .dc{background:#f8f8f8;border:1px solid #ddd;border-radius:6px;padding:10px}
 .dc b{display:block;font-size:20px} .bar{background:#417690;height:10px;border-radius:3px}
 .sec{display:inline-block;vertical-align:top;width:48%;min-width:320px;margin:0 1% 20px 0}
</style>
<form method="get" style="margin-bottom:14px">
 From <input type="date" name="start" value="{{ start|date:'Y-m-d' }}">
 To <input type="date" name="end" value="{{ end|date:'Y-m-d' }}">
 <button class="button" type="submit">Apply</button>
 <a class="button" href="?start={{ start|date:'Y-m-d' }}&end={{ end|date:'Y-m-d' }}&export=summary">Export summary CSV</a>
 <a class="button" href="?start={{ start|date:'Y-m-d' }}&end={{ end|date:'Y-m-d' }}&export=bookings">Export bookings CSV</a>
</form>
<h2>Revenue</h2>
<div class="dg">
 <div class="dc">Today<b>&#8377;{{ revenue.daily|default:0 }}</b></div>
 <div class="dc">This week<b>&#8377;{{ revenue.weekly|default:0 }}</b></div>
 <div class="dc">This month<b>&#8377;{{ revenue.monthly|default:0 }}</b></div>
 <div class="dc">This year<b>&#8377;{{ revenue.yearly|default:0 }}</b></div>
 <div class="dc">Selected range<b>&#8377;{{ revenue.selected|default:0 }}</b>{{ revenue.selected_bookings }} bookings</div>
 <div class="dc">Users<b>{{ users.total }}</b>{{ users.new }} new in range</div>
</div>
<h2>Cancellations, refunds and payments</h2>
<div class="dg">
 <div class="dc">Cancelled<b>{{ cancel.cancelled }}</b></div>
 <div class="dc">Refunded<b>{{ cancel.refunded }}</b>&#8377;{{ cancel.refunded_amount|default:0 }}</div>
 <div class="dc">Cancellation rate<b>{{ cancel.rate }}%</b></div>
 <div class="dc">Payments OK<b>{{ pay.success }}</b>of {{ pay.total }}</div>
 <div class="dc">Failed / cancelled<b>{{ pay.failed }} / {{ pay.cancelled }}</b></div>
 <div class="dc">Refunded payments<b>{{ pay.refunded }}</b></div>
</div>
<div class="sec"><h2>Booking trend</h2><table width="100%"><tr><th>Day</th><th>Bookings</th><th>Revenue</th><th></th></tr>
{% for r in trend %}<tr><td>{{ r.day }}</td><td>{{ r.bookings }}</td><td>{{ r.revenue }}</td><td width="35%"><div class="bar" style="width:{{ r.pct }}%"></div></td></tr>{% empty %}<tr><td colspan="4">No data</td></tr>{% endfor %}</table></div>
<div class="sec"><h2>Peak booking hours</h2><table width="100%"><tr><th>Hour</th><th>Bookings</th><th></th></tr>
{% for r in peak %}<tr><td>{{ r.hour }}</td><td>{{ r.n }}</td><td width="50%"><div class="bar" style="width:{{ r.pct }}%"></div></td></tr>{% endfor %}</table></div>
<div class="sec"><h2>Most booked movies</h2><table width="100%"><tr><th>Movie</th><th>Bookings</th><th>Revenue</th><th></th></tr>
{% for r in top_movies %}<tr><td>{{ r.movie__name }}</td><td>{{ r.n }}</td><td>{{ r.revenue }}</td><td width="25%"><div class="bar" style="width:{{ r.pct }}%"></div></td></tr>{% empty %}<tr><td colspan="4">No data</td></tr>{% endfor %}</table></div>
<div class="sec"><h2>Top-performing theaters</h2><table width="100%"><tr><th>Theater</th><th>City</th><th>Bookings</th><th>Revenue</th><th></th></tr>
{% for r in top_theaters %}<tr><td>{{ r.theater__name }}</td><td>{{ r.theater__city }}</td><td>{{ r.n }}</td><td>{{ r.revenue }}</td><td width="20%"><div class="bar" style="width:{{ r.pct }}%"></div></td></tr>{% empty %}<tr><td colspan="5">No data</td></tr>{% endfor %}</table></div>
<div class="sec"><h2>Occupancy by theater (shows in range)</h2><table width="100%"><tr><th>Theater</th><th>City</th><th>Booked/Seats</th><th>%</th><th></th></tr>
{% for r in occupancy %}<tr><td>{{ r.theater__name }}</td><td>{{ r.theater__city }}</td><td>{{ r.booked }}/{{ r.total }}</td><td>{{ r.occupancy }}</td><td width="25%"><div class="bar" style="width:{{ r.pct }}%"></div></td></tr>{% empty %}<tr><td colspan="5">No shows in range</td></tr>{% endfor %}</table></div>
<div class="sec"><h2>User growth</h2><table width="100%"><tr><th>Day</th><th>New users</th><th></th></tr>
{% for r in user_growth %}<tr><td>{{ r.day }}</td><td>{{ r.n }}</td><td width="50%"><div class="bar" style="width:{{ r.pct }}%"></div></td></tr>{% empty %}<tr><td colspan="3">No signups</td></tr>{% endfor %}</table></div>
{% endblock %}
"""


# ----------------------------- model admins -----------------------------
class MoviePosterInline(admin.TabularInline):
    model = MoviePoster
    extra = 1


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = ('name', 'age_certification', 'rating', 'release_date', 'is_trending')
    list_filter = ('genres', 'languages', 'is_trending', 'age_certification')
    search_fields = ('name', 'description')
    inlines = [MoviePosterInline]


@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ('name', 'city', 'screen', 'movie', 'time', 'price')
    list_filter = ('city', 'name', 'time')
    search_fields = ('name', 'movie__name')


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ('theater', 'seat_number', 'is_booked')
    list_filter = ('is_booked', 'theater')
    search_fields = ('seat_number', 'theater__name')


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'seat', 'movie', 'theater', 'status', 'amount', 'booked_at')
    list_filter = ('status', 'booked_at', 'theater')
    search_fields = ('user__username', 'movie__name', 'seat__seat_number')
    actions = ['cancel_and_refund']

    def get_urls(self):
        custom = [path('dashboard/', self.admin_site.admin_view(self.dashboard_view), name='movies_dashboard')]
        return custom + super().get_urls()     # custom first so it isn't swallowed by <object_id>/

    def dashboard_view(self, request):
        u = request.user
        if not (u.is_active and u.is_staff and u.has_perm('movies.view_booking')):
            raise PermissionDenied
        today = timezone.localdate()
        start = _d(request.GET.get('start'), today - timedelta(days=29))
        end = _d(request.GET.get('end'), today)
        if start > end:
            start, end = end, start
        if request.GET.get('export') == 'bookings':
            return bookings_csv(start, end)
        data = collect(start, end)
        if request.GET.get('export') == 'summary':
            return summary_csv(data, start, end)
        ctx = {**self.admin_site.each_context(request), 'title': 'Business dashboard',
               'start': start, 'end': end, **data}
        return HttpResponse(engines['django'].from_string(DASHBOARD_TEMPLATE).render(ctx, request))

    @admin.action(description='Cancel and refund selected bookings')
    def cancel_and_refund(self, request, queryset):
        from .views import razorpay_client
        done = 0
        for b in queryset.filter(status='confirmed').select_related('payment'):
            with transaction.atomic():
                b.status, b.cancelled_at = 'refunded', timezone.now()
                b.save(update_fields=['status', 'cancelled_at'])
                Seat.objects.filter(pk=b.seat_id).update(is_booked=False, reserved_by=None, reserved_until=None)
            if b.payment and b.payment.razorpay_payment_id:
                try:
                    razorpay_client().payment.refund(b.payment.razorpay_payment_id,
                                                     {'amount': int(b.amount * 100)})
                except Exception as exc:
                    self.message_user(request, f'Booking {b.id}: gateway refund failed ({exc})', messages.WARNING)
            done += 1
        self.message_user(request, f'{done} booking(s) cancelled and refunded.')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('booking_ref', 'user', 'theater', 'amount', 'status', 'razorpay_payment_id', 'email_sent', 'created_at')
    list_filter = ('status', 'email_sent', 'created_at')
    search_fields = ('razorpay_order_id', 'razorpay_payment_id', 'user__username')
    readonly_fields = [f.name for f in Payment._meta.fields]
    actions = ['resend_ticket']

    @admin.action(description='Re-send ticket email')
    def resend_ticket(self, request, queryset):
        from .views import queue_ticket_email
        for p in queryset.filter(status='success'):
            Payment.objects.filter(pk=p.pk).update(email_sent=False)
            queue_ticket_email(p.pk)
        self.message_user(request, 'Ticket emails queued.')


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('movie', 'user', 'rating', 'is_verified_viewer', 'is_reported', 'created_at')
    list_filter = ('is_reported', 'is_verified_viewer', 'rating')
    search_fields = ('movie__name', 'user__username', 'comment')


admin.site.register(RecentlyViewed)
admin.site.register(Genre)
admin.site.register(Language)
admin.site.register(CastMember)