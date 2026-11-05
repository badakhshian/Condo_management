"""WSGI entry point (used by Vercel's Flask runtime and any WSGI server)."""
from condo import create_app

app = create_app()
