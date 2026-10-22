"""Signing in the way the desktop does, keeping the session to its owner and its hub, and signing out."""

from __future__ import annotations

import json
import stat

import httpx
import pytest

from collab_hub_cli import credentials, hub, oidc

from .conftest import CLIENT, HUB, ISSUER, jwt


def _session_file(tmp_path, profile="default"):
    return tmp_path / "config" / "credentials" / f"{profile}.json"


def test_login_signs_in_with_pkce_on_a_loopback_redirect_and_keeps_the_session(stub, cli, tmp_path):
    result = cli("--hub", HUB, "login")
    assert result.exit_code == 0, result.output
    assert f"Signed in to {HUB} as alice in org-a." in result.stderr

    [asked] = stub.authorizations
    assert asked["client_id"] == CLIENT and asked["response_type"] == "code"
    assert asked["code_challenge_method"] == "S256" and asked["scope"] == "openid profile email offline_access"
    assert asked["redirect_uri"].startswith("http://127.0.0.1:") and asked["redirect_uri"].endswith("/callback")
    [exchange] = stub.exchanges  # the stub checked the verifier against the challenge
    assert exchange["redirect_uri"] == asked["redirect_uri"]

    path = _session_file(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    saved = json.loads(path.read_text())
    assert saved["hub"] == HUB and saved["issuer"] == ISSUER
    assert saved["refresh_token"] == stub.issued[0]["refresh_token"]
    assert saved["obtained_by"] == "browser"
    # The hub is remembered, so later commands need no --hub.
    whoami = cli("whoami", "--json")
    assert whoami.exit_code == 0, whoami.output
    me = json.loads(whoami.stdout)
    assert me["user"] == "alice" and me["signed_in"] is True and me["hub"] == HUB
    assert me["token_expires_at"] == "1970-01-12T13:51:40Z"  # clock + 300 s


def test_a_redirect_carrying_another_sign_in_s_state_is_refused(stub, monkeypatch):
    def browser(url):
        import urllib.error
        import urllib.request
        from urllib.parse import parse_qs, urlencode, urlparse

        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        forged = f"{query['redirect_uri']}?{urlencode({'code': 'stolen', 'state': 'not-this-one'})}"
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(forged)
        assert refused.value.code == 400
        stub.browser(approve=False)(url)  # then the real redirect: the user said no

    with httpx.Client(transport=httpx.MockTransport(stub.handle)) as http:
        metadata = oidc.discover(http, ISSUER)
        with pytest.raises(oidc.AuthError, match="did not sign you in: denied"):
            oidc.browser_login(http, metadata, CLIENT, open_browser=browser, show=lambda _url: None, timeout=5)
    assert stub.exchanges == []  # the forged code was never exchanged


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_login_port_fixes_the_listener_so_an_ssh_forward_can_be_set_up_first(stub, cli):
    port = _free_port()
    result = cli("--hub", HUB, "login", "--port", str(port))
    assert result.exit_code == 0, result.output
    [asked] = stub.authorizations
    assert asked["redirect_uri"] == f"http://127.0.0.1:{port}/callback"
    [exchange] = stub.exchanges
    assert exchange["redirect_uri"] == asked["redirect_uri"]


def test_a_login_port_already_in_use_is_a_usage_error(stub, cli):
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        result = cli("--hub", HUB, "login", "--port", str(busy.getsockname()[1]))
    assert result.exit_code == 2 and "cannot listen on 127.0.0.1:" in result.stderr
    assert stub.authorizations == []


def test_a_sign_in_that_never_returns_times_out(stub):
    with httpx.Client(transport=httpx.MockTransport(stub.handle)) as http:
        metadata = oidc.discover(http, ISSUER)
        with pytest.raises(oidc.AuthError, match="no sign-in arrived"):
            oidc.browser_login(http, metadata, CLIENT, open_browser=lambda _url: None, show=lambda _url: None,
                               timeout=0.2)


def test_an_expired_token_is_renewed_before_the_request_and_the_new_one_kept(stub, cli, tmp_path, monkeypatch):
    assert cli("--hub", HUB, "login").exit_code == 0
    monkeypatch.setattr(oidc, "clock", lambda: 1_000_000.0 + 290)  # inside the renewal margin
    result = cli("whoami", "--json")
    assert result.exit_code == 0, result.output
    saved = json.loads(_session_file(tmp_path).read_text())
    renewed = stub.issued[1]
    assert saved["access_token"] == renewed["access_token"] and saved["refresh_token"] == renewed["refresh_token"]
    assert stub.requests[-1].headers["authorization"] == f"Bearer {stub.issued[1]['access_token']}"


def test_a_session_the_realm_will_not_renew_exits_5_and_is_forgotten(stub, cli, tmp_path, monkeypatch):
    assert cli("--hub", HUB, "login").exit_code == 0
    stub.refreshable.clear()  # the realm ended the session
    monkeypatch.setattr(oidc, "clock", lambda: 1_000_000.0 + 3600)
    result = cli("whoami")
    assert result.exit_code == 5
    assert "could not be renewed" in result.stderr
    assert not _session_file(tmp_path).exists()


def test_logout_ends_the_realm_session_revokes_the_token_and_leaves_nothing_on_disk(stub, cli, tmp_path):
    assert cli("--hub", HUB, "login").exit_code == 0
    result = cli("logout", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"hub": HUB, "profile": "default", "was_signed_in": True, "revoked": True,
                                         "warning": None}
    first = stub.issued[0]["refresh_token"]
    assert stub.ended == [{"client_id": CLIENT, "refresh_token": first}]
    # Revocation is what the standard guarantees, so it happens whatever the end-session call did.
    assert stub.revoked == [{"client_id": CLIENT, "token": first, "token_type_hint": "refresh_token"}]
    assert list((tmp_path / "config" / "credentials").iterdir()) == []
    assert cli("whoami").exit_code == 5  # the hub now sees no one


def test_a_realm_that_cannot_revoke_leaves_a_warning_and_exit_1(stub, cli, tmp_path):
    assert cli("--hub", HUB, "login").exit_code == 0
    stub.publishes_revocation = False
    result = cli("logout")
    assert result.exit_code == 1
    assert "no revocation endpoint" in result.stderr and "may still be open" in result.stderr
    assert not _session_file(tmp_path).exists()  # forgotten here all the same


def test_login_json_reports_who_signed_in(stub, cli):
    result = cli("--hub", HUB, "login", "--json")
    assert result.exit_code == 0, result.output
    reported = json.loads(result.stdout)
    assert reported["user"] == "alice" and reported["signed_in"] is True and reported["hub"] == HUB
    assert reported["obtained_by"] == "browser" and reported["token_expires_at"] == "1970-01-12T13:51:40Z"


def test_a_session_is_never_sent_to_another_hub(stub, cli):
    assert cli("--hub", HUB, "login").exit_code == 0
    result = cli("--hub", "https://other.test", "whoami")
    assert result.exit_code == 1  # the stub does not serve it; what matters is what was sent
    [sent] = [r for r in stub.requests if r.url.host == "other.test"]
    assert "authorization" not in sent.headers


def test_login_with_token_reads_stdin_and_keeps_it_only_if_the_hub_accepts_it(stub, cli, tmp_path):
    token = jwt({"sub": "ci", "exp": 1_000_600})
    stub.users[token] = "ci-bot"
    result = cli("--hub", HUB, "login", "--with-token", input=token + "\n")
    assert result.exit_code == 0, result.output
    saved = credentials.load(tmp_path / "config", "default")
    assert saved.obtained_by == "token" and saved.expires_at == 1_000_600 and saved.refresh_token is None
    assert json.loads(cli("whoami", "--json").stdout)["user"] == "ci-bot"

    rejected = cli("--hub", HUB, "--profile", "other", "login", "--with-token", input="not-a-token\n")
    assert rejected.exit_code == 5
    assert credentials.load(tmp_path / "config", "other") is None


def test_an_expired_given_token_is_not_sent(stub, cli, monkeypatch):
    token = jwt({"sub": "ci", "exp": 1_000_600})
    stub.users[token] = "ci-bot"
    assert cli("--hub", HUB, "login", "--with-token", input=token).exit_code == 0
    monkeypatch.setattr(oidc, "clock", lambda: 1_000_600.0)
    result = cli("whoami")
    assert result.exit_code == 5 and "has expired" in result.stderr


def test_against_dev_auth_there_is_nothing_to_sign_in_to_and_whoami_says_so(stub, cli):
    stub.dev_auth, stub.issuer = True, None
    login = cli("--hub", HUB, "login")
    assert login.exit_code == 0 and "runs dev auth" in login.stderr
    assert json.loads(cli("--hub", HUB, "login", "--json").stdout) == {
        "hub": HUB, "profile": "default", "signed_in": False, "dev_auth": True}
    whoami = cli("whoami")
    assert whoami.exit_code == 0
    assert "unauthenticated dev auth" in whoami.stdout and "dev-user" in whoami.stdout
    assert json.loads(cli("whoami", "--json").stdout)["signed_in"] is False


def test_without_a_session_a_real_hub_is_exit_5(stub, cli):
    result = cli("--hub", HUB, "whoami")
    assert result.exit_code == 5
    assert f"not signed in to {HUB}" in result.stderr
    # The hint is a command the CLI accepts: `--hub` belongs to `collab-hub`, before `login`.
    assert f"run `collab-hub --hub {HUB} login`" in result.stderr


def test_without_a_hub_the_hint_puts_hub_before_the_command(cli):
    result = cli("whoami")
    assert result.exit_code != 0 and "`collab-hub --hub URL login`" in result.stderr


def test_a_command_with_no_hub_is_a_usage_error(stub, cli):
    result = cli("whoami")
    assert result.exit_code == 2 and "no hub for profile 'default'" in result.stderr


def test_an_unreachable_hub_is_exit_1(stub, cli, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(hub, "transport", httpx.MockTransport(refuse))
    result = cli("--hub", HUB, "whoami")
    assert result.exit_code == 1 and "cannot reach the hub" in result.stderr


LAUNCHPAD = ('<!doctype html><html lang="en"><head><meta charset="UTF-8" />'
             "<title>Nebari | Launchpad</title></head><body><div id=\"root\"></div></body></html>")


@pytest.mark.parametrize("args", [("login",), ("whoami",), ("cog", "list"), ("run", "list")])
def test_a_web_app_in_front_of_the_hub_is_named_not_a_traceback(cli, monkeypatch, args):
    # A portal that serves its page at every path answers 200 with HTML: the URL is not the hub's API.
    import httpx

    from collab_hub_cli import hub

    page = httpx.MockTransport(lambda request: httpx.Response(200, html=LAUNCHPAD))
    monkeypatch.setattr(hub, "transport", page)
    result = cli("--hub", "https://portal.example.org", *args)
    assert result.exit_code == 1  # EXIT_HUB: the hub failed, as any other hub failure
    assert "https://portal.example.org is not a Collab Hub API" in result.stderr, result.output
    assert 'a web page titled "Nebari | Launchpad"' in result.stderr and "Traceback" not in result.output


def test_a_token_endpoint_that_answers_without_tokens_is_a_realm_error():
    import httpx

    from collab_hub_cli import oidc

    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, html=LAUNCHPAD)))
    metadata = {"token_endpoint": "https://realm.example.org/token"}
    with pytest.raises(oidc.RealmError, match="answered without tokens"):
        oidc.refresh(http, metadata, "apollo-desktop", "refresh-token")


@pytest.mark.parametrize("args", [("login",), ("whoami",), ("run", "list")])
def test_a_web_server_refusing_with_a_page_is_not_the_hub_either(cli, monkeypatch, args):
    # A conventional HTML 404 (or 401) from a host that is not the hub: named the same way as a 200 page.
    import httpx

    from collab_hub_cli import hub

    page = httpx.MockTransport(lambda request: httpx.Response(
        404, html="<html><head><title>404 Not Found</title></head><body>nginx</body></html>"))
    monkeypatch.setattr(hub, "transport", page)
    result = cli("--hub", "https://www.example.org", *args)
    assert result.exit_code == 1
    assert 'https://www.example.org is not a Collab Hub API' in result.stderr, result.output
    assert 'a web page titled "404 Not Found" (HTTP 404)' in result.stderr


def test_a_proxy_error_page_in_front_of_a_failing_hub_keeps_its_status(cli, monkeypatch):
    # A 5xx page says the hub behind the proxy is failing, not that the address is wrong.
    import httpx

    from collab_hub_cli import hub

    page = httpx.MockTransport(lambda request: httpx.Response(
        502, html="<html><head><title>502 Bad Gateway</title></head></html>"))
    monkeypatch.setattr(hub, "transport", page)
    result = cli("--hub", "https://hub.example.org", "whoami")
    assert result.exit_code == 1 and "HTTP 502" in result.stderr and "not a Collab Hub API" not in result.stderr


@pytest.mark.parametrize("token", [None, "", 42])
def test_a_token_endpoint_with_an_unusable_access_token_is_a_realm_error(token):
    import httpx

    from collab_hub_cli import oidc

    http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"access_token": token, "refresh_token": "r"})))
    with pytest.raises(oidc.RealmError, match="answered without tokens"):
        oidc.refresh(http, {"token_endpoint": "https://realm.example.org/token"}, "apollo-desktop", "refresh-token")


TOKEN_ENDPOINT = {"token_endpoint": "https://realm.example.org/token"}


def _answering(body: dict) -> httpx.Client:
    """A realm whose token endpoint answers ``body``, NaN and infinity written as JSON allows them."""
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, content=json.dumps(body, allow_nan=True), headers={"content-type": "application/json"})))


@pytest.mark.parametrize("extra", [
    {"expires_in": ""},
    {"expires_in": "soon"},
    {"expires_in": "-5"},
    {"expires_in": "5.5"},
    {"expires_in": "²"},  # a digit to str.isdigit, and not one int() reads
    {"expires_in": "1" * 100_000},  # more digits than int() reads from a string
    {"expires_in": [5]},
    {"expires_in": -5},
    {"expires_in": True},
    {"expires_in": False},
    {"expires_in": float("nan")},
    {"expires_in": float("inf")},
    {"expires_in": 100**400},  # too large to be a float
    {"refresh_token": 7},
    {"refresh_token": ""},
])
def test_a_token_answer_with_a_malformed_lifetime_or_refresh_token_is_a_realm_error(extra):
    with pytest.raises(oidc.RealmError, match="token endpoint"):
        oidc.refresh(_answering({"access_token": "a", **extra}), TOKEN_ENDPOINT, "apollo-desktop", "refresh-token")


@pytest.mark.parametrize(("expires_in", "lifetime"), [
    (300, 300.0), ("300", 300.0), (12.5, 12.5), (0, 0.0), ("0", 0.0), (None, None),
])
def test_a_token_lifetime_is_a_number_of_seconds_or_digits(expires_in, lifetime, monkeypatch):
    # Zero is a lifetime: a token that has already expired, renewed at once, never one without an expiry.
    monkeypatch.setattr(oidc, "clock", lambda: 1000.0)
    tokens = oidc.refresh(_answering({"access_token": "a", "expires_in": expires_in}), TOKEN_ENDPOINT,
                          "apollo-desktop", "r")
    assert tokens.expires_at == (None if lifetime is None else 1000.0 + lifetime)
