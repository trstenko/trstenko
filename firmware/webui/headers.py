"""Headers that go on every response (plan 07, "Security").

- **Content-Security-Policy**: nothing on this page may load from anywhere but
  the device itself, so no third-party script can ever run in it, and a page
  that holds an API key during setup cannot be made to send it elsewhere.
  Inline styles are allowed because the page sets bar widths that way; inline
  scripts are not, which is why the page's script lives in its own file.
- **X-Content-Type-Options**: a file is the type the device says it is.
- **Referrer-Policy**: the device's address is not worth telling a link's
  destination.

There are no cookies. The token travels in the Authorization header, so
cross-site request forgery has nothing to ride on -- except `/api/auth`,
which needs no token. Any web page the owner visits can send it a "simple"
cross-origin POST (`text/plain`, no preflight) and burn through the PIN
lockout. A body therefore has to say it is JSON: a browser will only send
that header cross-origin after a preflight, and this server answers none.
"""

SECURITY_HEADERS = {
    "Content-Security-Policy":
        "default-src 'self'; style-src 'self' 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# The biggest legitimate request is a settings patch or a reminder, a few
# hundred bytes. Anything near this is somebody else's idea of a request, and
# it is read before the token is checked.
MAX_BODY = 64 * 1024

# The one exception: a `.trsttool` file on its way in (plan 10). The file is at
# most 256 KB and travels as base64 inside JSON, which is a third larger.
TOOL_PATHS = ("/api/v1/tools", "/api/v1/tools/preview")
MAX_TOOL_BODY = 352 * 1024


def max_body(method, path):
    """The largest body `method path` may carry."""
    if method == "POST" and "/" + path.strip("/") in TOOL_PATHS:
        return MAX_TOOL_BODY
    return MAX_BODY


def json_body_ok(content_type, raw):
    """Whether a request body may be read. An empty body needs no type; any
    other must be declared `application/json`."""
    if not (raw or b"").strip():
        return True
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    return kind == "application/json"
