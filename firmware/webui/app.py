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
from .headers import MAX_BODY, SECURITY_HEADERS


def create_app(manager, device=None, auth=None):
    from flask import Flask, Response, request, send_from_directory

    from .server import STATIC

    api = UpdateApi(manager, device, auth)
    app = Flask(__name__, static_folder=None)

    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY

    @app.after_request
    def secure(response):
        # On everything, including the static page and the error pages.
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name)

    @app.route("/api/<path:rest>", methods=["GET", "POST", "PATCH", "DELETE"])
    def api_route(rest):
        # Parsed whatever the Content-Type says, as the standard-library
        # server does, so the two cannot disagree about a request.
        body = ({} if request.method == "GET"
                else request.get_json(silent=True, force=True))
        if request.method != "GET" and body is None:
            raw = request.get_data()
            if raw.strip():
                return Response(json.dumps({"error": "body is not JSON"}),
                                status=400, mimetype="application/json",
                                headers={"Cache-Control": "no-store"})
            body = {}
        header = request.headers.get("Authorization", "")
        token = (header[7:].strip() if header.lower().startswith("bearer ")
                 else request.headers.get("X-Trstenko-Token", ""))
        opened = api.stream("/api/" + rest, token)
        if opened is not None:
            status, payload = opened
            if status != 200:
                return Response(json.dumps(payload), status=status,
                                mimetype="application/json")
            return Response(payload, mimetype="text/event-stream",
                            headers={"Cache-Control": "no-store",
                                     "X-Accel-Buffering": "no"})
        status, payload = api.dispatch(request.method, "/api/" + rest, body,
                                       token, request.args.to_dict())
        return Response(json.dumps(payload), status=status,
                        mimetype="application/json",
                        headers={"Cache-Control": "no-store"})

    return app
