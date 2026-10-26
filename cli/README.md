# collab-hub

A command-line client for the Collab Hub. It is a client, not a second implementation: every command is an HTTP call to the hub's REST API, the same endpoints the Collab desktop uses, and the package imports nothing from the hub's own packages.

Today it signs in and lists the Cogs the hub offers. Launching a Cog, listing what was launched and terminating it come next, then deciding Gates, reading payloads and retrying runs; each phase of [`COG_EXECUTION.md`](../COG_EXECUTION.md) that adds a hub surface adds its commands here.

## Installing

Python 3.11 or later.

```sh
uv tool install ./cli        # from a checkout of this repository
collab-hub --help
```

In the dev environment, `make -C dev cli` installs it and prints the sign-in line for the level in use.

## Signing in

```sh
collab-hub --hub https://hub.example.org login
```

`--hub`, like `--profile` and `--insecure`, is an option of `collab-hub` itself, so it comes before the command.

The CLI signs in the way the Collab desktop does, against the same Keycloak realm and the same public client (`apollo-desktop`): the authorization code flow with PKCE (S256) and a loopback redirect (RFC 8252). `login` opens the realm's sign-in page in a browser on this machine, which returns to a listener on `127.0.0.1` and a port the system picks. The hub's URL is all you type: the hub names its issuer and client at `GET /v1/auth/cli`.

- `--no-browser` prints the sign-in URL instead of opening it. The browser has to reach the CLI's listener on `127.0.0.1`, so on the same machine it just works.
- `--port PORT` fixes the listener's port (by default the system picks one). That is what makes a remote host work: forward the port from your laptop first, then sign in on the remote host and open the printed URL in your laptop's browser. The realm's client accepts any `127.0.0.1` port.

  ```sh
  ssh -L 8765:127.0.0.1:8765 remote-host          # on your laptop
  collab-hub --hub https://hub.example.org login --no-browser --port 8765   # on the remote host
  ```

  Or use `--with-token`.
- `--with-token` reads a bearer token from stdin, for CI or a script that already holds one: `make -C dev token | collab-hub login --with-token`. Such a token is used until it expires and never renewed.

The session is kept in `credentials/<profile>.json` in the configuration directory, readable only by you (the directory is `0700`, the file `0600`), and is sent only to the hub it was obtained for. It is renewed with its refresh token before it expires. Only a refresh the realm refuses (`invalid_grant`) ends it; a realm that is down or failing is exit 1, and the session is kept for the next command.

Signing in again replaces the profile's session, and the old one is ended at its realm rather than left open — unless both belong to the same realm session (the same `sid`), as when the browser was still signed in, since ending it would end the new one too. A sign-in the hub then refuses is ended the same way and never stored.

**What the CLI trusts.** Plain `http` is accepted only for a hub on this machine (`localhost`, `*.localhost`, loopback addresses); anywhere else the issuer that `GET /v1/auth/cli` names could be swapped on the way, and every token would cross in clear. `--insecure` accepts it anyway. The realm's discovery document must name the issuer the hub named, and its endpoints must be `https`, or `http` on this machine.

```sh
collab-hub whoami            # the user, organization and roles the hub resolved, and the token's expiry
collab-hub logout            # ends the realm session, revokes its refresh token, deletes the stored token
```

`logout` does what the desktop's sign-out does, ending the realm session at its end-session endpoint, and then revokes the refresh token at the realm's revocation endpoint (RFC 7009), which is what guarantees it never renews again. If the realm cannot revoke it, the stored token is still deleted, and `logout` warns and exits 1. A token given to `--with-token` is only forgotten: the CLI did not obtain it, so it stays valid until it expires.

`whoami` asks the hub (`GET /v1/me`), so it reports what the hub will act on rather than what the token claims. Against a hub running dev auth, as at dev level 1, it says the session is unauthenticated: the hub answers every request as its development user, and there is nothing to sign in to.

## Profiles

Several hubs are one flag apart. `login` records its hub under the profile it ran with, and the first profile recorded becomes the default:

```toml
# ~/.config/collab-hub/config.toml
default_profile = "default"

[profiles.default]
hub = "http://127.0.0.1:8000"

[profiles."work"]
hub = "https://hub.example.org"
```

A command's hub is `--hub`, else `COLLAB_HUB_URL`, else its profile's `hub`. Its profile is `--profile`, else `COLLAB_HUB_PROFILE`, else `default_profile`, else `default`. The configuration directory is `COLLAB_HUB_CONFIG_DIR`, else `$XDG_CONFIG_HOME/collab-hub`, else `~/.config/collab-hub`.

## The Cogs the hub offers

```sh
collab-hub cog list                        # every Cog in the catalog, at its newest version
collab-hub cog list --kind context -q review
collab-hub cog show acme/reviewer          # one Cog's card and every indexed version
```

`cog list` follows every page of `GET /v1/cogs` and takes every filter the catalog API does: `--kind`, `--publisher`, `--provides`, `--requires`, `--accepts`, `--produces`, `--source-id` and `--query`.

## Launching a Cog, and its runs

[`docs/cog-execution.md`](../docs/cog-execution.md) walks through it on a hub, from installing the CLI to stopping the Cog.

```sh
collab-hub cog launch hello --input '{"name": "Ada"}' --watch   # launch, and follow the run to its end
collab-hub cog launch hello --entry run --gate never --input -  # the input from stdin
collab-hub run list                                             # what was launched, newest first, and how to connect
collab-hub run list --status running
collab-hub run show run-3eb7ab0eaf43                            # one run, each step, and what it answered
collab-hub run watch run-3eb7ab0eaf43                           # follow it until it ends or waits at a Gate
collab-hub run terminate run-3eb7ab0eaf43                       # cancel it, and wait until it has ended
```

`cog launch NAME` submits a one-step Op through `POST /v1/runs`, and `--name LABEL` names the run in `run list` (`hermes-on-claude`, say): `NAME` is a Cog package the hub's run controller can launch, and a name it cannot is refused with the names it can. `--entry` names the entry point (`run` by default), `--input` its input as JSON, and `--gate` when the step's Gate asks a person (`never`, `error`, `warn` or `always`). Without `--watch` it prints the run's id and returns; the run controller starts the run. It says which durability backend and which location the run is on, since a run on `none` does not survive a controller restart and a `local` worker shares the controller's host.

### Talking to a running Cog

```sh
collab-hub cog list --launchable                         # the Cogs this hub can launch
collab-hub cog launch hello --entry session --gate never # a Cog that holds a session
collab-hub run say run-c42c387d6e23 sum 1 2 3            # one turn: prints what it answered
toad acp "collab-hub run connect run-c42c387d6e23"       # the same Cog, from an ACP client
```

`run say` sends one turn and prints the answer; with none within `--timeout` seconds (300 by default) it says what the run is doing and exits 1, and the turn stays on the run. `run connect` serves the run as an agent of the [Agent Client Protocol](https://agentclientprotocol.com) on stdin and stdout, so a client such as [Toad](https://github.com/batrachianai/toad) can talk to the Cog: each prompt becomes one turn, sent to the hub and read back, and the answer is streamed to the client. Start it from the client, not by hand; it writes nothing but the protocol to stdout. A cancelled prompt stops waiting, though its turn stays on the run.

`run terminate` asks the hub to cancel the run and waits until the controller has ended it; `--no-wait` returns once the request is recorded. A run that has already ended is refused, naming its status.

These commands need a hub with Cog packages to launch and a run controller beside it. [`examples/cog-local`](../examples/cog-local/README.md) runs all of them against a hub on your own machine.

## Output and exit codes

Tables for people by default; `--json`, which every command but `run connect` takes, prints one JSON document for scripts, shaped for `jq` (`run connect`'s stdout carries the ACP protocol and nothing else). Results go to stdout, messages to stderr.

- `login --json`: what `whoami --json` prints, plus `obtained_by` (`browser` or `token`) and `dev_auth`; against a hub running dev auth, `{hub, profile, signed_in: false, dev_auth: true}`.
- `logout --json`: `{hub, profile, was_signed_in, revoked, warning}`.
- `whoami --json`: the hub's `GET /v1/me` answer plus `hub`, `profile`, `signed_in` and `token_expires_at`.
- `cog list --json` and `cog show --json`: the catalog API's items and Cog, as the hub returns them.
- `run say --json`: the turn, with its `state`, `answer` or `error`. `cog list --launchable --json`: the names.
- `cog launch --json`, `run show --json`, `run watch --json` and `run terminate --json`: the run, as `GET /v1/runs/{id}` returns it; `run list --json`: the runs.

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the hub or the realm refused or failed the request, or could not be reached; a session is kept. Also a run that was waited for and failed, or ran out of budget |
| 2 | a usage error: a bad option, a missing hub |
| 3 | a run that was waited for (`--watch`, `run watch`) ended `INTERRUPTED` |
| 4 | a run that was waited for is waiting at a Gate |
| 5 | not signed in, or the session has expired and could not be renewed |
| 6 | a run that was waited for was `CANCELLED` |

## Development

```sh
cd cli
uv run --group test pytest
uv run --with ruff ruff check src tests
```

The sign-in is tested against a stub realm, including a real redirect to the loopback listener. An import-boundary test keeps the CLI from importing `collab_hub_api` or `collab_hub_execution`.
