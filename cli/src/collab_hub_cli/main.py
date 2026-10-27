"""The ``collab-hub`` command line.

Exit codes: 0 success; 1 the hub refused or failed the request, or could not
be reached, or a run that was waited for failed; 2 a usage error; 3 a run that
was waited for ended interrupted; 4 it is waiting at a Gate; 5 not signed in,
or the session cannot be used; 6 a run that was waited for was cancelled.
Messages go to stderr, results to stdout, and ``--json`` makes stdout one JSON
document a script can hand to ``jq``.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import sys
import time
import webbrowser
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

import typer

from . import config, credentials, oidc
from .credentials import Credentials
from .hub import Hub, HubError, http_client
from .oidc import AuthError, RealmError

EXIT_HUB = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 3
EXIT_AT_GATE = 4
EXIT_AUTH = 5
EXIT_CANCELLED = 6

# How a run that was waited for ends the command; a status not listed here is a failure.
RUN_EXIT = {"COMPLETED": 0, "INTERRUPTED": EXIT_INTERRUPTED, "WAITING_AT_GATE": EXIT_AT_GATE,
            "CANCELLED": EXIT_CANCELLED}
DECISIONS = ("approve", "reject", "send_back")
GATE_POLICIES = ("never", "error", "warn", "always")
POLL_SECONDS = 0.5
TURN_TIMEOUT_SECONDS = 300.0

app = typer.Typer(
    name="collab-hub",
    help="A command-line client for the Collab Hub: sign in, then call the hub's REST API.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
cog_app = typer.Typer(help="The Cogs the hub offers.", no_args_is_help=True)
app.add_typer(cog_app, name="cog")
run_app = typer.Typer(help="The runs the hub was asked for: what was launched, and stopping it.",
                      no_args_is_help=True)
app.add_typer(run_app, name="run")

HubOption = Annotated[
    str | None, typer.Option("--hub", envvar="COLLAB_HUB_URL", help="The hub's URL; overrides the profile's.")
]
ProfileOption = Annotated[
    str | None, typer.Option("--profile", envvar="COLLAB_HUB_PROFILE", help="The profile to use.")
]
InsecureOption = Annotated[bool, typer.Option(
    "--insecure", help="Accept plain http for a hub or realm off this machine: tokens then cross in clear.")]
JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON document for scripts.")]


class State:
    hub: str | None = None
    profile: str | None = None
    insecure: bool = False


state = State()


@app.callback()
def main(hub: HubOption = None, profile: ProfileOption = None, insecure: InsecureOption = False) -> None:
    state.hub, state.profile, state.insecure = hub, profile, insecure


def _err(message: str) -> None:
    typer.echo(message, err=True)


def handled(command: Callable) -> Callable:
    """Turn the CLI's own failures into a message on stderr and their exit code."""

    @wraps(command)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return command(*args, **kwargs)
        except config.UsageError as exc:
            _err(f"error: {exc}")
            raise typer.Exit(EXIT_USAGE) from None
        except AuthError as exc:
            _err(f"error: {exc}")
            raise typer.Exit(EXIT_AUTH) from None
        except (HubError, RealmError) as exc:
            _err(f"error: {exc}")
            raise typer.Exit(EXIT_HUB) from None

    return wrapper


def _target() -> config.Target:
    return config.resolve(state.hub, state.profile, insecure=state.insecure)


def _program() -> str:
    """This CLI as another shell finds it: the path it was run from.

    Not `collab-hub` by name: a PATH that finds it here may be one only this
    shell has (`uv run` puts its environment first), not the client's.
    """

    own = shutil.which(sys.argv[0])
    return str(Path(own).absolute()) if own else "collab-hub"


def _connect_command(target: config.Target, run_id: str) -> str:
    """What an ACP client starts as its agent to talk to a run: ACP is a command's stdin and stdout, not a URL.

    It names this CLI, its configuration directory when one was chosen, the
    hub and the profile, so it works from any shell; the client runs it as
    given, `toad acp "<it>"` for Toad.
    """

    command = []
    if os.environ.get("COLLAB_HUB_CONFIG_DIR"):
        command += ["env", f"COLLAB_HUB_CONFIG_DIR={target.directory.absolute()}"]
    command += [_program(), "--hub", target.require_hub()]
    if target.profile != config.DEFAULT_PROFILE:
        command += ["--profile", target.profile]
    if target.insecure:
        command.append("--insecure")
    return shlex.join([*command, "run", "connect", run_id])


def _print_json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


def _table(headers: list[str], rows: Iterable[list[Any]]) -> None:
    rows = [["" if cell is None else str(cell) for cell in row] for row in rows]
    widths = [max([len(h)] + [len(row[i]) for row in rows]) for i, h in enumerate(headers)]
    for row in [headers, *rows]:
        typer.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def _when(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _token_expiry(token: str) -> float | None:
    """A JWT's ``exp``, read without verifying it: only to know when to stop sending it."""

    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return None


# --- signing in ------------------------------------------------------------------------------


def _end(session: Credentials, insecure: bool) -> str | None:
    """End a session at its realm, best effort: ``None`` when it ended, else why it may still be open."""

    if not (session.refresh_token and session.issuer and session.client_id):
        return None
    http = http_client()
    try:
        oidc.sign_out(http, oidc.discover(http, session.issuer, insecure), session.client_id, session.refresh_token)
        return None
    except (AuthError, RealmError) as exc:
        return str(exc)
    finally:
        http.close()


def _same_realm_session(old: Credentials, new: Credentials) -> bool:
    """Whether two sessions are one realm session, which ending the old one would end for the new one too.

    A browser that is still signed in to the realm hands a second sign-in the
    same realm session (the same ``sid``). When either ``sid`` is unknown the
    answer is yes, so a session is never ended from under the one replacing it.
    """

    if old.issuer != new.issuer:
        return False
    old_sid = oidc.session_id(old.access_token) or oidc.session_id(old.refresh_token)
    new_sid = oidc.session_id(new.access_token) or oidc.session_id(new.refresh_token)
    return old_sid is None or new_sid is None or old_sid == new_sid


@app.command()
@handled
def login(
    with_token: Annotated[bool, typer.Option(
        "--with-token", help="Read a bearer token from stdin instead of signing in through the browser.")] = False,
    no_browser: Annotated[bool, typer.Option(
        "--no-browser", help="Print the sign-in URL without opening a browser.")] = False,
    port: Annotated[int, typer.Option(
        "--port", min=0, max=65535,
        help="The 127.0.0.1 port the sign-in returns to; 0 lets the system pick. Fix it to forward it over SSH "
             "(ssh -L PORT:127.0.0.1:PORT) when the CLI runs on a remote host.")] = 0,
    as_json: JsonOption = False,
) -> None:
    """Sign in to the hub through its realm, the way the Collab desktop does.

    Opens the realm's sign-in page in a browser on this machine, which sends
    you back to a listener on 127.0.0.1. The session is kept for this profile
    and renewed as it expires. On a hub running dev auth there is nothing to
    sign in to, and the command says so.
    """

    target = _target()
    url = target.require_hub()
    previous = credentials.load(target.directory, target.profile)
    with Hub(target) as hub:
        auth = hub.get_json("/v1/auth/cli", authenticate=False)
        issuer, client_id = auth.get("issuer"), auth["client_id"]

        if with_token:
            token = sys.stdin.read().strip()
            if not token:
                raise config.UsageError("--with-token reads the token from stdin, and stdin was empty")
            session = Credentials(hub=url, access_token=token, issuer=issuer, client_id=client_id,
                                  expires_at=_token_expiry(token), obtained_by="token")
        elif issuer is None:
            if not auth.get("dev_auth"):
                raise HubError(f"{url} verifies no bearer tokens and runs no dev auth: there is no way to sign in")
            config.remember(target)
            _err(f"{url} runs dev auth: there is nothing to sign in to, and requests are answered as its "
                 "development user without a token.")
            if as_json:
                _print_json({"hub": url, "profile": target.profile, "signed_in": False, "dev_auth": True})
            return
        else:
            metadata = oidc.discover(hub.http, issuer, target.insecure)

            def show(sign_in_url: str) -> None:
                _err(f"Sign in to {url} in your browser:\n\n  {sign_in_url}\n")
                if no_browser:
                    _err("Open it in a browser on this machine: it returns to a listener on 127.0.0.1.")

            tokens = oidc.browser_login(hub.http, metadata, client_id,
                                        open_browser=(lambda _url: None) if no_browser else webbrowser.open,
                                        show=show, port=port)
            session = Credentials(hub=url, access_token=tokens.access_token, issuer=issuer, client_id=client_id,
                                  refresh_token=tokens.refresh_token, expires_at=tokens.expires_at)

        # Keep the session only once the hub accepts it; a browser session it
        # refused is ended rather than left open at the realm with nothing holding it.
        hub.session = session
        try:
            me = hub.get_json("/v1/me")
        except (AuthError, HubError, RealmError):
            if session.obtained_by == "browser":
                _end(session, target.insecure)
            raise
        credentials.save(target.directory, target.profile, session)
        config.remember(target)
    # The session this profile held before is replaced, so end it at its realm, unless it is
    # the same realm session the new one belongs to. A failure warns and never undoes the sign-in.
    ended_previous, warning = False, None
    if previous is not None and previous.refresh_token and not _same_realm_session(previous, session):
        warning = _end(previous, target.insecure)
        ended_previous = warning is None
        if warning:
            warning = f"the previous session ({previous.hub}) may still be open at its realm: {warning}"
            _err(f"warning: {warning}")
    organization = f" in {me['org_id']}" if me.get("org_id") else ""
    _err(f"Signed in to {url} as {me['user']}{organization}.")
    if as_json:
        _print_json({**me, "hub": url, "profile": target.profile, "signed_in": True, "dev_auth": False,
                     "obtained_by": session.obtained_by, "token_expires_at": _when(session.expires_at),
                     "previous_session_ended": ended_previous, "warning": warning})


@app.command()
@handled
def logout(as_json: JsonOption = False) -> None:
    """Sign out: end the realm session, revoke its refresh token, and forget the stored token."""

    target = _target()
    session = credentials.load(target.directory, target.profile)
    if session is None:
        _err(f"Not signed in (profile {target.profile}).")
        if as_json:
            _print_json({"hub": target.hub, "profile": target.profile, "was_signed_in": False, "revoked": False,
                         "warning": None})
        return
    problem = None
    revoked = False
    if session.refresh_token and session.issuer and session.client_id:
        failure = _end(session, target.insecure)
        revoked = failure is None
        if failure:
            problem = f"{failure}; the stored token is deleted, but the realm session may still be open"
    elif session.obtained_by == "token":
        problem = "the token was given to `login --with-token`; it is forgotten here and stays valid until it expires"
    credentials.delete(target.directory, target.profile)
    _err(f"Signed out of {session.hub}.")
    if problem:
        _err(f"warning: {problem}")
    if as_json:
        _print_json({"hub": session.hub, "profile": target.profile, "was_signed_in": True, "revoked": revoked,
                     "warning": problem})
    if problem and session.obtained_by != "token":
        raise typer.Exit(EXIT_HUB)


@app.command()
@handled
def whoami(as_json: JsonOption = False) -> None:
    """Who the hub says you are: the user, the organization and its roles, and how you signed in."""

    target = _target()
    with Hub(target) as hub:
        me = hub.get_json("/v1/me")
        session = hub.session
    signed_in = me["authenticated_by"] == "token"
    expires_at = session.expires_at if session is not None and signed_in else None
    if as_json:
        _print_json({**me, "hub": hub.url, "profile": target.profile, "signed_in": signed_in,
                     "token_expires_at": _when(expires_at)})
        return
    organization = me.get("org_id") or "none"
    if me.get("org_role"):
        organization += f" ({me['org_role']})"
    rows = [["hub", f"{hub.url}  (profile {target.profile})"], ["user", me["user"]]]
    if me.get("name") or me.get("email"):
        rows.append(["name", " ".join(part for part in (me.get("name"), f"<{me['email']}>" if me.get("email")
                                                          else None) if part)])
    rows.append(["organization", organization])
    if me.get("platform_role"):
        rows.append(["platform role", me["platform_role"]])
    if signed_in:
        rows.append(["signed in", f"yes, token expires {_when(expires_at)}" if expires_at else "yes"])
    else:
        rows.append(["signed in", "no: unauthenticated dev auth, the hub answers every request as this user"])
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"{label.ljust(width)}  {value}")


# --- the Cogs the hub offers ------------------------------------------------------------------


@cog_app.command("list")
@handled
def cog_list(
    kind: Annotated[str | None, typer.Option(help="Only Cogs of this kind, e.g. complete or context.")] = None,
    publisher: Annotated[str | None, typer.Option(help="Only this publisher's Cogs.")] = None,
    provides: Annotated[str | None, typer.Option(help="Only Cogs that provide this.")] = None,
    requires: Annotated[str | None, typer.Option(help="Only Cogs that require this capability.")] = None,
    accepts: Annotated[str | None, typer.Option(help="Only Cogs that accept this io type.")] = None,
    produces: Annotated[str | None, typer.Option(help="Only Cogs that produce this io type.")] = None,
    source_id: Annotated[str | None, typer.Option(
        "--source-id", help="Only this registry source; the newest version is chosen within it.")] = None,
    query: Annotated[str | None, typer.Option("--query", "-q", help="Text in the name or description.")] = None,
    launchable: Annotated[bool, typer.Option(
        "--launchable", help="The Cogs this hub can launch now, instead of the catalog.")] = False,
    as_json: JsonOption = False,
) -> None:
    """List the Cogs in the hub's catalog, each at its newest version, following every page.

    With --launchable, the Cog packages the hub's run controller can launch,
    which is what `cog launch` takes until Cogs are installed from the catalog.
    """

    if launchable:
        with Hub(_target()) as hub:
            names = hub.get_json("/v1/runs/launchable")["items"]
        if as_json:
            _print_json(names)
        elif names:
            typer.echo("\n".join(names))
        else:
            _err("This hub launches no Cogs.")
        return
    filters = {"kind": kind, "publisher": publisher, "provides": provides, "requires": requires,
               "accepts": accepts, "produces": produces, "source_id": source_id, "q": query}
    with Hub(_target()) as hub:
        items = list(hub.pages("/v1/cogs", {key: value for key, value in filters.items() if value is not None}))
    if as_json:
        _print_json(items)
        return
    if not items:
        _err("The catalog has no Cogs.")
        return
    _table(["COG", "VERSION", "KIND", "DESCRIPTION"], [
        [item["cog_id"], item.get("version") or item["digest"][:19], item["card"].get("kind"),
         _clip(item["card"].get("description"))]
        for item in items
    ])


def _clip(text: Any, width: int = 60) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


@cog_app.command("show")
@handled
def cog_show(cog_id: Annotated[str, typer.Argument(help="The Cog's id, <publisher>/<name>.")],
             as_json: JsonOption = False) -> None:
    """Show one Cog: its card at the newest version, and every indexed version."""

    with Hub(_target()) as hub:
        cog = hub.get_json(f"/v1/cogs/{cog_id}")
    if as_json:
        _print_json(cog)
        return
    card = cog.get("card", {})
    rows = [["cog", cog["cog_id"]], ["name", card.get("name")], ["kind", card.get("kind")],
            ["publisher", card.get("publisher")], ["version", cog.get("version")], ["digest", cog["digest"]],
            ["reference", cog["reference"]], ["description", _clip(card.get("description"), 100)]]
    rows = [row for row in rows if row[1]]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"{label.ljust(width)}  {value}")
    typer.echo("")
    _table(["VERSION", "DIGEST", "TAGS", "REMOVED"], [
        [version.get("version"), version["digest"][:19], ",".join(version.get("tags", [])),
         "yes" if version.get("removed_at") else ""]
        for version in cog.get("versions", [])
    ])


# --- launching a Cog, and the runs that makes ---------------------------------------------------


def _age(timestamp: str) -> str:
    seconds = max(0, int((datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds()))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _print_run(run: dict) -> None:
    rows = [["run", run["id"] + (f"  ({run['name']})" if run.get("name") else "")], ["status", run["status"]],
            ["runs on", f"backend {run['backend']}, workers {run['location']}"],
            ["submitted", f"{run['submitted_at']} by {run.get('submitted_by_name') or run['submitted_by']}"]]
    if run.get("cancel_requested_by") and not run["ended"]:
        rows.append(["cancel", f"requested by {run['cancel_requested_by']}"])
    if run.get("error"):
        rows.append(["error", f"{run['error']}: {run['reason']}" if run.get("reason") else run["error"]])
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"{label.ljust(width)}  {value}")
    typer.echo("")
    _table(["STEP", "COG", "ENTRY", "STATE", "ERROR"], [
        [step["name"], step["cog"], step["entry_point"], step["state"], step.get("error")] for step in run["steps"]])
    for step in run["steps"]:
        if step.get("output") is not None:
            typer.echo(f"\n{step['name']} answered:")
            typer.echo(json.dumps(step["output"], indent=2, sort_keys=True))
        elif step.get("output_ref"):
            typer.echo(f"\n{step['name']} answered with a result too large to show here ({step['output_ref']}).")


def _follow(hub: Hub, run: dict, until: Callable[[dict], bool], *, quiet: bool) -> dict:
    """Ask the hub for the run until ``until`` holds, saying on stderr each status it passes through."""

    said = run["status"]
    while not until(run):
        time.sleep(POLL_SECONDS)
        run = hub.get_json(f"/v1/runs/{run['id']}")
        if run["status"] != said and not quiet:
            _err(f"{run['id']}: {run['status']}")
        said = run["status"]
    return run


def _settled(run: dict) -> bool:
    """A run that will not move on its own: it has ended, or it waits at a Gate for a decision."""

    return run["ended"] or run["status"] == "WAITING_AT_GATE"


def _finish(run: dict, as_json: bool) -> None:
    """Print a run that was waited for, and exit with the code its status maps to."""

    if as_json:
        _print_json(run)
    else:
        _print_run(run)
    code = RUN_EXIT.get(run["status"], EXIT_HUB)
    if code:
        raise typer.Exit(code)


@cog_app.command("launch")
@handled
def cog_launch(
    name: Annotated[str, typer.Argument(help="The Cog to launch: a package the hub's controller can run.")],
    entry: Annotated[str, typer.Option("--entry", help="The Cog's entry point to invoke.")] = "run",
    input_json: Annotated[str | None, typer.Option(
        "--input", help="The step's input, as JSON; `-` reads it from stdin.")] = None,
    gate: Annotated[str, typer.Option(
        "--gate", help="When the step's Gate asks a person: never, error (the default), warn or always.")] = "error",
    watch: Annotated[bool, typer.Option(
        "--watch", help="Follow the run until it ends or waits at a Gate, and exit with its outcome.")] = False,
    run_name: Annotated[str | None, typer.Option(
        "--name", help="What to call the run in `run list`, e.g. hermes-on-claude.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Launch a Cog: submit a one-step Op that invokes one of its entry points.

    The hub records the run and a run controller starts it; the command prints
    the run and returns, or with --watch follows it to its end.
    """

    if gate not in GATE_POLICIES:
        raise config.UsageError(f"--gate is one of {', '.join(GATE_POLICIES)}, not {gate!r}")
    raw = sys.stdin.read() if input_json == "-" else input_json
    try:
        value = None if raw is None or not raw.strip() else json.loads(raw)
    except ValueError as exc:
        raise config.UsageError(f"--input is not JSON: {exc}") from None
    step = {"name": name.split("/")[-1], "cog": name, "entry_point": entry, "input": value,
            "gate": {"escalate": gate}}
    with Hub(_target()) as hub:
        body = {"steps": [step], **({"name": run_name} if run_name else {})}
        run = hub.request_json("POST", "/v1/runs", json=body)
        called = f" ({run_name})" if run_name else ""
        _err(f"Launched {name} as {run['id']}{called} on the {run['backend']} backend, workers {run['location']}.")
        if not watch:
            if as_json:
                _print_json(run)
            else:
                typer.echo(run["id"])
            return
        run = _follow(hub, run, _settled, quiet=False)
    _finish(run, as_json)


@run_app.command("list")
@handled
def run_list(
    status_filter: Annotated[str | None, typer.Option(
        "--status", help="Only runs in this status, e.g. running or completed.")] = None,
    as_json: JsonOption = False,
) -> None:
    """List the runs your organization launched, newest first."""

    params = {"status": status_filter} if status_filter else {}
    target = _target()
    with Hub(target) as hub:
        runs = list(hub.pages("/v1/runs", params))
    if as_json:
        _print_json(runs)
        return
    if not runs:
        _err("No runs.")
        return
    _table(["RUN", "NAME", "COG", "STATUS", "AGE", "BY", "CONNECT"], [
        [run["id"], run.get("name"), ",".join(dict.fromkeys(step["cog"] for step in run["steps"])), run["status"],
         _age(run["submitted_at"]), run.get("submitted_by_name") or run["submitted_by"],
         None if run["ended"] else _connect_command(target, run["id"])]
        for run in runs
    ])


@run_app.command("show")
@handled
def run_show(run_id: Annotated[str, typer.Argument(help="The run's id.")], as_json: JsonOption = False) -> None:
    """Show one run: its status, what it runs on, and each step's state."""

    with Hub(_target()) as hub:
        run = hub.get_json(f"/v1/runs/{run_id}")
    if as_json:
        _print_json(run)
    else:
        _print_run(run)


def _event_line(sequence: int | None, kind: str, data: Any) -> str:
    """One event as `run watch` shows it: when, what, and the fields a reader acts on."""
    payload = (data or {}).get("payload") or {} if isinstance(data, dict) else {}
    when = (data or {}).get("occurred_at", "")[11:19] if isinstance(data, dict) else ""
    said = []
    for key in ("step", "attempt", "controller", "escalation", "outcome", "actor", "error", "reason", "message",
                "turn", "text"):
        if key in payload and payload[key] not in (None, "", []):
            value = payload[key]
            said.append(f"{key}={json.dumps(value) if not isinstance(value, str) else value}")
    return f"{sequence if sequence is not None else '':>6}  {when}  {kind:<22} {' '.join(said)}".rstrip()


RETRY_TAKEN_POLL = 0.5
"""Seconds `run retry --watch` waits between looks at an ended run, for a controller to take the retry asked for."""


def _watch(hub: Hub, run_id: str, *, as_json: bool, after: int = 0) -> dict:
    """Print the run's events as the hub streams them, until the run ends or waits at a Gate; the run after.

    A `gate_escalated` stops the watch only while that escalation is the run's
    open one: an escalation already decided (replayed from before ``after``, or
    answered while the stream was down) is history the run moved past.
    """
    for sequence, kind, data in hub.events(f"/v1/runs/{run_id}/events", after=after):
        if kind == "end":
            break
        if as_json:
            typer.echo(json.dumps({"sequence": sequence, "type": kind,
                                   **(data if isinstance(data, dict) else {"data": data})}))
        else:
            typer.echo(_event_line(sequence, kind, data))
        if kind == "gate_escalated":
            # The run waits at its Gate for a decision: watching would wait with it. Unless the decision landed
            # already, in which case the run moved on and the stream will say how.
            escalation = ((data or {}).get("payload") or {}).get("escalation") if isinstance(data, dict) else None
            current = hub.get_json(f"/v1/runs/{run_id}")
            if current.get("escalation") == escalation and current.get("decision") is None:
                return current
    return hub.get_json(f"/v1/runs/{run_id}")


@run_app.command("watch")
@handled
def run_watch(run_id: Annotated[str, typer.Argument(help="The run's id.")], as_json: JsonOption = False) -> None:
    """Follow a run's events as they land until it ends or waits at a Gate, and exit with its outcome.

    The events come from the hub's stream; a dropped connection is reopened
    from the last event seen, so none is missed or repeated. With --json, one
    event per line, then the run.
    """

    with Hub(_target()) as hub:
        run = _watch(hub, run_id, as_json=as_json)
    _finish(run, as_json)


def _read_op(path: str) -> dict:
    """An Op from a file: YAML or JSON, `-` for stdin; its `steps`, and a `name` when it has one."""
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    try:
        if path.endswith((".yaml", ".yml")) or not raw.lstrip().startswith("{"):
            import yaml

            document = yaml.safe_load(raw)
        else:
            document = json.loads(raw)
    except (ValueError, ImportError) as exc:
        raise config.UsageError(f"{path} is not an Op: {exc}") from None
    if not isinstance(document, dict) or not isinstance(document.get("steps"), list) or not document["steps"]:
        raise config.UsageError(f"{path} is not an Op: it needs a list of steps")
    body = {"steps": []}
    for step in document["steps"]:
        if not isinstance(step, dict) or not step.get("cog") or not step.get("name"):
            raise config.UsageError(f"{path}: each step names its cog and has a name")
        one = {"name": step["name"], "cog": step["cog"], "entry_point": step.get("entry_point", "run"),
               "input": step.get("input")}
        gate = step.get("gate")
        if isinstance(gate, str):
            gate = {"escalate": gate}
        if gate is not None:
            one["gate"] = gate
        if step.get("frames"):
            one["frames"] = list(step["frames"])
        body["steps"].append(one)
    if document.get("name"):
        body["name"] = str(document["name"])
    return body


@run_app.command("submit")
@handled
def run_submit(
    file: Annotated[str, typer.Option("--file", "-f", help="The Op: a YAML or JSON file, or `-` for stdin.")],
    watch: Annotated[bool, typer.Option(
        "--watch", help="Follow the run's events until it ends or waits at a Gate; exit with its outcome.")] = False,
    run_name: Annotated[str | None, typer.Option("--name", help="What to call the run in `run list`.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Submit an Op from a file: several steps, each with its Cog, input, Gate and Frames.

    A step's `frames` are Frame ids: the hub checks each exists and is readable
    by you, snapshots it at submit, and places it beside the step's input.
    """

    body = _read_op(file)
    if run_name:
        body["name"] = run_name
    with Hub(_target()) as hub:
        run = hub.request_json("POST", "/v1/runs", json=body)
        steps = ", ".join(step["name"] for step in run["steps"])
        _err(f"Submitted {run['id']}{' (' + run['name'] + ')' if run.get('name') else ''}: {steps}; "
             f"on the {run['backend']} backend, workers {run['location']}.")
        if not watch:
            if as_json:
                _print_json(run)
            else:
                typer.echo(run["id"])
            return
        run = _watch(hub, run["id"], as_json=as_json)
    _finish(run, as_json)


@run_app.command("payload")
@handled
def run_payload(
    run_id: Annotated[str, typer.Argument(help="The run's id.")],
    step: Annotated[str, typer.Argument(help="The step whose output to read.")],
    as_json: JsonOption = False,
) -> None:
    """Print what a completed step's Cog answered with, or why a failed step failed.

    The output is read back whole, even one the hub keeps by reference.
    """

    with Hub(_target()) as hub:
        answer = hub.get_json(f"/v1/runs/{run_id}/steps/{step}/payload")
    if as_json:
        _print_json(answer)
    elif answer["state"] == "completed":
        typer.echo(json.dumps(answer["payload"], indent=2, sort_keys=True))
    if answer["state"] != "completed":
        error = answer.get("error") or {}
        _err(f"error: step {step} {answer['state']}: {error.get('code', '')} {error.get('message', '')}".rstrip())
        raise typer.Exit(EXIT_HUB)


@run_app.command("decide")
@handled
def run_decide(
    run_id: Annotated[str, typer.Argument(help="The run's id.")],
    approve: Annotated[bool, typer.Option("--approve", help="Accept the result as it is: the step completes.")] = False,
    reject: Annotated[bool, typer.Option("--reject", help="Refuse it: the run ends REJECTED.")] = False,
    send_back: Annotated[bool, typer.Option(
        "--send-back", help="Have the step run again, with the findings as its signal.")] = False,
    findings: Annotated[list[str] | None, typer.Option(
        "--finding", help="A finding to send the step back with; repeat for several. JSON, or plain text.")] = None,
    escalation: Annotated[str | None, typer.Option(
        "--escalation", help="The escalation this decision answers; the run's open one when omitted. A decision "
                             "on an escalation that a send back has since closed is refused as stale.")] = None,
    watch: Annotated[bool, typer.Option(
        "--watch", help="Follow the run's events after the decision until it ends or waits again.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Decide the Gate a run waits at: approve, reject, or send the step back with findings.

    The hub records the decision for the run's controller, which delivers it;
    who may decide is the Gate's approvers. The decision names the escalation
    it answers, so one made on a revision you never saw is refused.
    """

    choices = (("approve", approve), ("reject", reject), ("send_back", send_back))
    outcome = [name for name, chosen in choices if chosen]
    if len(outcome) != 1:
        raise config.UsageError("one of --approve, --reject or --send-back")
    if findings and outcome != ["send_back"]:
        raise config.UsageError("--finding goes with --send-back")
    parsed = []
    for finding in findings or ():
        try:
            parsed.append(json.loads(finding))
        except ValueError:
            parsed.append(finding)
    with Hub(_target()) as hub:
        run = hub.get_json(f"/v1/runs/{run_id}")
        waiting = next((step for step in run["steps"] if step["state"] == "waiting_at_gate"), None)
        if waiting is None or not run.get("escalation"):
            _err(f"error: {run_id} is {run['status']}, and waits on no decision")
            raise typer.Exit(EXIT_HUB)
        seen = run.get("sequence") or 0  # the events up to here are the escalation; the ones after, its delivery
        decision = {"escalation": escalation or run["escalation"], "outcome": outcome[0], "findings": parsed}
        run = hub.request_json("POST", f"/v1/runs/{run_id}/steps/{waiting['name']}/decision", json=decision)
        _err(f"{run_id}: {outcome[0].replace('_', ' ')} of {waiting['name']} recorded for escalation "
             f"{decision['escalation']}; the controller delivers it.")
        if watch:
            run = _watch(hub, run_id, as_json=as_json, after=seen)
            _finish(run, as_json)
    if as_json:
        _print_json(run)
    else:
        typer.echo(run["status"])


@run_app.command("retry")
@handled
def run_retry(
    run_id: Annotated[str, typer.Argument(help="The run's id.")],
    watch: Annotated[bool, typer.Option(
        "--watch", help="Follow the run's events after the retry until it ends or waits at a Gate.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Ask for an ended run to run again, and say how it will: resuming its attempt, or as a new one.

    A run that ended INTERRUPTED resumes the step that was in flight under its
    attempt and key; one that FAILED runs that step again as a new attempt. A
    completed, rejected or cancelled run has nothing to retry, and one that
    exhausted its budget is a new run instead.
    """

    with Hub(_target()) as hub:
        run = hub.request_json("POST", f"/v1/runs/{run_id}/retry")
        was = run["status"]
        how = {"INTERRUPTED": "the step in flight resumes under its attempt and key",
               "FAILED": "the failed step runs again as a new attempt"}.get(was, "it runs again")
        _err(f"{run_id}: retry asked for; it ended {was}, so {how}.")
        if watch:
            # The run ended, so its stream ends at once. Until a controller answers the ask, with
            # `retry_requested` (the run runs on, and the stream with it) or `retry_refused`, the stream is
            # reopened from the last event seen.
            seen = run.get("sequence") or 0
            while True:
                run = _watch(hub, run_id, as_json=as_json, after=seen)
                if not run.get("retry_asked_by"):
                    break
                seen = run.get("sequence") or seen
                time.sleep(RETRY_TAKEN_POLL)
            _finish(run, as_json)
    if as_json:
        _print_json(run)
    else:
        typer.echo(run["status"])


@run_app.command("say")
@handled
def run_say(
    run_id: Annotated[str, typer.Argument(help="The run's id.")],
    text: Annotated[list[str], typer.Argument(help="What to say: one turn of the Cog's session.")],
    timeout: Annotated[float, typer.Option(
        "--timeout", min=1, help="How long to wait for the answer, in seconds.")] = TURN_TIMEOUT_SECONDS,
    as_json: JsonOption = False,
) -> None:
    """Say one thing to a Cog that holds a session, and print what it answered.

    The hub records the turn on the run's Track, the run controller hands it to
    the Cog's worker, and the answer comes back the same way. With no answer
    within --timeout, it says what the run is doing and exits; the turn stays
    on the run, and is still answered if its worker comes up.
    """

    with Hub(_target()) as hub:
        turn = hub.request_json("POST", f"/v1/runs/{run_id}/turns", json={"text": " ".join(text)})
        deadline = time.monotonic() + timeout
        while turn["state"] == "pending" and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            turn = hub.get_json(f"/v1/runs/{run_id}/turns/{turn['turn']}")
        if turn["state"] == "pending":
            status = hub.get_json(f"/v1/runs/{run_id}")["status"]
            _err(f"error: no answer within {timeout:g} seconds; the run is {status}"
                 + (" (is a run controller watching the hub?)" if status == "SUBMITTED" else "")
                 + f". The turn stays on the run: collab-hub run show {run_id}")
            raise typer.Exit(EXIT_HUB)
    if as_json:
        _print_json(turn)
    elif turn["state"] == "answered":
        typer.echo(turn["answer"])
    if turn["state"] != "answered":
        _err(f"error: the Cog did not answer: {turn['error']}")
        raise typer.Exit(EXIT_HUB)


@run_app.command("connect")
@handled
def run_connect(run_id: Annotated[str, typer.Argument(help="The run's id.")]) -> None:
    """Serve a running Cog as an ACP agent on stdin and stdout, for an ACP client such as Toad.

    Start it from the client, not by hand: `toad acp "collab-hub run connect RUN_ID"`.
    Each prompt becomes one turn of the run, as with `run say`.
    """

    from . import acp

    with Hub(_target()) as hub:
        acp.connect(hub, run_id)


@run_app.command("terminate")
@handled
def run_terminate(
    run_id: Annotated[str, typer.Argument(help="The run's id.")],
    no_wait: Annotated[bool, typer.Option(
        "--no-wait", help="Return once the hub has recorded the request, without waiting for the run to end.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Terminate a run: ask the hub to cancel it, and wait until it has ended.

    The controller tears the run's worker down and the run ends CANCELLED. A
    run that has already ended is refused, naming its status.
    """

    with Hub(_target()) as hub:
        run = hub.request_json("POST", f"/v1/runs/{run_id}/cancel")
        if not no_wait:
            run = _follow(hub, run, lambda current: current["ended"], quiet=True)
    if run["ended"]:
        _err(f"{run_id} ended {run['status']}.")
    else:
        _err(f"{run_id}: cancel requested; it is still {run['status']}.")
    if as_json:
        _print_json(run)
    elif not run["ended"]:
        typer.echo(run["status"])
    if run["ended"] and run["status"] != "CANCELLED":
        # It ended on its own between the request and the controller acting on it.
        raise typer.Exit(RUN_EXIT.get(run["status"], EXIT_HUB) or EXIT_HUB)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
