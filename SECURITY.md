# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
**Report a vulnerability** button (Security → Advisories) on this
repository, rather than opening a public issue. You should get a
response within a few days. There is no bug bounty.

## Security model

- **No credentials are required in the common setup.** When Music
  Assistant's Home Assistant provider is configured, this plugin
  borrows that existing authenticated connection and reads the HA
  device/entity/area registries through it. No Home Assistant token
  is entered into, stored by, or logged by this plugin in that mode.
- **Direct-HA fallback.** If you configure a Home Assistant URL and
  long-lived access token instead, the token is stored as a Music
  Assistant secret (`SECURE_STRING`) and used read-only against the
  HA WebSocket API. Use an `https://` URL for any non-local address:
  an `http://` URL sends the token over a plaintext WebSocket.
- **LAN discovery probes the network by design.** To find lights
  that broadcast discovery misses (notably across VLANs), the plugin
  unicast-probes every host of the subnets where lights are already
  known to live with a standard LIFX GetService packet. This is
  ordinary LIFX LAN discovery behavior, but it is a subnet-wide UDP
  probe — run the plugin only on networks you control.
- **The LIFX LAN protocol is unauthenticated** (a property of the
  protocol, shared by every LIFX integration): anything on the LAN
  can control the lights. This plugin adds no new exposure there.
- **Debug frame log** is off by default and logs only commanded
  brightness levels, never credentials or network identifiers beyond
  target names.
