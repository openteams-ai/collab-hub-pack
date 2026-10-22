# Security Policy

## Supported Versions

Security fixes are made on `main` and shipped in the next release. Only the most recent release receives fixes; older releases are not patched. Please confirm the issue against the latest release or `main` before reporting.

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues, pull requests, or discussions.

Report them privately through GitHub's private vulnerability reporting: [open a new advisory](https://github.com/openteams-ai/collab-hub-pack/security/advisories/new). Only the maintainers can see the report.

Please include as much of the following as possible:

- The version of this pack you are running, and where it is deployed
- Steps to reproduce, or a proof of concept
- The impact as you understand it, including what an attacker would need to exploit it

## What to Expect

The maintainers will acknowledge the report within 7 days, work with you to confirm and understand the issue, and keep you updated on progress toward a fix. Once a fix is released, we will publish a GitHub Security Advisory and credit you unless you ask us not to.

Please give us a reasonable time (at least 45 days) to release a fix before disclosing the issue publicly.

## Scope

In scope:

- The pack's Helm chart and its default values, and the chart and container images we publish to GHCR
- Code in this repository, including the API service, admin UI, and `collab-hub` CLI
- Cog execution, including isolation between a running cog and the hub, other cogs, user credentials, and the network
- How the pack integrates with Nebari, such as NebariApp routing, TLS, and Keycloak authentication
- How the Collab desktop applications or other clients interact with the pack

Out of scope here:

- Vulnerabilities in the Nebari platform itself; report those to [Nebari Infrastructure Core](https://github.com/nebari-dev/nebari-infrastructure-core/security/advisories/new) or [nebari-operator](https://github.com/nebari-dev/nebari-operator/security/advisories/new)
- Issues that require an attacker to already hold cluster-admin or cloud-account administrator credentials, or have control over the underlying hardware
- Vulnerabilities in upstream components the chart deploys, such as Keycloak or PostgreSQL; report those to the upstream project. How this pack configures them remains in scope
