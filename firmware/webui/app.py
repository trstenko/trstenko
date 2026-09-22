"""The Flask app the device runs.

Thin on purpose: every decision is in `api.py`, and this maps HTTP onto it.
The same endpoints are served by `server.py` from the standard library, which
is what the emulator uses -- keeping the logic out of the framework is what
lets both exist.

Flask is an optional import. A device whose virtualenv is broken should still
boot and show a face, so `firmware.webui` never imports this at module level.
"""

import json

from .api import UpdateApi


def create_app(manager, device=None, auth=None):
    from flask import Flask, Response, request, send_from_directory

    from .server import STATIC

    api = UpdateApi(manager, device, auth)
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name)

    @app.route("/api/<path:rest>", methods=["GET", "POST"])
    def api_route(rest):
        body = request.get_json(silent=True) if request.method == "POST" else {}
        header = request.headers.get("Authorization", "")
        token = (header[7:].strip() if header.lower().startswith("bearer ")
                 else request.headers.get("X-Trstenko-Token", ""))
        status, payload = api.dispatch(request.method, "/api/" + rest, body,
                                       token)
        return Response(json.dumps(payload), status=status,
                        mimetype="application/json",
                        headers={"Cache-Control": "no-store"})

    return app
