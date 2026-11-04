# Contributing

Thanks for helping. This project is a Music Assistant provider
plugin (Python) that syncs LIFX lights to music via the Sendspin
visualizer stream. It is not a standalone app: the provider code in
`lifx_lights_sync/` only runs inside a Music Assistant instance,
while the pure modules (discovery, color, analyzer) and the test
suite are dependency-free.

## Before you start

- Open an issue before a large pull request so the approach can be
  agreed before you invest the work.
- Run the checks below and make sure they pass. CI runs ruff,
  mypy, JSON validation, and the test suite on every push and pull
  request, and that check must be green before a pull request can
  merge.

```bash
# From the repo root (no Music Assistant, lifx-async, aiohttp or
# numpy needed for the pure modules and the test suite):
python3 -m unittest discover -s tests -v
# The suite is stdlib unittest by design; pytest can also run it:
python3 -m pytest tests

# Lint and type checks (mirroring Music Assistant's own tooling,
# configured in pyproject.toml):
ruff check lifx_lights_sync tests
ruff format --check lifx_lights_sync tests
mypy
python3 -m json.tool lifx_lights_sync/manifest.json > /dev/null
python3 -m json.tool lifx_lights_sync/strings.json > /dev/null
```

Lint and type checks are also wired up via pre-commit:

```bash
pip install pre-commit && pre-commit run --all-files
```

## Compatibility rules

These are load-bearing for existing users; changing them needs a
migration story in the PR:

- The provider domain (`lifx_lights_sync`), its manifest identity,
  and the `depends_on: sendspin` relationship.
- Setting keys and defaults in the Configuration table in the
  README (for example `discovery_source`, `brightness`,
  `brightness_floor`, `color_mode`, `latency_ms`, and
  `send_rate_hz`).
- Discovery behaviour: Home Assistant provides identity (names,
  areas, serials) through its registries, and light IPs come from
  the LAN (broadcast/mDNS discovery plus a unicast subnet sweep for
  serials still missing an IP). Do not add a dependency on raw
  Home Assistant WebSocket commands on another provider's client;
  the hass-provider path uses that provider's public registry
  methods only.
- State restore: the plugin snapshots every target light before a
  stream starts and restores it when the stream stops, because
  LIFX lights do not revert on their own.

## Style

- Keep the render loop split as it is: `analyzer.py` knows nothing
  about LIFX, `lifx_output.py` knows nothing about Music
  Assistant, and only `bridge.py` / `provider.py` touch Music
  Assistant / Sendspin internals. That split is what keeps the
  core testable without a running Music Assistant.
- No new runtime dependencies without discussion: the manifest
  currently pins only `lifx-async==8.0.0`.
- Comments should explain the current design timelessly; incident
  history belongs in the changelog / git log, not in dated comment
  diaries.

## Pull requests

- Work on a branch; never push directly to `main`. Changes land
  on `main` only through pull requests: branch -> green CI ->
  squash-merge.
- One topic per PR, with tests for behaviour changes.
- Update the README when behaviour, configuration, or setup steps
  change, and add a `CHANGELOG.md` entry under `[Unreleased]`.
- Describe user-visible changes in the PR body.

## AI involvement

This project's development uses AI-agent assistance. Every change is reviewed by the repository owner, who remains the responsible human for the project.
