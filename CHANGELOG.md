# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This project is pre-1.0 and in alpha (the provider manifest carries
`stage: alpha`). There are no git tags or GitHub releases yet, so
everything after the initial commit is recorded under `[Unreleased]`.

## [Unreleased]

### Added

- Dedicated Bass color mode: one shared brightness envelope driven
  only by the low spectrum bins (20-369 Hz), revised to follow the
  smoothed low-band level per spectrum frame.
- Brightness floor setting (`brightness_floor`): a configurable
  resting brightness between hits; the existing Brightness setting
  is the peak cap.
- Debug frame log setting (`debug_frame_log`): when enabled, each
  streaming target logs one FRAMELOG line per second with the
  mean/max brightness actually commanded.
- Setup flow for direct Home Assistant credentials (HA URL and
  long-lived access token), for installs where Music Assistant's
  Home Assistant provider is not configured; the provider stage
  was set to `alpha` in the same change.
- Project documentation and repository tooling: `CONTRIBUTING.md`,
  this changelog, issue and pull request templates, `SECURITY.md`,
  README Troubleshooting and Uninstalling sections,
  Music Assistant-style ruff + mypy pre-commit tooling, a CI
  workflow with least-privilege permissions (ruff, mypy, JSON
  validation, and the test suite on push and pull request),
  Dependabot configuration, and an OpenSSF Scorecard workflow.

### Changed

- The analyzer was reworked into a faithful port of the Hue
  analyzer: transient-only per-channel multipliers, onset peaks,
  spectrum noise gate and gamma, bass-transient saturation, and
  beat-segment palette interpolation, rendered at 50 Hz.
- Energetic mode's brightness is now driven only by the low
  spectrum bins, with a bass-band transient also lifting global
  brightness.
- The LIFX output layer now sends fire-and-forget color packets to
  bulbs at a steady 20 Hz cap, and matrix/ceiling frames go through
  the lifx-async Animator.
- Discovery via Music Assistant's hass provider no longer uses the
  unsupported `config_entries/get` call.
- Live-apply was fixed so the brightness floor and debug frame log
  settings take effect on running bridges.
- The test suite was rewritten in pytest style, with tooling
  aligned to the Music Assistant upstream pins (ruff 0.16.5, mypy
  2.3.0, Python 3.14 target); provider methods were reordered so
  public methods precede private ones, and the provider manifest
  no longer carries the non-standard `version` field.

### Fixed

- README corrections: the status note and Development commands now
  match the current state — the provider runs against upstream
  Music Assistant `dev` and the test suite runs under pytest — and
  the Known limitations section, whose entries (matrix
  classification, upstream alignment) no longer apply, was removed.
- Documentation and comment corrections: contributor instructions
  in `CONTRIBUTING.md` and the pull request template now describe
  the pytest suite; LAN discovery is described as broadcast-only
  in the README, `CONTRIBUTING.md`, and the discovery module,
  matching the code (no mDNS is used); the README Roadmap no
  longer lists the shipped Animator fast path as future work; and
  code headers no longer cite a superseded aiosendspin version or
  describe the Hue/MA references as "installed".
- Lights silently doing nothing: typed LIFX light/matrix/ceiling
  devices are now created instead of the base device type returned
  by IP lookup, which has no color methods.
- Missing light IPs: because Home Assistant strips config-entry
  data, IPs are now found by a unicast subnet sweep for serials
  still missing an IP.
- Frozen output: spectrum bins are uint16 dB-scaled values, not
  0-255, and visualizer frames are rendered on their own
  timestamped timeline rather than the server clock.

## [0.1.0] - 2026-11-02

### Added

- Initial LIFX Lights Sync plugin for Music Assistant: a Sendspin
  visualizer client that renders per-light colors and brightness
  from Music Assistant's streamed spectrum, peak, and beat data
  and writes them to LIFX lights over the local LAN protocol.
- Each sync target appears in Music Assistant as a virtual LIGHT
  player that can be joined to a Sendspin player or group: one
  target per Home Assistant area with LIFX lights, one per
  matrix/ceiling light, plus "Ungrouped" and LAN-only targets.
- Home Assistant-first discovery (names, areas, and serials from
  the HA registries, via Music Assistant's hass provider or a
  direct HA connection), with LAN discovery via lifx-async as a
  fallback and IP gap-filler.
- Four color modes (Smooth, Ambient, Flashing, and Energetic; the
  Bass mode was added later, under `[Unreleased]` above),
  album-artwork palette support, per-light rate limiting, and
  snapshot/restore of each target light's state when a stream
  starts and stops.
