# LIFX Lights Sync

> **Status: alpha scaffold.** Modeled on Music Assistant's Hue Lights
> Sync plugin, **not yet live-tested against a running Music Assistant**
> with real LIFX lights. The pure modules (discovery, color, analyzer)
> are unit-tested; the Sendspin bridge/provider integration is marked
> `TODO(live-verify)` where the exact Music Assistant API surface still
> needs confirmation on a live instance.

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

- **Home Assistant-first discovery** — lights keep their HA names and
  areas, and HA config entries provide their IPs (see below).
- **LAN fallback** — works without Home Assistant, and fills IP gaps.
- **Bulbs and matrix lights** — ordinary bulbs get one color; LIFX
  Ceiling / Tile-style matrix lights get a full per-zone frame
  (16 x 8 for a 13"x26" ceiling, 8 x 8 for a round ceiling). A ceiling's
  uplight zone (the last zone) is never overwritten by image pixels.
- **Four color modes** — Smooth, Ambient, Flashing, Energetic (port of
  the Hue analyzer's presets).
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
public methods plus `config_entries/get` through its generic command
sender — the same pattern AI Radio uses for HA-backed engines. **With
the hass provider configured, no HA credentials are entered in this
plugin at all.** A direct HA URL + long-lived token is the alternative
when the hass provider is not configured.

```
   preferred ───► MA hass provider (already connected to HA)
                  get_device_registry() / get_area_registry()
                  get_entity_registry()          [names/areas/entities]
                  hass.send_command("config_entries/get")
                      -> data.host + data.serial  [PRIMARY IP SOURCE]

   alternative ─► Home Assistant directly (URL + token, WebSocket API)
                  config/device_registry/list
                  config/entity_registry/list
                  config/area_registry/list
                  config_entries/get
                      -> same data as above

                      │                    │
                      │ names/areas/IPs    │ no host for some lights,
                      ▼                    │ or HA absent/unreachable
                  ┌─────────────────────────────────────────┐
                  │ LAN discovery (lifx-async)              │
                  │   UDP broadcast + mDNS                  │
                  │   -> serial, IP, label, product,        │
                  │      matrix width/height  [FALLBACK IP, │
                  │      GAP-FILLER, CROSS-CHECK, GEOMETRY] │
                  └─────────────────────────────────────────┘
                      │
                      ▼ merge on normalized serial
                  sync targets (one virtual LIGHT player each)
```

Key facts that shape this design:

- **HA's LIFX config entry data stores `{"host": "<ip>", "serial":
  "<serial>"}` per light** — that is how HA itself reaches the light,
  and HA's discovery keeps `host` current when the IP changes. It is
  only the device/entity *registries* that lack IPs, and HA's
  diagnostics display redacts host/serial — which is easy to mistake
  for "HA doesn't store IPs".
- **Merge precedence: HA host always wins** over a differing LAN IP
  for the same serial. LAN discovery then fills in lights whose HA
  entry has no host, cross-checks the rest, and supplies matrix
  geometry (which HA never exposes).
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
   configured, add your HA URL and a long-lived access token in this
   plugin's settings (profile → Security → Long-lived access tokens;
   read-only registry access is all it needs).
5. Each discovered target appears as a LIGHT player. Join it to any
   Sendspin player or group that is playing.

## Configuration

| Setting | Key | Default | Notes |
| ------- | --- | ------- | ----- |
| Discovery source | `discovery_source` | `auto` | `auto` = MA hass provider → direct HA (if configured) → LAN; `hass` = MA's HA provider only; `ha` = direct HA only (LAN fills IP gaps in both HA modes); `lan` = LAN only |
| Home Assistant URL | `ha_url` | — | Base URL, e.g. `http://homeassistant.local:8123`. Only needed when MA's Home Assistant provider is not configured |
| Home Assistant token | `ha_token` | — | Long-lived access token (stored as a secret). Same condition as above — with the hass provider configured, no HA credentials are entered in this plugin at all |
| Brightness (%) | `brightness` | 80 | Cap on effect brightness |
| Color mode | `color_mode` | `smooth` | `smooth` / `ambient` / `flashing` / `energetic` |
| Latency compensation (ms) | `latency_ms` | 0 | Shifts render time for speaker/network latency |
| Max sends per second | `send_rate_hz` | 20 | Per-light rate limit |

Brightness, color mode, latency and send rate are live-applied to
running bridges (no restart needed). Discovery source and HA settings
take effect on the next provider load.

## Architecture / file map

```
lifx_lights_sync/
├── __init__.py      Plugin entry point; setup() lazily imports the
│                    provider (exact installed Hue pattern).
├── manifest.json    Plugin manifest (domain, deps, requirements).
├── strings.json     Config entry labels/help for the MA UI.
├── icon.svg / icon_dark.svg
├── constants.py     Config keys, defaults, mode names, visualizer request.
├── settings.py      Typed, defensive config readers (safe fallbacks).
├── color.py         Pure stdlib: RGB <-> HSBK, palette building rules.
├── analyzer.py      Pure stdlib: queued spectrum/peak/beat/palette ->
│                    one LightColorCommand per channel, per mode preset.
├── discovery.py     HA registries + config entries, LAN discovery
│                    (lifx-async), serial normalization, target merge.
├── ha_via_mass.py   HA discovery via MA's own hass provider
│                    (registries + config_entries/get over its
│                    existing connection; duck-typed, no MA imports).
├── lifx_output.py   LifxSender: snapshot / restore / rate-limited
│                    bulb and matrix writes via lifx-async.
├── bridge.py        Sendspin bridge manager + per-target bridge:
│                    virtual LIGHT players, visualizer/color roles,
│                    30 Hz render loop, 2 s debounced stop.
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
call shapes in `bridge.py` and `provider.py` were aligned against the
installed Music Assistant source and import-verified in the live
container; the remaining `TODO(live-verify)` markers are all in
`discovery.py` / `lifx_output.py` (lifx-async device semantics that
only a run against real lights can confirm).

## Roadmap

- [ ] First live run in Music Assistant: add the provider instance in
      the UI and confirm end-to-end streaming (registration, role
      callbacks and player removal are aligned to the installed
      source; lifx-async output semantics remain `TODO(live-verify)`).
- [ ] Measure sustained frame rate on a 16 x 8 ceiling and, if needed,
      switch matrix writes to the lifx-async `Animator` fast path
      (`device.animator.prepare()` + synchronous `send_frame()`).
- [ ] Per-target settings (e.g. different modes per room).
- [ ] Re-discovery / IP refresh on stream start (lights change IPs).
- [ ] Submit to the Music Assistant custom-provider community once
      the live path is verified.

## Credits

- Architectural blueprint: Music Assistant's **Hue Lights Sync**
  plugin (`providers/hue_entertainment`, reference revision 73c3e59)
  — the Sendspin visualizer bridge, virtual LIGHT player pattern and
  analyzer mode presets are modeled on it. No Hue-specific streaming
  code (DTLS / HueStream) is used.
- LIFX communication: **lifx-async** 8.0.0 by Avi Miller
  (https://github.com/Djelibeybi/lifx-async), also used by LedFx.
- LIFX(R) is a trademark of LIFX, Inc. This project is not affiliated
  with or endorsed by LIFX, Inc., Music Assistant, or Signify/Philips Hue.

See `NOTICE` for license details. This project is Apache-2.0 licensed.
