#!/usr/bin/env python3
"""A browser for CI: sign in at the dev realm's login form and follow the redirect.

``collab-hub login`` opens its sign-in URL through Python's ``webbrowser``,
which runs the command in ``BROWSER`` when it is set. Pointing ``BROWSER`` at
this script lets CI run the real sign-in end to end, with no browser: the
authorization request, Keycloak's login form, the redirect to the CLI's
loopback listener, and the code exchange with PKCE::

    BROWSER="python3 scripts/keycloak_login_browser.py %s" collab-hub --hub http://127.0.0.1:8000 login

The user comes from ``KC_USER`` and ``KC_PASS`` (``dev``/``dev``, the dev
realm's user). Stdlib only, and never for anything but the dev realm.
"""

from __future__ import annotations

import html
import os
import re
import sys
import urllib.parse
import urllib.request


def main(url: str) -> int:
    with urllib.request.urlopen(url, timeout=30) as response:
        page = response.read().decode()
        # Keycloak marks its login cookies Secure even on http://localhost, which a
        # browser treats as secure and http.cookiejar does not: carry them by hand.
        cookies = "; ".join(header.split(";", 1)[0] for header in response.headers.get_all("Set-Cookie") or [])
    form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page)
    if not form:
        print("keycloak_login_browser: no login form on the sign-in page", file=sys.stderr)
        return 1
    fields = urllib.parse.urlencode({
        "username": os.environ.get("KC_USER", "dev"),
        "password": os.environ.get("KC_PASS", "dev"),
        "credentialId": "",
    }).encode()
    # Keycloak answers with a redirect to the CLI's loopback listener, which
    # urlopen follows: that request is the sign-in arriving.
    request = urllib.request.Request(html.unescape(form.group(1)), data=fields, headers={"Cookie": cookies})
    response = urllib.request.urlopen(request, timeout=30)
    if not response.url.startswith("http://127.0.0.1:"):
        print(f"keycloak_login_browser: the sign-in ended at {response.url}, not the loopback", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
