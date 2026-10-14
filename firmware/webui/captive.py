"""The captive portal: making a phone open the setup page by itself.

A phone that joins a Wi-Fi asks a well-known address on the internet whether it
is really online (`connectivitycheck.gstatic.com`, `captive.apple.com`,
`www.msftconnecttest.com`, and others). On the setup hotspot there is no
internet, so left alone the phone says "no internet" and offers to leave.

Two things turn that into "sign in to this network", which opens the page:

1. Every name resolves to the device. That is a line of dnsmasq configuration
   for NetworkManager's shared mode (`firmware/deploy/trstenko-captive.conf`),
   so it only ever applies to the hotspot.
2. A request for a host that is not the device is answered with a redirect to
   the setup page. That is this module.

It only happens while setup mode has the hotspot up. On the home Wi-Fi the
device is an ordinary web server and redirects nothing.

The rule is about the `Host` header and not about a list of probe paths: the
vendors add and change those, and any request for somebody else's host that
reaches this device is a probe or a lost page either way.

TODO on the board: try an iPhone, an Android phone and a Windows laptop. Each
should offer to sign in after joining from the QR code, and the page it opens
should be able to pair and finish the Wi-Fi step.
"""

import ipaddress
import socket

PORTAL = "http://10.42.0.1/"

# Hosts that mean this device. Anything else, during setup, is redirected.
OWN_HOSTS = ("10.42.0.1", "trstenko.local", "trstenko", "localhost",
             "127.0.0.1", "[::1]")

SETUP_STATES = ("starting", "waiting", "joining")


def host_of(header):
    """The host from a `Host` header, lower case and without its port."""
    host = (header or "").strip().lower()
    if host.startswith("["):                      # an IPv6 literal
        return host.partition("]")[0] + "]"
    return host.partition(":")[0]


def redirect(host_header, setup_state):
    """Where to send this request, or None to serve it as usual."""
    if setup_state not in SETUP_STATES:
        return None
    host = host_of(host_header)
    if not host or host in OWN_HOSTS:
        return None
    return PORTAL


def _machine_names():
    try:
        name = socket.gethostname().lower()
    except OSError:
        return ()
    return (name, name + ".local") if name else ()


# Endings only a home network's own DNS can answer for: mDNS, the names
# routers hand out (`trstenko.lan`, `trstenko.home`, `trstenko.home.arpa`),
# and endings ICANN has reserved or will never delegate. Nobody can register
# a public name under one of these, so a rebinding page cannot have one.
# `.box` is not here: it is a real top-level domain (`fritz.box` is AVM's).
PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal",
                    ".localdomain", ".corp")


def host_allowed(host_header):
    """Whether a request naming this host is meant for the device, at any
    time, setup or not.

    DNS rebinding is a page on somebody else's site whose name, a moment
    later, resolves to this device: the browser then lets that page talk to
    the device as though it were its own. The browser still sends the page's
    own name in `Host`, so anything that is not an address, one of the
    device's own names, a name with no dot (the router's DNS, `trstenko`) or
    a name under a private ending is refused.
    """
    host = host_of(host_header).rstrip(".")
    if not host:
        return True       # HTTP/1.0; a browser always sends one
    if host in OWN_HOSTS or "." not in host \
            or host.endswith(PRIVATE_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass
    return host in _machine_names()
