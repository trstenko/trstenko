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
from .headers import MAX_TOOL_BODY, SECURITY_HEADERS, json_body_ok, max_body


def create_app(manager, device=None, auth=None):
    from flask import Flask, Response, request, send_from_directory

    import os

    from .server import STATIC, accepts_gzip

    api = UpdateApi(manager, device, auth)
    app = Flask(__name__, static_folder=None)

    # Flask's own limit is the largest any path may send; `api_route` holds
    # each path to its own.
    app.config["MAX_CONTENT_LENGTH"] = MAX_TOOL_BODY

    @app.before_request
    def captive():
        # A phone's "am I online" check, during setup: send it to the page.
        target = api.captive(request.headers.get("Host"))
        if target is not None and request.method in ("GET", "HEAD"):
            return Response(status=302, headers={"Location": target,
                                                 "Cache-Control": "no-store"})
        # Somebody else's host name, outside that redirect: DNS rebinding.
        if not api.host_allowed(request.headers.get("Host")):
            return Response(json.dumps({"error": "this is not that host"}),
                            status=421, mimetype="application/json",
                            headers={"Cache-Control": "no-store"})

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
        # The gzipped copy a release carries, for a browser that takes it.
        if accepts_gzip(request.headers.get("Accept-Encoding")) \
                and not name.endswith(".gz"):
            target = os.path.normpath(os.path.join(STATIC, name + ".gz"))
            if target.startswith(STATIC + os.sep) and os.path.isfile(target):
                response = send_from_directory(STATIC, name + ".gz")
                plain = send_from_directory(STATIC, name)
                response.headers["Content-Type"] = plain.headers[
                    "Content-Type"]
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Vary"] = "Accept-Encoding"
                return response
        return send_from_directory(STATIC, name)

    @app.route("/api/<path:rest>", methods=["GET", "POST", "PATCH", "DELETE"])
    def api_route(rest):
        if (request.content_length or 0) > max_body(request.method,
                                                    "/api/" + rest):
            return Response(json.dumps({"error": "request too large"}),
                            status=413, mimetype="application/json",
                            headers={"Cache-Control": "no-store"})
        # A body must be declared JSON (see headers.py), and is then parsed
        # as the standard-library server does, so the two cannot disagree.
        if request.method != "GET" and not json_body_ok(
                request.headers.get("Content-Type"), request.get_data()):
            return Response(json.dumps(
                {"error": "send the body as application/json"}),
                status=415, mimetype="application/json",
                headers={"Cache-Control": "no-store"})
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
