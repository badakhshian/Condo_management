"""Vercel serverless entry point: exposes the Flask WSGI app as `app`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from condo import create_app  # noqa: E402

app = create_app()
