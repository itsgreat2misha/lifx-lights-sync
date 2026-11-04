# LIFX Lights Sync

> **Status: alpha, live-tested on the maintainer's own Music Assistant
> install** with real LIFX lights (end-to-end streaming verified
> there). It has not yet been run against upstream Music Assistant
> `dev` builds or other people's installs, and it is not yet part of
> Music Assistant itself — treat other setups as untested. The pure
> modules (discovery, color, analyzer) are unit-tested.

A Music Assistant **plugin** that makes your LIFX lights react to
whatever is playing — colors and brightness driven by the music, the
way Hue Lights Sync does for Philips Hue entertainment areas.

Instead of a separate visualizer app with its own audio capture, this
plugin is a *Sendspin visualizer client*: Music Assistant's server
computes the FFT spectrum, peak levels and beat timing and streams
them to the plugin, which renders per-light colors and writes them to
your LIFX lights over the local LAN protocol. Each sync target shows
up in Music Assistant as a virtual **LIGHT player** — join it to a
Sendspin player or group, and the lights start reacting.

## Features

- **Home Assistant-first discovery** — lights keep their HA names,
  areas and serials; IPs are resolved on the LAN (see below).
- **LAN fallback** — works without Home Assistant, and fills IP gaps.
- **Bulbs and matrix lights** — ordinary bulbs get one color; LIFX
  Ceiling / Tile-style matrix lights get a full per-zone frame
  (16 x 8 for a 13"x26" ceiling, 8 x 8 for a round ceiling). A ceiling's
  uplight zone (the last zone) is never overwritten by image pixels.
- **Five color modes** — Smooth, Ambient, Flashing, Energetic (port of
  the Hue analyzer's presets), plus a dedicated Bass mode: one shared
  brightness envelope from the 20-369 Hz spectrum bins only, so the
  whole target pops together on low-end thumps and rests dim between
  them (no beat pulse, no onset-peak effects).
- **Album palette** — colors follow the playing track's artwork
  palette when the Sendspin server provides it.
- **State restore** — unlike Hue, LIFX lights do not revert on their
  own, so the plugin snapshots every target light before a stream
  starts and restores it when the stream stops.
- **Rate limiting** — color sends are capped per light (default 20/s).

## How discovery works

The preferred path borrows Music Assistant's *own* Home Assistant
connection: MA's `hass` provider already holds an authenticated HA
WebSocket client, and this plugin reads the registries through its
public methods only — no raw WebSocket commands on the provider's
client, matching the supported cross-provider pattern (the official
`sendspin` provider soft-optionally uses the hass provider the same
way). **With the hass provider configured, no HA credentials are
entered in this plugin at all.** A direct HA URL + long-lived token
is the alternative when the hass provider is not configured.

```
   preferred ───► MA hass provider (already connected to HA)
                  get_device_registry() / get_area_registry()
                  get_entity_registry()          [names/areas/entities]
                      -> public registry methods only: no raw HA
                         commands, and entry hosts are NOT exposed
                         on this path (see key facts below)

   alternative ─► Home Assistant directly (URL + token, WebSocket API)
                  config/device_registry/list
                  config/entity_registry/list
                  config/area_registry/list
                  config_entries/get
                      -> parsed opportunistically for entry hosts:
                         the API strips entry data today, so hosts
                         normally do NOT come from HA either

                      │                    │
                      │ names/areas/       │ lights still missing
                      ▼ serials            │ an IP, or HA absent
                  ┌─────────────────────────────────────────┐
                  │ LAN discovery (lifx-async)              │
                  │   UDP broadcast + mDNS, then a unicast  │
                  │   subnet sweep for missing serials      │
                  │   -> serial, IP, label, product,        │
                  │      matrix width/height  [IP SOURCE,   │
                  │      GAP-FILLER, CROSS-CHECK, GEOMETRY] │
                  └─────────────────────────────────────────┘
                      │
                      ▼ merge on normalized serial
                  sync targets (one virtual LIGHT player each)
```

Key facts that shape this design:

- **HA provides identity, not IPs.** Each LIFX config entry does
  store `{"host": "<ip>", "serial": "<serial>"}` on disk in HA, but
  the WebSocket `config_entries/get` API strips `data`, and the
  registries carry no IP either — so no supported HA API hands the
  plugin a light's address. HA's contribution is the authoritative
  list of lights (serial, name, area, bound entities).
- **IPs come from the LAN, in two passes.** Broadcast discovery
  (lifx-async) finds what it can and supplies matrix geometry, but
  across VLANs/relays it can miss half the install. So for every
  HA-known serial still missing an IP, the plugin unicast-probes
  every host of the subnets where lights are already known to live
  (raw LIFX GetService) — unicast UDP is routed, which closes the
  gap. On the direct path, if a future HA ever exposes entry hosts,
  they win over LAN results for the same serial.
- When HA is absent or unreachable and the source is not `ha`/`hass`,
  LAN-only mode works on its own: bulbs group under one
  "LIFX Lights" target, matrix lights still get individual targets.
- The hass provider is an *optional* source, never a manifest
  dependency: `depends_on` stays `sendspin` only, and `auto` mode
  simply skips the hass step when no instance is loaded.

### Sync targets

| Target | Contents |
| ------ | -------- |
| One per HA area with LIFX lights | All lights in that area, as whole-light channels (matrix lights participate as single channels here) |
| One per matrix / ceiling light | That light's zones as individual channels (uplight zone excluded for ceilings) |
| "Ungrouped" | Lights with no area (HA or LAN-only) |
| "LIFX Lights" (LAN-only mode) | All non-matrix lights, one group |

A light may appear in its area target *and* (if matrix) its own
target — that is intentional: the two targets render differently.

## Installation

This is a custom provider for Music Assistant, not a published add-on
yet.

1. Copy the `lifx_lights_sync/` folder into Music Assistant's custom
   providers directory (the `custom_providers` share on the HA host).
2. Restart the Music Assistant server / custom-loader add-on.
3. In Music Assistant, go to **Settings → Plugins → Add a plugin** and
   select **LIFX Lights Sync**.
4. If Music Assistant's Home Assistant provider is configured, you
   are done — discovery borrows its connection. Only if it is *not*
   configured, the setup flow asks for a direct connection instead:
   enter your HA URL and a long-lived access token (HA profile →
   Security → Long-lived access tokens; read-only registry access is
   all it needs). The credentials are stored encrypted by Music
   Assistant's setup-flow store, not in this plugin's settings; to
   change or clear them later, use the plugin's **Reconfigure** action
   (leaving the token empty there keeps the stored one).
5. Each discovered target appears as a LIGHT player. Join it to any
   Sendspin player or group that is playing.

## Configuration

| Setting | Key | Default | Notes |
| ------- | --- | ------- | ----- |
| Discovery source | `discovery_source` | `auto` | `auto` = MA hass provider → direct HA (if configured) → LAN; `hass` = MA's HA provider only; `ha` = direct HA only (LAN fills IP gaps in both HA modes); `lan` = LAN only |
| Home Assistant URL | `ha_url` | — | *Setup flow, not a setting.* Base URL, e.g. `http://homeassistant.local:8123`. Only needed when MA's Home Assistant provider is not configured |
| Home Assistant token | `ha_token` | — | *Setup flow, not a setting.* Long-lived access token, stored encrypted in Music Assistant's setup-data store. Same condition as above — with the hass provider configured, no HA credentials are entered in this plugin at all |
| Brightness (%) | `brightness` | 80 | Cap on effect brightness (the peaks) |
| Brightness floor (%) | `brightness_floor` | 0 | Resting brightness between hits; 0 keeps each mode's built-in rest level |
| Color mode | `color_mode` | `smooth` | `smooth` / `ambient` / `flashing` / `energetic` / `bass` |
| Latency compensation (ms) | `latency_ms` | 0 | Shifts render time for speaker/network latency |
| Max sends per second | `send_rate_hz` | 20 | Per-light rate limit |
| Debug frame log | `debug_frame_log` | off | Logs one FRAMELOG line per second per streaming target with the mean/max brightness actually commanded (for diagnosing sync from the server log) |

Brightness, brightness floor, color mode, latency, send rate and the
debug frame log are live-applied to running bridges (no restart
needed). Discovery source takes effect on the next provider load.

**Upgrading:** installs that entered the HA URL/token in the plugin
settings before the setup flow existed keep working unchanged — the
stored values are still read. They move into the encrypted setup-data
store the first time you run the setup/reconfigure flow and submit
the form. On Music Assistant versions without setup-flow support,
nothing changes at all: the stored settings values are used directly.

## Architecture / file map

```
lifx_lights_sync/
├── __init__.py      Plugin entry point; setup() lazily imports the
│                    provider (exact installed Hue pattern).
├── manifest.json    Plugin manifest (domain, deps, requirements).
├── strings.json     Config entry labels/help and setup-flow text
│                    for the MA UI.
├── setup_flow.py    Interactive setup flow: collects the optional
│                    direct HA URL + token into MA's encrypted
│                    setup-data store (hass provider pattern).
├── icon.svg / icon_dark.svg
├── constants.py     Config keys, defaults, mode names, visualizer request.
├── settings.py      Typed, defensive config readers (safe fallbacks).
├── color.py         Pure stdlib: RGB <-> HSBK, palette building rules.
├── analyzer.py      Pure stdlib: queued spectrum/peak/beat/palette ->
│                    one LightColorCommand per channel, per mode preset.
├── discovery.py     HA registries + config entries, LAN discovery
│                    (lifx-async), serial normalization, target merge.
├── ha_via_mass.py   HA discovery via MA's own hass provider
│                    (registries only, via the provider's public
│                    methods; duck-typed, no MA imports).
├── lifx_output.py   LifxSender: snapshot / restore / rate-limited
│                    bulb and matrix writes via lifx-async.
├── bridge.py        Sendspin bridge manager + per-target bridge:
│                    virtual LIGHT players, visualizer/color roles,
│                    50 Hz render loop, 2 s debounced stop.
└── provider.py      PluginProvider: config entries, loaded_in_mass,
                     update_config, unload.
tests/               stdlib unittest tests for the pure modules.
```

The render loop is deliberately split: `analyzer.py` knows nothing
about LIFX, `lifx_output.py` knows nothing about Music Assistant, and
only `bridge.py` / `provider.py` touch MA/Sendspin internals. That is
what keeps the core testable without a running Music Assistant.

## Development

```bash
# From the repo root (no Music Assistant, lifx-async, aiohttp or numpy
# needed — the pure modules and the test suite are dependency-free):
python3 -m unittest discover -s tests -v
python3 -m compileall lifx_lights_sync tests
python3 -m json.tool lifx_lights_sync/manifest.json > /dev/null
python3 -m json.tool lifx_lights_sync/strings.json > /dev/null
```

To try the LIFX output path against real hardware you need a Music
Assistant instance with the Sendspin provider enabled, `lifx-async`
installed, and LIFX lights on the LAN. The Music Assistant / Sendspin
call shapes in `bridge.py` and `provider.py` were aligned against
the Music Assistant source and import-verified, and the full path
has been run end-to-end against real lights on the maintainer's
install.

Lint and type checks mirror Music Assistant's own tooling
(ruff + mypy via pre-commit, configured in `pyproject.toml`):

```bash
pip install pre-commit && pre-commit run --all-files
```

## Known limitations

- **Matrix classification is partly unverified.** A LAN-discovered
  device is treated as a matrix light when its lifx-async class is
  `MatrixLight`/`CeilingLight` (confirmed path) *or* when its
  reported capabilities carry a `has_matrix` flag (a belt-and-braces
  extra). Whether the `has_matrix` flag on its own correctly
  identifies every matrix product — and never misfires on ordinary
  bulbs — has not been verified against the full range of real
  hardware; a false positive would give a bulb its own multi-channel
  matrix target instead of treating it as a single light.
- **Upstream drift.** The bridge/provider API surface is aligned to
  the Music Assistant / aiosendspin versions the maintainer runs;
  newer upstream builds may restructure the provider APIs this
  plugin builds on.

## Roadmap

- [ ] Measure sustained frame rate on a 16 x 8 ceiling and, if needed,
      switch matrix writes to the lifx-async `Animator` fast path
      (`device.animator.prepare()` + synchronous `send_frame()`).
- [ ] Per-target settings (e.g. different modes per room).
- [ ] Re-discovery / IP refresh on stream start (lights change IPs).

## Credits

- Adapted from Music Assistant's **Hue Lights Sync** plugin
  (`providers/hue_entertainment`, reference revision 73c3e59): the
  analyzer is an adapted port of the Hue analyzer (mode presets and
  rendering mechanics), and the Sendspin visualizer bridge and
  virtual LIGHT player pattern are adapted from the same plugin. No
  Hue-specific streaming code (DTLS / HueStream) is used.
- The Bass and Energetic modes' low-end behavior was inspired by
  observing **LedFx**'s Filter effect; the implementation is original.
- LIFX communication: **lifx-async** 8.0.0 by Avi Miller
  (https://github.com/Djelibeybi/lifx-async), also used by LedFx.
- LIFX(R) is a trademark of LIFX, Inc. This project is not affiliated
  with or endorsed by LIFX, Inc., Music Assistant, or Signify/Philips Hue.

See `NOTICE` for license details. This project is Apache-2.0 licensed.
