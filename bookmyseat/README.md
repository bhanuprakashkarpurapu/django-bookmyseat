# BookMySeat

## Setup
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver

## Admin
URL: http://127.0.0.1:8000/admin/
Dashboard: http://127.0.0.1:8000/admin/movies/booking/dashboard/
Username: (your superuser username)
Password: (your superuser password)

## Test modes
- PAYMENT_MODE=mock (default): simulated payment gateway, no real money.
  For real Razorpay, set PAYMENT_MODE=razorpay plus RAZORPAY_KEY_ID,
  RAZORPAY_KEY_SECRET and RAZORPAY_WEBHOOK_SECRET.
- CELERY_EAGER=1: runs email tasks inline when Redis isn't available.
  With Redis: run `celery -A bookmyseat worker -l info --pool=solo`
  and leave CELERY_EAGER unset.