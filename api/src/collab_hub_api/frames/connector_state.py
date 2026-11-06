"""Which connectors are switched on, and making that switch bite.

Deployment configuration holds the credentials and therefore decides what this
hub is *capable* of. This table holds one bit per connector and decides what is
*available* right now. The split is deliberate and non-overlapping:

* the panel can never turn on a connector that has no credentials, because it
  does not hold any -- there is nothing for it to write;
* the panel can turn a configured connector off, and back on again.

So there is no second source of truth for how a connector authenticates. There
is exactly one new fact -- is it on -- and it lives in one place.

How it is enforced
------------------
:func:`apply_disabled` blanks the credential fields of a disabled connector
before the request sees the configuration, through the one dependency every
connector route already takes. A disabled connector is then indistinguishable,
downstream, from one that was never configured -- which is a state those routes
already refuse correctly. No route had to change, and no route can forget.

The status routes add one thing on top, in ``routers/connectors.py``: they
answer 404 for a switched-off connector, because "not connected" would invite
a user to connect something this hub is not offering.

The alternative was a check in each of the twenty-odd connector handlers, which
is twenty-odd chances to miss one.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["CONNECTOR_KEYS", "apply_disabled"]

CONNECTOR_KEYS = ("google", "slack", "github")
"""The connectors this hub knows how to switch.

Enumerated rather than derived from the config object's fields, so that adding
an unrelated section to that model cannot silently make it switchable before
anyone has thought about what disabling it should mean.
"""

_CREDENTIAL_FIELDS = ("broker_token_url", "static_access_token")
"""What has to be blanked for a connector to read as unconfigured.

Base URLs are left alone: they are not credentials, they carry no authority,
and a connector with an endpoint but no token is exactly the "not configured"
state the rest of the code already understands.
"""


def apply_disabled(connectors, disabled: Iterable[str]):
    """Return *connectors* with every name in *disabled* rendered unusable.

    The original is never mutated -- it is the app's shared configuration
    object, and a request that scribbled on it would disable a connector for
    every other request too, permanently and invisibly. Unknown names are
    ignored rather than raising: the set comes from a table that outlives any
    one build, so a connector removed from the code must not break startup for
    a deployment that had switched it off.
    """

    wanted = {name for name in disabled if name in CONNECTOR_KEYS}
    if not wanted:
        # The overwhelmingly common case, and worth not copying the model for.
        return connectors

    updates = {}
    for name in wanted:
        section = getattr(connectors, name, None)
        if section is None:
            continue
        updates[name] = section.model_copy(update=dict.fromkeys(_CREDENTIAL_FIELDS, ""))
    return connectors.model_copy(update=updates)
