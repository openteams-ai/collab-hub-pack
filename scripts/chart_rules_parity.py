#!/usr/bin/env python3
"""Prove the chart's refusals agree with the API's, from one fixture.

    scripts/chart_rules_parity.py --helm helm --chart helm/collab-hub \
        scripts/testdata/chart/cogs-negative-cases.yaml

The Cog registry rules live in three layers — values.schema.json, the
validations template, and the API's config models — and this script is the
chart side of keeping them in step (api/tests/test_config_cogs.py is the API
side; both read the same fixture, whose header documents the fields). For
every case:

1. Render the case's ``values``: the chart must refuse, with ``chart_error``.
   When ``template_error`` is set, render again with the schema skipped: the
   template must refuse on its own.
2. Unless ``compare_render`` is false, render once more with the schema
   skipped *and* the validations template removed, and assert the ``cogs``
   settings the Deployment carries equal the case's ``settings``. That proves
   the two forms in the fixture describe one configuration, which is what
   lets the API test stand in for "the chart's JSON is refused at startup"
   without needing helm.

When ``schema_error`` is set, render with the validations template removed
but the schema on: the schema must refuse on its own, so a rule that exists
in both layers is proven in each.

Then every ``fail`` in templates/cogs-validations.yaml must have fired during
step 1 for some case, so a rule added to the template without a case is
caught here, and a case added without its rule fails step 1. Schema and API
rules have no such inventory; the fixture enumerates them by hand.

Finally the fixture's ``accepted`` configurations — boundaries every layer
must let through — are rendered with the full chart and compared the same
way, so the schema cannot drift stricter than the API unnoticed.

Driven by scripts/chart_render_tests.sh. Needs PyYAML and a helm on PATH (or
``--helm "docker run --rm -v $PWD:$PWD -w $PWD alpine/helm"``; the scratch
directory is created under the repository so a bind mount can see it).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES_VAR = "COLLAB_HUB_API__COGS__REGISTRY_SOURCES"
ENABLED_VAR = "COLLAB_HUB_API__COGS__INDEX__ENABLED"
INTERVAL_VAR = "COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS"
RUN_ON_STARTUP_VAR = "COLLAB_HUB_API__COGS__INDEX__RUN_ON_STARTUP"
VALIDATIONS_TEMPLATE = Path("templates") / "cogs-validations.yaml"

# `fail "..."` and `fail (printf "..." ...)` in a Go template; the literal is
# group 1. The template's messages contain no escaped quotes today; the
# pattern tolerates them anyway.
FAIL_LITERAL = re.compile(r'\bfail\s*\(?\s*(?:printf\s*)?"((?:[^"\\]|\\.)*)"')
PLACEHOLDER = re.compile(r"%[sdvq%]")

INDEX_DEFAULTS = {"enabled": False, "interval_seconds": 300, "run_on_startup": True}


class Reporter:
    def __init__(self) -> None:
        self.failures = 0

    def ok(self, name: str) -> None:
        print(f"ok   {name}")

    def fail(self, name: str, detail: str) -> None:
        self.failures += 1
        print(f"FAIL {name}\n     {detail}")


def run_helm(helm: list[str], chart: Path, values_file: Path, *extra: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [*helm, "template", "t", str(chart), "-f", str(values_file), *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


def first_lines(text: str, n: int = 3) -> str:
    return " | ".join(line for line in text.strip().splitlines()[:n])


def rendered_cogs(manifests: str) -> dict[str, Any]:
    """The ``cogs`` settings the rendered release hands the sweeping process, in the API's shape.

    With ``cogs.index.enabled`` the chart renders a dedicated indexer
    Deployment and that is the process the index settings reach (issue
    #148); the API Deployment then carries ``enabled=false`` and no tuning.
    So the indexer's environment is read when it is rendered, the API's
    otherwise -- the source list is the same on both.
    """

    docs = [d for d in yaml.safe_load_all(manifests) if d]
    deployments = {d["metadata"]["labels"]["app.kubernetes.io/component"]: d for d in docs if d.get("kind") == "Deployment"}
    deployment = deployments.get("indexer") or deployments["api"]
    env: dict[str, str | None] = {}
    for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]:
        env[entry["name"]] = entry.get("value")
    index = dict(INDEX_DEFAULTS)
    index["enabled"] = env.get(ENABLED_VAR) == "true"
    if INTERVAL_VAR in env:
        index["interval_seconds"] = int(env[INTERVAL_VAR] or 0)
    if RUN_ON_STARTUP_VAR in env:
        index["run_on_startup"] = env[RUN_ON_STARTUP_VAR] == "true"
    sources = json.loads(env[SOURCES_VAR] or "[]") if SOURCES_VAR in env else []
    return {"registry_sources": sources, "index": index}


def normalized_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """The fixture's ``settings`` with the API's defaults filled, for comparison with a render."""

    index = {**INDEX_DEFAULTS, **settings.get("index", {})}
    return {"registry_sources": settings.get("registry_sources", []), "index": index}


def check_case(
    case: dict[str, Any],
    *,
    helm: list[str],
    chart: Path,
    bare_chart: Path,
    scratch: Path,
    report: Reporter,
    collected: list[str],
) -> None:
    name = case["name"]
    values_file = scratch / f"{re.sub(r'[^a-z0-9]+', '-', name.lower())}.yaml"
    values_file.write_text(yaml.safe_dump(case.get("values", {}), sort_keys=False))

    rendered, out = run_helm(helm, chart, values_file)
    if rendered:
        report.fail(name, f"rendered, but should have failed with: {case['chart_error']}")
        return
    if not re.search(case["chart_error"], out):
        report.fail(name, f"failed for another reason: {first_lines(out)}")
        return
    collected.append(out)

    if template_error := case.get("template_error"):
        rendered, out = run_helm(helm, chart, values_file, "--skip-schema-validation")
        if rendered:
            report.fail(
                name,
                f"rendered with the schema skipped; the template rule should stand alone: {template_error}",
            )
            return
        if not re.search(template_error, out):
            report.fail(
                name,
                f"with the schema skipped, failed for another reason: {first_lines(out)}",
            )
            return
        collected.append(out)

    if schema_error := case.get("schema_error"):
        rendered, out = run_helm(helm, bare_chart, values_file)
        if rendered:
            report.fail(
                name, f"rendered with the template removed; the schema should refuse on its own: {schema_error}"
            )
            return
        if not re.search(schema_error, out):
            report.fail(name, f"with the template removed, failed for another reason: {first_lines(out)}")
            return

    if case.get("compare_render", True) and "settings" in case:
        rendered, out = run_helm(helm, bare_chart, values_file, "--skip-schema-validation")
        if not rendered:
            report.fail(name, f"does not render even without its rules: {first_lines(out)}")
            return
        got = rendered_cogs(out)
        want = normalized_settings(case["settings"])
        if got != want:
            report.fail(
                name,
                "the chart renders different settings than the fixture's `settings` form:\n"
                f"     rendered: {json.dumps(got, sort_keys=True)}\n"
                f"     fixture:  {json.dumps(want, sort_keys=True)}",
            )
            return

    report.ok(name)


def check_accepted(case: dict[str, Any], *, helm: list[str], chart: Path, scratch: Path, report: Reporter) -> None:
    """A boundary configuration every layer must let through: full chart renders it, settings match."""

    name = f"accepted: {case['name']}"
    values_file = scratch / f"accepted-{re.sub(r'[^a-z0-9]+', '-', case['name'].lower())}.yaml"
    values_file.write_text(yaml.safe_dump(case["values"], sort_keys=False))
    rendered, out = run_helm(helm, chart, values_file)
    if not rendered:
        report.fail(name, f"the chart refused a configuration every layer must accept: {first_lines(out)}")
        return
    got = rendered_cogs(out)
    want = normalized_settings(case["settings"])
    if got != want:
        report.fail(
            name,
            "the chart renders different settings than the fixture's `settings` form:\n"
            f"     rendered: {json.dumps(got, sort_keys=True)}\n"
            f"     fixture:  {json.dumps(want, sort_keys=True)}",
        )
        return
    report.ok(name)


def check_manifest_shape(cases: list[dict[str, Any]], report: Reporter) -> None:
    names = [case["name"] for case in cases]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        report.fail("fixture", f"duplicate case names: {duplicates}")
    for case in cases:
        has_app = "app_error" in case
        has_why = "why_chart_only" in case
        if has_app == has_why:
            report.fail(case["name"], "a case needs exactly one of app_error / why_chart_only")
        if has_app and "settings" not in case:
            report.fail(
                case["name"],
                "app_error needs a settings form for the API test to feed Config",
            )
        if "chart_error" not in case:
            report.fail(case["name"], "chart_error is required")


def check_template_coverage(chart: Path, collected: list[str], report: Reporter) -> None:
    """Every fail() in the validations template must have fired for some case."""

    template = (chart / VALIDATIONS_TEMPLATE).read_text()
    literals = FAIL_LITERAL.findall(template)
    if not literals:
        report.fail(
            "template coverage",
            f"found no fail() literals in {VALIDATIONS_TEMPLATE}; pattern out of date?",
        )
        return
    joined = "\n".join(collected)
    missing = []
    for literal in literals:
        fragment = max(PLACEHOLDER.split(literal), key=len).strip()
        if fragment not in joined:
            missing.append(fragment)
    if missing:
        detail = "\n     ".join(f"- {fragment[:90]}" for fragment in missing)
        report.fail(
            "template coverage",
            f"{len(missing)} of {len(literals)} fail() rules in {VALIDATIONS_TEMPLATE} never fired; "
            f"add a case:\n     {detail}",
        )
    else:
        report.ok(f"template coverage: all {len(literals)} fail() rules in {VALIDATIONS_TEMPLATE.name} exercised")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("fixture", type=Path)
    parser.add_argument(
        "--helm",
        default="helm",
        help="helm command; may be several words (a docker wrapper)",
    )
    parser.add_argument("--chart", type=Path, default=ROOT / "helm" / "collab-hub")
    args = parser.parse_args()

    helm = shlex.split(args.helm)
    if not helm:
        print("--helm is empty: pass the helm command (e.g. --helm helm, or a docker wrapper)", file=sys.stderr)
        return 2
    chart = args.chart.resolve()
    fixture = yaml.safe_load(args.fixture.read_text())
    cases = fixture["cases"]
    accepted = fixture.get("accepted", [])
    report = Reporter()
    check_manifest_shape(cases, report)

    # Scratch lives under the repository so a docker-wrapped helm that bind
    # mounts $PWD can read the values files and the rule-less chart copy.
    scratch = Path(tempfile.mkdtemp(prefix=".chart-parity-", dir=ROOT))
    try:
        bare_chart = scratch / "chart-without-rules"
        shutil.copytree(chart, bare_chart)
        (bare_chart / VALIDATIONS_TEMPLATE).unlink()
        collected: list[str] = []
        for case in cases:
            check_case(
                case,
                helm=helm,
                chart=chart,
                bare_chart=bare_chart,
                scratch=scratch,
                report=report,
                collected=collected,
            )
        check_template_coverage(chart, collected, report)
        for case in accepted:
            check_accepted(case, helm=helm, chart=chart, scratch=scratch, report=report)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if report.failures:
        print(f"\n{report.failures} chart rule parity check(s) failed")
        return 1
    print(
        f"\nall {len(cases)} negative cases refused by the chart and consistent with their settings form; "
        f"{len(accepted)} accepted boundary case(s) render"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
