"""The web UI the device serves over its own hotspot.

`api.py` holds the endpoints as plain functions of (method, path, body).
`server.py` serves them from the standard library; `app.py` serves the same
thing through Flask on the device. Flask is imported only by `app.py`, so a
device with a broken virtualenv still boots and still shows a face.
"""

from .api import UpdateApi
from .server import serve

__all__ = ["UpdateApi", "serve", "create_app"]


def create_app(manager, device=None):
    """Flask app for the device. Imported late: Flask is not a hard dependency
    of the firmware package."""
    from .app import create_app as _create

    return _create(manager, device)
