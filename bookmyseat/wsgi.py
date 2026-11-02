
import os
import sys

# Add the inner project directory to Python's import path
PROJECT_DIR = os.path.dirname(__file__)
sys.path.insert(0, PROJECT_DIR)

# Load Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bookmyseat.settings")

# Import the actual Django WSGI application
from bookmyseat.wsgi import application
