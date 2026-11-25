# Third-party licensing

calsync's own code and documentation are MIT-licensed (see `LICENSE`). calsync does **not** vendor or
copy any third-party source; it uses the components below at install or image-build time. This page is an
engineering summary, **not legal advice**. License data was read from the package metadata of the pinned
versions in `requirements.lock` (2026-11-25); re-check it when you bump a pin.

## Python runtime dependencies (`requirements.lock`)

| Package | Version | License (per metadata) |
|---|---|---|
| vdirsyncer | 0.21.0 | BSD-3-Clause |
| **aiostream** | 0.7.2 | **GPL-3.0-or-later** (transitive dependency of vdirsyncer) |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT |
| aiohttp-oauthlib | 0.1.0 | ISC |
| oauthlib | 4.0.1 | BSD-3-Clause |
| requests | 2.34.2 | Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| charset-normalizer | 3.5.2 | MIT |
| idna | 3.20 | BSD-3-Clause |
| click, click-log | 8.5.0 / 0.4.0 | BSD-3-Clause / MIT |
| attrs | 26.1.0 | MIT |
| tenacity | 9.2.1 | Apache-2.0 |
| aiohappyeyeballs, typing_extensions | 2.7.1 / 4.16.0 | PSF-2.0 |
| aiosignal, frozenlist, multidict, propcache, yarl | see lock | Apache-2.0 |

calsync's own modules use only the Python standard library; vdirsyncer is invoked as a **separate
process** (read-only local mirror), not imported.

## Test-only dependencies (not installed in the image)

pytest (MIT) and **Radicale** (GPL-3.0). Radicale runs as a separate local test server process on
loopback; it is neither imported nor distributed by this project.

## Container image components

The `Dockerfile` builds on the Debian-based `python:3.13-slim-bookworm` image (pinned by digest) and installs
`age`, `rclone`, `ca-certificates` and `tini` from Debian packages. These carry their own licenses
(BSD/MIT/GPL-family, see `/usr/share/doc/*/copyright` inside the image). The image also contains the
license texts of calsync (`/usr/share/doc/calsync/`).

## GPL-3.0-or-later (aiostream) and distributing images

- **Using or building the image yourself** for your own calendars does not trigger distribution duties.
- **Distributing** the image (pushing it to a public or third-party registry, handing it to others) means you
  distribute GPL-3.0-or-later code (aiostream, plus Debian GPL packages): you must make the corresponding
  source available (written offer or link to the exact sources of the pinned versions: PyPI sdist for
  `aiostream==0.7.2`, Debian source packages for the base layers), keep the license texts and notices in the
  image, and not impose additional restrictions on those components. The MIT license of calsync's own code is
  compatible with this; the combined image is distributed under the terms of each component's license.
- This repository **does not publish binaries or images**. If you publish one, you take on these obligations.
  A possible mitigation (not implemented) is to install vdirsyncer without aiostream-dependent features; that
  requires verification against vdirsyncer upstream and is out of scope here.

## Provider terms and trademarks

calsync is not affiliated with, endorsed by or sponsored by Apple Inc. or Google LLC. iCloud, Apple Calendar,
Google Calendar and Google OAuth are services/trademarks of their owners. You are responsible for complying with
their terms of service and API policies, including the OAuth app configuration you create.
