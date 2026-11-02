import os
import sys

# Add the inner Django project directory to Python's path
PROJECT_DIR = os.path.dirname(__file__)
sys.path.insert(0, PROJECT_DIR)

# Set Django settings module
os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "bookmyseat.settings"
)

# Initialize Django directly instead of importing another wsgi.py
from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()