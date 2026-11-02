import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import razorpay
from celery import shared_task
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.db import transaction, OperationalError
from django.db.models import Count, F, Min, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (Genre, Language, Movie, Review, Theater, Seat,
                     Booking, Payment, RecentlyViewed)

logger = logging.getLogger(__name__)
HOLD_FOR_PAYMENT = timedelta(minutes=8)


def is_mock():
    return getattr(settings, 'PAYMENT_MODE', 'mock') == 'mock'


# =====================================================================
# TASK 5: DISCOVERY, SEARCH, FILTERS, RECOMMENDATIONS
# =====================================================================
def record_view(user, movie):
    if user.is_authenticated:
        RecentlyViewed.objects.update_or_create(
            user=user, movie=movie, defaults={'viewed_at': timezone.now()})


def _popularity():
    sub = (Booking.objects.filter(movie=OuterRef('pk'), status='confirmed')
           .order_by().values('movie').annotate(c=Count('id')).values('c'))
    return Coalesce(Subquery(sub), 0)


def recommended_movies(user, limit=6):
    booked = list(Booking.objects.filter(user=user, status='confirmed')
                  .values_list('movie_id', flat=True).distinct())
    viewed = list(RecentlyViewed.objects.filter(user=user)
                  .order_by('-viewed_at').values_list('movie_id', flat=True)[:10])
    qs = Movie.objects.exclude(id__in=booked)
    liked = booked + viewed
    top_genres = []
    if liked:
        top_genres = list(
            Genre.objects.filter(movies__id__in=liked)
            .annotate(w=2 * Count('movies', filter=Q(movies__id__in=booked), distinct=True)
                        + Count('movies', filter=Q(movies__id__in=viewed), distinct=True))
            .order_by('-w').values_list('id', flat=True)[:3])
    if top_genres:
        qs = (qs.filter(genres__id__in=top_genres)
              .annotate(score=Count('genres', filter=Q(genres__id__in=top_genres), distinct=True))
              .order_by('-score', '-rating'))
    else:
        qs = qs.annotate(popularity=_popularity()).order_by('-popularity', '-rating')
    return qs.prefetch_related('genres')[:limit]


TIMING_SLOTS = {'morning': (5, 12), 'afternoon': (12, 16), 'evening': (16, 20), 'night': (20, 24)}


def movie_list(request):
    p = request.GET
    f = {k: p.get(k, '').strip() for k in
         ('search', 'genre', 'language', 'city', 'theater', 'timing', 'show_date',
          'release_from', 'release_to', 'min_rating', 'sort')}

    movies = Movie.objects.all()
    if f['search']:
        movies = movies.filter(name__icontains=f['search'])
    if f['genre'].isdigit():
        movies = movies.filter(genres__id=int(f['genre']))
    if f['language'].isdigit():
        movies = movies.filter(languages__id=int(f['language']))
    if f['release_from']:
        movies = movies.filter(release_date__gte=f['release_from'])
    if f['release_to']:
        movies = movies.filter(release_date__lte=f['release_to'])
    if f['min_rating']:
        try:
            movies = movies.filter(rating__gte=Decimal(f['min_rating']))
        except Exception:
            pass

    show = {}
    if f['city']:
        show['theater__city__iexact'] = f['city']
    if f['theater']:
        show['theater__name'] = f['theater']
    if f['timing'] in TIMING_SLOTS:
        lo, hi = TIMING_SLOTS[f['timing']]
        show['theater__time__hour__gte'] = lo
        show['theater__time__hour__lt'] = hi
    if f['show_date']:
        show['theater__time__date'] = f['show_date']
    if show:
        movies = movies.filter(**show)

    movies = movies.annotate(min_price=Min('theater__price'), popularity=_popularity())

    sorts = {
        'popularity': ['-popularity', '-rating', 'name'],
        'newest': [F('release_date').desc(nulls_last=True), 'name'],
        'rating': ['-rating', 'name'],
        'price_low': [F('min_price').asc(nulls_last=True), 'name'],
        'price_high': [F('min_price').desc(nulls_last=True), 'name'],
    }
    movies = movies.order_by(*sorts.get(f['sort'], sorts['popularity']))
    movies = movies.prefetch_related('genres', 'languages', 'extra_posters', 'reviews__user')

    paginator = Paginator(movies, 9)
    page_obj = paginator.get_page(p.get('page'))

    qd = p.copy()
    qd.pop('page', None)

    context = {
        'movies': page_obj,
        'total': paginator.count,
        'f': f,
        'qs': qd.urlencode(),
        'genres': Genre.objects.all(),
        'languages': Language.objects.all(),
        'cities': Theater.objects.order_by().values_list('city', flat=True).distinct(),
        'theater_names': Theater.objects.order_by().values_list('name', flat=True).distinct(),
        'trending_movies': Movie.objects.filter(is_trending=True)[:4],
        'recommended': recommended_movies(request.user) if request.user.is_authenticated else [],
    }
    return render(request, 'movies/movies_list.html', context)


def movie_detail(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    record_view(request.user, movie)
    if request.GET.get('track'):
        return HttpResponse(status=204)
    return redirect('theater_list', movie_id=movie.id)


@login_required(login_url='login/')
def add_review(request, movie_id):
    if request.method == 'POST':
        movie = get_object_or_404(Movie, id=movie_id)
        rating = request.POST.get('rating')
        comment = request.POST.get('comment')
        if not Booking.objects.filter(user=request.user, movie=movie, status='confirmed').exists():
            messages.error(request, "You can only review movies you've booked.")
            return redirect('movie_list')
        review, created = Review.objects.update_or_create(
            movie=movie, user=request.user,
            defaults={'rating': int(rating), 'comment': comment, 'is_verified_viewer': True})
        messages.success(request, "Thank you! Your verified review has been submitted."
                         if created else "Your review has been updated.")
    return redirect('movie_list')


@login_required
def report_review(request, review_id):
    review = get_object_or_404(Review, id=review_id)
    if review.user_id != request.user.id:
        Review.objects.filter(id=review.id).update(is_reported=True)
        messages.success(request, 'Thanks, the review was reported to the admin.')
    return redirect('movie_list')


def theater(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    record_view(request.user, movie)
    theaters = Theater.objects.filter(movie=movie).order_by('time')
    return render(request, 'movies/theater_list.html', {'movie': movie, 'theaters': theaters})


# =====================================================================
# SEAT HOLD
# =====================================================================
@login_required(login_url='/login/')
def book_Seats(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)

    if request.method == 'POST':
        selected_seats = request.POST.getlist('seats')
        now = timezone.now()
        expiry = now + timedelta(minutes=2)

        if not selected_seats:
            return render(request, 'movies/seat_selection.html', {
                'theater': theater, 'seats': Seat.objects.filter(theater=theater),
                'error': 'Please select at least one seat.'})
        try:
            with transaction.atomic():
                seats = list(Seat.objects.filter(theater=theater, id__in=selected_seats)
                             .order_by('id').select_for_update())
                if len(seats) != len(set(selected_seats)):
                    raise ValueError('One or more selected seats are invalid.')

                Seat.objects.filter(theater=theater, reserved_by=request.user, is_booked=False
                                    ).exclude(id__in=selected_seats
                                              ).update(reserved_by=None, reserved_until=None)
                for seat in seats:
                    updated = Seat.objects.filter(id=seat.id, theater=theater, is_booked=False).filter(
                        Q(reserved_by__isnull=True) | Q(reserved_until__lte=now)
                        | Q(reserved_by=request.user, reserved_until__gt=now)
                    ).update(reserved_by=request.user, reserved_until=expiry)
                    if updated != 1:
                        raise ValueError(f'Seat {seat.seat_number} is no longer available.')
        except ValueError as exc:
            return render(request, 'movies/seat_selection.html', {
                'theater': theater, 'seats': Seat.objects.filter(theater=theater), 'error': str(exc)})
        except OperationalError:
            return render(request, 'movies/seat_selection.html', {
                'theater': theater, 'seats': Seat.objects.filter(theater=theater),
                'error': 'The seats are being reserved by another user. Please refresh and try again.'},
                status=409)

        return redirect('checkout', theater_id=theater.id)

    return render(request, 'movies/seat_selection.html',
                  {'theater': theater, 'seats': Seat.objects.filter(theater=theater)})


# =====================================================================
# TASK 3: PAYMENTS (Razorpay, or built-in mock gateway)
# =====================================================================
def razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def release_payment(order_id, status, reason=''):
    """Mark an open payment failed/cancelled and free its seats. Idempotent."""
    with transaction.atomic():
        p = Payment.objects.select_for_update().get(razorpay_order_id=order_id)
        if p.status != 'created':
            return p
        p.status, p.failure_reason = status, reason[:255]
        p.save(update_fields=['status', 'failure_reason', 'updated_at'])
        Seat.objects.filter(id__in=p.seat_id_list, theater=p.theater,
                            reserved_by=p.user, is_booked=False
                            ).update(reserved_by=None, reserved_until=None)
    return p


def captured_payment_id(order_id):
    if is_mock():
        return None
    try:
        for item in razorpay_client().order.payments(order_id).get('items', []):
            if item.get('status') == 'captured':
                return item['id']
    except Exception:
        logger.exception('Could not query Razorpay for order %s', order_id)
    return None


def try_refund(payment):
    if is_mock():
        Payment.objects.filter(pk=payment.pk).update(status='refunded')
        return
    try:
        razorpay_client().payment.refund(payment.razorpay_payment_id,
                                         {'amount': int(payment.amount * 100)})
        Payment.objects.filter(pk=payment.pk).update(status='refunded')
    except Exception:
        logger.exception('Auto refund failed for %s (left as refund_pending)', payment.booking_ref)


def finalize_payment(order_id, razorpay_payment_id, signature=''):
    """
    The ONLY place bookings are created. Idempotent: calling it twice
    never duplicates bookings. Returns (payment, newly_confirmed).
    """
    now = timezone.now()
    with transaction.atomic():
        payment = (Payment.objects.select_for_update()
                   .select_related('theater__movie', 'user').get(razorpay_order_id=order_id))
        if payment.status in ('success', 'refund_pending', 'refunded'):
            return payment, False

        ids = payment.seat_id_list
        seats = list(Seat.objects.select_for_update().filter(id__in=ids, theater=payment.theater).order_by('id'))
        available = len(seats) == len(ids) and all(
            not s.is_booked and (s.reserved_by_id in (None, payment.user_id)
                                 or not s.reserved_until or s.reserved_until <= now)
            for s in seats)

        payment.razorpay_payment_id = razorpay_payment_id or None
        payment.razorpay_signature = signature
        if not available:
            payment.status = 'refund_pending'
            payment.failure_reason = 'Seats were no longer available after payment'
            payment.save()
        else:
            payment.status, payment.paid_at = 'success', now
            payment.save()
            per_seat = (payment.amount / len(seats)).quantize(Decimal('0.01'))
            Booking.objects.bulk_create([
                Booking(user=payment.user, seat=s, movie=payment.theater.movie,
                        theater=payment.theater, status='confirmed', amount=per_seat, payment=payment)
                for s in seats])
            Seat.objects.filter(id__in=ids).update(is_booked=True, reserved_by=None, reserved_until=None)
            transaction.on_commit(lambda: queue_ticket_email(payment.id))

    if payment.status == 'refund_pending':
        try_refund(payment)
        return payment, False
    return payment, True


@login_required
def checkout(request, theater_id):
    theater = get_object_or_404(Theater.objects.select_related('movie'), id=theater_id)
    now = timezone.now()
    with transaction.atomic():
        seats = list(Seat.objects.select_for_update().filter(
            theater=theater, reserved_by=request.user, reserved_until__gt=now, is_booked=False
        ).order_by('id'))
        if not seats:
            messages.error(request, 'Your seat hold expired. Please select seats again.')
            return redirect('book_seats', theater_id=theater.id)
        Seat.objects.filter(id__in=[s.id for s in seats]).update(reserved_until=now + HOLD_FOR_PAYMENT)

    seat_ids = ','.join(str(s.id) for s in seats)
    amount = (theater.price * len(seats)).quantize(Decimal('0.01'))

    Payment.objects.filter(user=request.user, theater=theater, status='created'
                           ).exclude(seat_ids=seat_ids).update(status='cancelled', failure_reason='Superseded')
    payment = Payment.objects.filter(user=request.user, theater=theater, seat_ids=seat_ids,
                                     amount=amount, status='created').first()
    if payment is None:
        if is_mock():
            order_id = f'order_mock_{uuid.uuid4().hex[:16]}'
        else:
            try:
                order = razorpay_client().order.create({
                    'amount': int(amount * 100), 'currency': 'INR',
                    'receipt': f'u{request.user.id}-t{theater.id}-{int(now.timestamp())}'})
                order_id = order['id']
            except Exception:
                logger.exception('Razorpay order creation failed')
                messages.error(request, 'Payment gateway is unavailable. Please try again.')
                return redirect('book_seats', theater_id=theater.id)
        payment = Payment.objects.create(user=request.user, theater=theater, seat_ids=seat_ids,
                                         amount=amount, razorpay_order_id=order_id)

    return render(request, 'movies/seat_selection.html', {
        'theater': theater, 'seats': seats,
        'checkout': {'mock': is_mock(), 'key': settings.RAZORPAY_KEY_ID,
                     'order_id': payment.razorpay_order_id,
                     'amount': amount, 'amount_paise': int(amount * 100)}})


@login_required
@require_POST
def mock_payment(request):
    """Fake gateway used when PAYMENT_MODE='mock'. Uses the same finalize/release logic as the real flow."""
    if not is_mock():
        raise Http404()
    order_id = request.POST.get('order_id', '')
    action = request.POST.get('action', '')
    get_object_or_404(Payment, razorpay_order_id=order_id, user=request.user)

    if action == 'success':
        payment, _ = finalize_payment(order_id, f'pay_mock_{uuid.uuid4().hex[:14]}', 'mock-signature')
        if payment.status == 'success':
            messages.success(request, f'Payment successful. Booking {payment.booking_ref} confirmed. '
                                      'Your ticket will be emailed shortly.')
        else:
            messages.error(request, 'Payment was received but the seats were no longer available. '
                                    'A refund has been initiated.')
    elif action == 'fail':
        release_payment(order_id, 'failed', 'Mock payment failed')
        messages.warning(request, 'Payment failed. Your seats were released. You can retry from your payment history.')
    else:
        release_payment(order_id, 'cancelled', 'Payment cancelled by user')
        messages.warning(request, 'Payment cancelled. Your seats were released. You can retry from your payment history.')
    return redirect('profile')


@login_required
@require_POST
def payment_verify(request):
    order_id = request.POST.get('razorpay_order_id', '')
    payment_id = request.POST.get('razorpay_payment_id', '')
    signature = request.POST.get('razorpay_signature', '')
    get_object_or_404(Payment, razorpay_order_id=order_id, user=request.user)
    try:
        razorpay_client().utility.verify_payment_signature({
            'razorpay_order_id': order_id, 'razorpay_payment_id': payment_id,
            'razorpay_signature': signature})
    except razorpay.errors.SignatureVerificationError:
        release_payment(order_id, 'failed', 'Signature verification failed')
        messages.error(request, 'Payment could not be verified. Your seats were released.')
        return JsonResponse({'ok': False, 'redirect': reverse('profile')}, status=400)

    payment, _ = finalize_payment(order_id, payment_id, signature)
    if payment.status == 'success':
        messages.success(request, f'Payment successful. Booking {payment.booking_ref} confirmed. '
                                  'Your ticket will be emailed shortly.')
    else:
        messages.error(request, 'Payment was received but the seats were no longer available. '
                                'A refund has been initiated.')
    return JsonResponse({'ok': payment.status == 'success', 'redirect': reverse('profile')})


@login_required
@require_POST
def payment_abort(request, kind):
    """kind = 'cancelled' (user closed the popup) or 'failed' (gateway reported failure)."""
    order_id = request.POST.get('order_id', '')
    get_object_or_404(Payment, razorpay_order_id=order_id, user=request.user)
    paid_id = captured_payment_id(order_id)
    if paid_id:
        payment, _ = finalize_payment(order_id, paid_id)
        messages.success(request, f'Payment confirmed. Booking {payment.booking_ref}.')
    else:
        release_payment(order_id, kind, request.POST.get('reason', '') or f'Payment {kind}')
        messages.warning(request, f'Payment {kind}. Your seats were released. '
                                  'You can retry from your payment history.')
    return JsonResponse({'ok': True, 'redirect': reverse('profile')})


@login_required
@require_POST
def retry_payment(request, payment_id):
    old = get_object_or_404(Payment, id=payment_id, user=request.user, status__in=['failed', 'cancelled'])
    now = timezone.now()
    try:
        with transaction.atomic():
            seats = list(Seat.objects.select_for_update().filter(id__in=old.seat_id_list, theater=old.theater))
            if len(seats) != len(old.seat_id_list):
                raise ValueError('Some seats no longer exist.')
            for s in seats:
                ok = Seat.objects.filter(id=s.id, is_booked=False).filter(
                    Q(reserved_by__isnull=True) | Q(reserved_until__lte=now) | Q(reserved_by=request.user)
                ).update(reserved_by=request.user, reserved_until=now + HOLD_FOR_PAYMENT)
                if ok != 1:
                    raise ValueError(f'Seat {s.seat_number} was taken by someone else.')
    except ValueError as exc:
        messages.error(request, f'Cannot retry: {exc} Please pick seats again.')
        return redirect('book_seats', theater_id=old.theater_id)
    return redirect('checkout', theater_id=old.theater_id)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    signature = request.headers.get('X-Razorpay-Signature', '')
    body = request.body.decode('utf-8')
    try:
        razorpay_client().utility.verify_webhook_signature(body, signature, settings.RAZORPAY_WEBHOOK_SECRET)
    except Exception:
        return HttpResponse(status=400)

    event = json.loads(body)
    entity = event.get('payload', {}).get('payment', {}).get('entity', {})
    order_id, pid = entity.get('order_id'), entity.get('id')
    if not order_id or not Payment.objects.filter(razorpay_order_id=order_id).exists():
        return HttpResponse(status=200)

    etype = event.get('event')
    if etype in ('payment.captured', 'order.paid'):
        finalize_payment(order_id, pid)
    elif etype == 'payment.failed' and not captured_payment_id(order_id):
        release_payment(order_id, 'failed', entity.get('error_description') or 'Payment failed')
    return HttpResponse(status=200)


# =====================================================================
# TASK 6: PDF TICKET, EMAIL (CELERY), DOWNLOAD, VERIFY
# =====================================================================
def build_ticket_pdf(payment):
    import qrcode
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A5, landscape
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    t = payment.theater
    seat_numbers = ', '.join(Booking.objects.filter(payment=payment).order_by('seat__id')
                             .values_list('seat__seat_number', flat=True))
    verify_url = f'{settings.SITE_URL}{reverse("ticket_verify", args=[payment.ticket_token])}'

    buf = BytesIO()
    w, h = landscape(A5)
    c = canvas.Canvas(buf, pagesize=(w, h))
    c.setFillColor(colors.HexColor('#212529')); c.rect(0, h - 60, w, 60, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont('Helvetica-Bold', 22); c.drawString(30, h - 38, 'BookMySeat  |  E-Ticket')
    c.setFillColor(colors.black); c.setFont('Helvetica-Bold', 20); c.drawString(30, h - 95, t.movie.name[:45])

    rows = [('Theater', f'{t.name}, {t.city}'), ('Screen', t.screen),
            ('Show time', timezone.localtime(t.time).strftime('%d %b %Y, %I:%M %p')),
            ('Seats', seat_numbers), ('Booking ID', payment.booking_ref),
            ('Payment ref', payment.razorpay_payment_id or '-'), ('Amount paid', f'Rs. {payment.amount}')]
    y = h - 130
    for label, value in rows:
        c.setFont('Helvetica', 10); c.setFillColor(colors.grey); c.drawString(30, y, label.upper())
        c.setFont('Helvetica-Bold', 12); c.setFillColor(colors.black); c.drawString(130, y, str(value)[:48])
        y -= 24

    qr_buf = BytesIO(); qrcode.make(verify_url).save(qr_buf); qr_buf.seek(0)
    c.drawImage(ImageReader(qr_buf), w - 180, h - 240, 150, 150)
    c.setFont('Helvetica', 8); c.setFillColor(colors.grey)
    c.drawString(w - 180, h - 255, 'Scan at the entrance to verify')
    c.setStrokeColor(colors.lightgrey); c.line(30, 40, w - 30, 40)
    c.drawString(30, 26, 'Non-cancellable. Please carry a valid ID. Enjoy the show!')
    c.showPage(); c.save()
    return buf.getvalue()


@shared_task(bind=True, max_retries=5, acks_late=True)
def send_ticket_email(self, payment_id):
    """Runs in the Celery worker, never in the request. Retries with exponential back-off."""
    try:
        payment = Payment.objects.select_related('theater__movie', 'user').get(pk=payment_id)
        if payment.email_sent or payment.status != 'success':
            return 'skipped'
        if not payment.user.email:
            return 'user has no email'
        t = payment.theater
        msg = EmailMessage(
            subject=f'Your ticket for {t.movie.name} ({payment.booking_ref})',
            body=(f'Hi {payment.user.username},\n\nYour booking is confirmed.\n\n'
                  f'Movie: {t.movie.name}\nTheater: {t.name}, {t.city} ({t.screen})\n'
                  f'Show time: {timezone.localtime(t.time):%d %b %Y, %I:%M %p}\n'
                  f'Booking ID: {payment.booking_ref}\n\nYour PDF ticket is attached.\n'),
            from_email=settings.DEFAULT_FROM_EMAIL, to=[payment.user.email])
        msg.attach(f'ticket_{payment.booking_ref}.pdf', build_ticket_pdf(payment), 'application/pdf')
        msg.send(fail_silently=False)
        Payment.objects.filter(pk=payment_id).update(email_sent=True)
        return 'sent'
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60 * 2 ** self.request.retries)


def queue_ticket_email(payment_id):
    """Fire-and-forget. If the broker is down the booking must still succeed."""
    try:
        send_ticket_email.apply_async(args=[payment_id], retry=False)
    except Exception:
        logger.exception('Could not queue ticket email for payment %s', payment_id)


@login_required
def download_ticket(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related('theater__movie', 'user'),
                                id=payment_id, user=request.user, status='success')
    resp = HttpResponse(build_ticket_pdf(payment), content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="ticket_{payment.booking_ref}.pdf"'
    return resp


def ticket_verify(request, token):
    """Target of the QR code. Returns minimal, non-personal info."""
    payment = Payment.objects.select_related('theater__movie').filter(ticket_token=token).first()
    if not payment or payment.status != 'success':
        return JsonResponse({'valid': False}, status=404)
    t = payment.theater
    return JsonResponse({
        'valid': True, 'booking_id': payment.booking_ref, 'movie': t.movie.name,
        'theater': t.name, 'screen': t.screen, 'show_time': timezone.localtime(t.time).isoformat(),
        'seats': list(Booking.objects.filter(payment=payment, status='confirmed')
                      .values_list('seat__seat_number', flat=True))})