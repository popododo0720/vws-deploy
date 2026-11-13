"""Origin and redirect policy for authenticated live PVE requests."""
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler


class Refused(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def origin(value):
    u = urlsplit(value)
    if (not u.hostname or u.username or u.password or u.path or u.query or u.fragment
            or not u.port and u.netloc.endswith(":") or
            u.scheme != "https" and not (u.scheme == "http" and u.hostname in
                                         ("127.0.0.1", "::1", "localhost"))):
        raise Refused("endpoint must be an HTTPS origin (HTTP only on loopback)")
    return value
