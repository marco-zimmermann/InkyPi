# PiFrame deployment history

Phase 1 completed on 2026-10-10 (Europe/Zurich). Stock InkyPi and standard
plugins were tested successfully, including a physically confirmed display
refresh. Automatic service startup and memory-limit enforcement were verified
after reboot. This document retains the historical Phase 1 deployment; commands below are
operational instructions, not actions executed by reading this document.

## Phase 2 status — 2026-10-26

The running isolated release is `flight-duties-design-02b0bbd15633`, using the persistent
service drop-in under `/etc/systemd/system/inkypi.service.d/`. The original checkout
and previous release remain available for rollback. The Phase 1 source/launcher
paths below describe the original installation, not the current active release.

- A restores the remembered Weather instance; B restores the remembered METAR
  airport; C cycles the active Weather/METAR group; D opens Flight Duties.
- Weather rotation: Contra (`Weather`) → Little Salmon (`Salmon`). Lugano remains
  saved outside the rotation. METAR rotation: LSZL → LSZA.
- Weather defaults to Pillow at 800 × 480; explicit Chromium fallback remains.
- Automatic active-screen refresh is enabled for Weather, METAR and Flight Duties
  at 900 seconds;
  `manual_only=false`, `startup=false`. It never rotates playlists or locations.
- The diagnosed historical `memory_critical` pause was deliberately recovered
  after backup and approval. Resource protection remains enabled; new pauses
  require diagnosis and must never be cleared automatically.
- At the last check, Flight Duties was selected, Weather memory was Salmon,
  METAR memory was LSZL, and
  displacement/display uncertainty were false. Selection is mutable runtime state,
  not a setting to copy blindly into a new release.
- The first automatic Pillow Weather cycle took 6.4 s generation / 35.6 s total,
  peaked at 102.4 MiB kernel cgroup memory, and used no swap or limit/OOM events.
  Limits remain MemoryMax=200 MiB and CPUQuota=40%. This is a first-cycle result,
  not a long-running reliability guarantee.

Flight Duties uses a private FlightLogger iCalendar subscription and a Pillow
agenda: TODAY before 18:00 and TOMORROW afterward in Europe/Zurich, evaluated on
the next eligible refresh. Its redesigned screen was physically confirmed, with
2.3 s generation, 28.7 s display, 92.4 MiB kernel peak and no swap/limit/OOM events.
See [Flight Duties](FLIGHT_DUTIES.md) for Button D, private credentials/cache,
planning-day semantics and deployment evidence.

Full scheduler and recovery semantics are in [BUTTON_NAVIGATION.md](BUTTON_NAVIGATION.md).
[Pillow Weather details](WEATHER_RENDERING.md) include benchmark limits and physical
validation. Existing saved settings, caches and provider calculations were preserved.

## Hardware and software

| Component | Verified baseline |
| --- | --- |
| Development computer | Mac |
| Host / SSH | `PiFrame`, `ssh zimmi@192.168.1.153` |
| Board | Raspberry Pi Zero 2 W Rev 1.0 |
| OS | Raspberry Pi OS, Debian 13 Trixie, ARM64 (`aarch64`) |
| Kernel | `6.18.50+rpt-rpi-v8` |
| Python | `3.13.5` |
| Display | Pimoroni Inky Impression Spectra 7.3 inch |
| Controller / revision | E673, EEPROM variant **22**, original Spectra revision |
| Display geometry | **800 × 480**, horizontal |
| Palette | Six colours: black, white, yellow, red, blue, green |
| Driver | Inky `2.4.0`, `inky.inky_e673.Inky` |
| Web interface | `http://192.168.1.153/` or `http://PiFrame.local/` when mDNS resolves |

The revision was read from EEPROM on I²C bus 1, address `0x50`. The 29-byte
record uses `<HHBBB22p`; display variant is byte offset 6. Variant 22 selects
E673 automatically through `inky.auto.auto()`. It is distinct from variant 26,
the newer AC-waveform revision. Do not select a driver from the product name
alone or use `impression73` as InkyPi's display type. InkyPi defaults to `inky`;
the Pimoroni manual selection name for this display is `spectra73`.

## Repository relationship and deployed commit

- Fork: <https://github.com/marco-zimmermann/InkyPi>
- Original upstream: <https://github.com/fatihak/InkyPi>
- Working application commit: **`02675af57bb60a64c095d178a778a4b6bd0e9b7a`**.
- The fork was fast-forwarded from `8d08acd` by 67 upstream commits, without
  rewriting history. Local `main`, GitHub fork `main`, and fetched
  `upstream/main` matched the working commit at synchronization.
- Mac remotes: `origin=git@github.com:marco-zimmermann/InkyPi.git` and
  `upstream=https://github.com/fatihak/InkyPi.git`.
- The Pi checkout was cloned from the fork over HTTPS and remains at a detached
  HEAD for the exact deployed commit. It does not automatically follow `main`.

GitHub SSH authentication is configured on the Mac. Do not copy private keys
to the Pi. Public-fork HTTPS cloning on the Pi needs no GitHub credentials.

## Installation architecture

| Path on Pi | Purpose |
| --- | --- |
| `/home/zimmi/InkyPi` | Fork checkout and generated application data |
| `/usr/local/inkypi` | Root-owned installation directory, mode `755` |
| `/usr/local/inkypi/src` | Symlink to `/home/zimmi/InkyPi/src` |
| `/usr/local/inkypi/venv_inkypi` | Dedicated Python virtual environment |
| `/usr/local/bin/inkypi` | Root-owned executable launcher, mode `755` |
| `/usr/local/inkypi/cli/inkypi-plugin` | Installed plugin CLI, mode `755` |
| `/etc/systemd/system/inkypi.service` | Installed unit, mode `644` |
| `/run/inkypi` | systemd-created runtime/working directory |
| `src/config/device.json` | Generated device settings, relative to Pi checkout |
| `src/static/images/current_image.png` | Latest image saved by the application |
| `src/static/images/plugins/` | Plugin instance image files |

The launcher activates the venv and runs
`python -u /home/zimmi/InkyPi/src/inkypi.py`. Python dependencies were installed
only in the venv, preferring wheels. `spidev` compiled successfully with
`MAKEFLAGS=-j1`; `CMAKE_BUILD_PARALLEL_LEVEL=1` was also set. All required imports
and `pip check` passed. The full installer was executed as reviewed stages,
not as an unattended script.

Generated settings include `startup=false`, `orientation=horizontal`, and
`resolution=[800,480]`. Resolution, default playlist, and refresh metadata are
written by the application. `startup=false` suppresses the startup splash
refresh; it does **not** disable manual refreshes or configured playlist
scheduling. The baseline initially had an empty default playlist.

Five generated vendor assets were downloaded with HTTP-error checks and
validated as nonempty CSS/JavaScript rather than HTML error pages:

- `src/static/styles/select2.min.css`: Select2 `4.1.0-beta.1`.
- `src/static/scripts/select2.min.js`: Select2 `4.1.0-beta.1`.
- `src/static/scripts/jquery.min.js`: jQuery `3.6.0`.
- `src/static/scripts/calendar.min.js`: FullCalendar `6.1.17`.
- `src/static/scripts/chart.js`: Chart.js from the repository's unversioned CDN
  URL; the source commit alone does not pin this downloaded asset.

Vendor assets are Git-ignored; `device.json` is untracked. Preserve runtime
settings and downloaded assets when deploying. The vendor script's `curl -sL`
does not reject HTTP errors; use `curl -fL` and validate responses on regeneration.

## System packages and Python versions

Installed Trixie system requirements:

```text
libtiff-dev python3 python3-venv python3-pip avahi-daemon
libopenblas-dev libopenjp2-7 chromium-headless-shell libfreetype-dev
fonts-noto-color-emoji swig liblgpio-dev libheif-dev jq
```

`libfreetype-dev` provides the repository's `libfreetype6-dev` requirement.
Existing `git`, `curl`, `build-essential`, and `python3-dev` supported deployment
and compilation. Chromium Headless Shell was verified at `154.0.8037.92`.
Ordinary Chromium does not replace the application's hardcoded headless-shell
executable. No extra earlyoom service was installed during the staged setup;
the existing systemd-managed zram was retained.

Installed direct Python dependencies:

```text
Flask==3.1.3                 python-dotenv==1.2.2
inky==2.4.0                  requests==2.33.0
urllib3==2.7.0               Werkzeug==3.1.6
Pillow==12.3.0               pi-heif==1.3.0
pytz==2025.2                 openai==2.20.0
numpy==2.4.2                 icalendar==6.3.2
recurring-ical-events==3.8.0  psutil==7.2.2
cysystemd==2.0.1             waitress==3.0.2
feedparser==6.0.11           astral==3.2
```

Relevant resolved hardware dependencies: `gpiod==2.5.0`,
`gpiodevice==0.1.0`, `spidev==3.8`, and `smbus2==0.6.1`.
Packaging tools: `pip==26.2.1`, `setuptools==84.0.0`, `wheel==0.48.0`.
Astral and transitive dependencies are not fully pinned by the repository;
future installations can resolve different versions. Inspect the deployed
environment with `venv_inkypi/bin/python -m pip list --format=freeze` using the
absolute venv path above. This is a baseline record, not a complete lockfile.

## SPI, I²C, and memory cgroups

`/boot/firmware/config.txt` retains these enabled parameters:

```ini
dtparam=i2c_arm=on
dtparam=spi=on
```

Its final `[all]` section includes:

```ini
[all]
dtoverlay=spi0-0cs
```

This frees hardware chip-select GPIOs for the E673 driver's GPIO-controlled
selection. `/dev/i2c-1`, `/dev/spidev0.0`, and `/dev/gpiochip0` were verified.
Driver pins are GPIO8 (CS), GPIO22 (DC), GPIO27 (reset), and GPIO17 (busy).
Before the first test, these pins were unclaimed. Two spidev nodes are not a
requirement for this configured E673 deployment.

`/boot/firmware/cmdline.txt` remains a single line with all previous parameters
preserved and `cgroup_enable=memory` appended. The device tree supplies an
earlier `cgroup_disable=memory`; the later enable parameter overrides it.
The kernel has `CONFIG_MEMCG=y`, with v2 support and no legacy `MEMCG_V1`.
Check `/sys/fs/cgroup/cgroup.controllers`, not just `/proc/cgroups`.

The memory controller was verified after reboot. When InkyPi runs,
`/sys/fs/cgroup/system.slice/inkypi.service/memory.max` is `209715200` bytes.
Inactive services may have no live cgroup or `MemoryCurrent` value.

## Service and resource configuration

The installed unit matches `install/inkypi.service`:

```ini
[Unit]
Description=InkyPi App
After=network-online.target
Wants=network-online.target

[Service]
User=root
RuntimeDirectory=inkypi
WorkingDirectory=/run/inkypi
ExecStart=/usr/local/bin/inkypi run
Restart=on-failure
RestartSec=60
KillSignal=SIGINT
StandardOutput=journal
StandardError=journal
CPUQuota=40%
MemoryMax=200M

[Install]
WantedBy=multi-user.target
```

The service is enabled and running, with automatic startup verified by a
changed boot ID and HTTP 200 after reboot. CPU quota is 40% of one CPU's time,
not 40% of all four cores. The memory limit covers the service's processes,
including rendering children and accounted cache/kernel memory. Swap is not
separately capped by this unit. Do not change limits based on Python RSS alone.

The Pi has about 415 MiB usable RAM, 414 MiB zram swap, and approximately 22 GB
free disk at baseline. Readings varied with workload: initial service memory
peaked around 106 MiB; post-reboot verification recorded about 60 MiB peak.
These are observations, not guarantees for all plugins. Chromium rendering is
more demanding than the Pillow-only physical test. No limit-hit, OOM, or
unexpected restart events occurred during the recorded checks.

## SSH deployment and operation

Connect from the Mac:

```bash
ssh -o BatchMode=yes zimmi@192.168.1.153
```

For a fresh checkout, use the fork and an explicitly reviewed commit:

```bash
git clone https://github.com/marco-zimmermann/InkyPi.git /home/zimmi/InkyPi
cd /home/zimmi/InkyPi
git checkout --detach 02675af57bb60a64c095d178a778a4b6bd0e9b7a
git rev-parse HEAD
git status --short
```

For future deployment, first review changes on the Mac (`git fetch upstream`,
compare histories), then commit/push approved changes to the fork. On the Pi,
back up runtime data, confirm no unexpected tracked changes, stop InkyPi, fetch
the fork, and check out the reviewed commit. The source symlink means checkout
changes immediately affect the installed application. Review dependency,
launcher, CLI, and unit changes together before restarting; the newer launcher
requires the `run` argument. Do not blindly run the full installer on an existing
deployment: it removes `/usr/local/inkypi`, alters boot settings, and installs
additional services. Inspect updates and retain visible exit-status checks.

Service commands on the Pi (mutating commands require approval):

```bash
systemctl status inkypi --no-pager
systemctl is-enabled inkypi
sudo -n systemctl stop inkypi
sudo -n systemctl start inkypi
sudo -n systemctl restart inkypi
sudo -n systemctl enable inkypi
sudo -n systemctl disable inkypi
```

Passwordless sudo was verified during setup. If `sudo -n` requires a password,
stop and use an approved interactive session; do not alter sudo/authentication
permissions to work around it. Allow initialization to finish before testing
HTTP: observed startup took tens of seconds, especially after reboot.

```bash
curl -f -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1/
systemctl show inkypi -p MemoryCurrent -p MemoryPeak -p MemoryMax -p NRestarts
cat /sys/fs/cgroup/system.slice/inkypi.service/memory.events
free -h
df -h /
```

## Logs, troubleshooting, and rollback

Application stdout/stderr go to the systemd journal; no application log file is
configured by `src/config/logging.conf`.

```bash
sudo -n journalctl -u inkypi -b --no-pager
sudo -n journalctl -u inkypi -n 100 --no-pager
sudo -n journalctl -u inkypi -f
sudo -n journalctl -k -b -p err --no-pager
systemctl --failed --no-pager
/usr/local/inkypi/venv_inkypi/bin/python -m pip check
```

- **HTTP unavailable:** check service state and journal first; initialization
  may still be running. Check `ss -ltn 'sport = :80'` for conflicts.
- **Detection failure:** verify I²C device permissions, EEPROM revision, and
  installed Inky version. A detection error does not justify a fallback to the
  older seven-colour driver. Do not initialize or refresh while diagnosing
  without approval.
- **GPIO/SPI errors:** check `gpioinfo`, required device nodes, overlay placement,
  and competing processes. Stop InkyPi before a direct-driver test to avoid
  concurrent access. Do not retry failed physical refreshes automatically.
- **Memory failure:** inspect `memory.events` (`max`, `oom`, `oom_kill`), peak
  usage, journal, and swap. Stop on repeated failures; review workload and limits
  before changing settings. A configured limit is ineffective when the memory
  controller is missing.
- **Display appearance:** successful software execution does not establish
  physical correctness. Confirm the image on the panel. The initial test used
  exactly one E673 refresh of a six-colour 800 × 480 Pillow image, without
  Chromium or network APIs; the user subsequently confirmed physical testing.

Retained Pi-side rollback records:

```text
/home/zimmi/inkypi-baseline-il933bSI/
/boot/firmware/config.txt.pre-inkypi-20261010T185341722283Z
/boot/firmware/cmdline.txt.pre-memcg-20261010T191136756780Z
```

The baseline directory records packages, service states, OS, disk, RAM, and
swap. After approval, rollback can stop/disable InkyPi, restore the specific
boot backup, and reboot. Restoring the old command line disables memory-limit
enforcement. Review intervening changes before restoring whole files. Preserve
device settings, plugin data, and secrets before changing checkout or removing
installation paths. Do not blanket-autoremove packages. Stopping the service
does not erase the panel's retained image.

## Future custom plugin development

Start with [Building InkyPi Plugins](building_plugins.md) and
[Development Quick Start](development.md). Use a feature branch on the Mac and
the mock-display `--dev` mode for initial work. Install development dependencies
in a separate Mac venv, not the Pi's system Python.

A plugin lives in `src/plugins/<id>/`, subclasses `BasePlugin`, implements
`generate_image(settings, device_config)`, and returns a Pillow image. Register
it with `plugin-info.json`; add an optional settings template and icon. Prefer
Pillow for simple layouts, respect the device's dimensions, bound network
timeouts, and close image/resources promptly on this low-RAM board.

Deploy reviewed commits from the fork, verify imports and memory peaks, and
request approval before a physical refresh. Keep API keys/private credentials
outside tracked files using existing environment-key mechanisms; never include
them in documentation, logs, or commits. The plugin CLI's install/uninstall
commands mutate plugin directories and restart the service, so inspect them
before use.
