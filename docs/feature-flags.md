# Feature flags

Work in progress lands on `main`, so a feature can reach a deployment before it is ready to expose. Feature flags keep such a feature dark by default and let an operator turn it on per deployment, with no code change and no long-lived branch.

The mechanism is always on. Each flag is declared once and is off until a deployment sets it to true.

## Adding a flag

Declare it in `FEATURE_FLAGS` in `api/src/collab_hub_api/config.py`, with one line on what it exposes:

```python
FEATURE_FLAGS: dict[str, str] = {
    "cogs_ui": "The Cogs screens in the admin panel.",
}
```

Names are lowercase snake_case (`cogs_ui`, not `cogsUI` or `cogs-ui`), the form environment variables arrive in and the only form the chart accepts, and a name can't be both declared and retired. The API checks both when it loads its configuration, so a bad entry stops it at startup and fails every test.

Then gate the code path on it. A name that is not declared is refused wherever it appears: set in the environment or the chart, it stops the API at startup, and passed to `enabled()`, it raises. A misspelled flag therefore fails loudly instead of silently reading as off.

Flags are for work in progress: once the feature ships, remove the flag from the code and move its name from `FEATURE_FLAGS` to `RETIRED_FEATURE_FLAGS`. A shipped capability that needs a permanent operational switch belongs in its own config section as an explicit `bool` field with a documented default, such as `connectors.github.api_get_enabled`. A deployment that still sets a retired flag starts, and the API logs a `feature_flag_retired_ignored` warning naming it, so the chart that retires a flag can roll out before every deployment's values drop it. A name that was never declared still stops startup.

The same rule makes rollback a two-step change. An image that predates a flag doesn't know its name, so a deployment that sets the flag won't start on that image. Before rolling the API back past the release that introduced a flag, drop the flag from the deployment's values.

## The flags

| Flag | What it exposes | Also needs |
|---|---|---|
| — | No flag is registered today | |

**Retired.** `cog_runs` (the run API, `/v1/runs`) shipped with the image's
execution package and is always served: on the hub's own database, or on
`runs.track_path` ([runs](cog-execution/runs.md#the-run-controller-and-the-run-api)).
A deployment that still sets it starts, and the API logs it.

## Turning a flag on

With the Helm chart, add the flag to `features` and set it to `true`. The chart accepts only lowercase names made of words joined by single underscores, so every name maps to exactly one environment variable:

```yaml
features:
  cogs_ui: true
```

The chart renders each entry as an environment variable, `COLLAB_HUB_API__FEATURES__<NAME>`, which is also how to set a flag without the chart:

```sh
COLLAB_HUB_API__FEATURES__COGS_UI=true
```

The variable name follows pydantic-settings' [nested environment variables](https://docs.pydantic.dev/latest/concepts/pydantic_settings/#parsing-environment-variable-values), and the value is parsed with pydantic's [boolean rules](https://docs.pydantic.dev/latest/api/standard_library_types/#booleans): `true`, `1`, `yes` and `on` (any case) turn a flag on, and `false`, `0`, `no` and `off` keep it off. Any other value stops the API at startup with an error naming the flag. The chart's schema also refuses a non-boolean value at render time.

## Reading a flag

In a route, take the features through the `get_features` dependency:

```python
from fastapi import Depends

from collab_hub_api.config import FeaturesConfig
from collab_hub_api.dependencies import get_features


@router.get("/example")
def example(features: FeaturesConfig = Depends(get_features)):
    if features.enabled("cogs_ui"):
        ...
```

In the app factory and the store builders, read `config.features.enabled("cogs_ui")` directly. Always pass the name exactly as `FEATURE_FLAGS` spells it; `enabled()` doesn't normalize case or dashes. Never read the environment variable with `os.environ`.

The admin panel receives the flags that are on as `features` in its session payload (`GET /admin/api/session`), so an unfinished screen can be hidden until its flag is set.
