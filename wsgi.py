import os
import sys

# PythonAnywhere points at this file, so we expose the Flask app as "application".
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app as application
