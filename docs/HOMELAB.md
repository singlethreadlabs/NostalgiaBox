# NostalgiaBox homelab deployment

The headless server creates one shared, persistent channel schedule and serves
it to browsers on a trusted LAN. Source media is never modified.

## Host preparation

These instructions assume Debian or Ubuntu, Docker Engine with the Compose
plugin, and media already mounted below `/srv/media`.

```bash
sudo install -d -m 755 /srv/nostalgiabox/config
sudo install -d -m 775 -o 10001 -g 10001 \
  /srv/nostalgiabox/data /srv/nostalgiabox/cache
sudo cp config.server.example.yaml /srv/nostalgiabox/config/config.yaml
sudo editor /srv/nostalgiabox/config/config.yaml
```

Every configured media path must exist below `/media` inside the container,
which corresponds to `/srv/media` on the host. The server refuses paths outside
that root.

Each folder under a channel's `shows` list is treated as one show. Scheduling
rotates between show folders before choosing an episode, so shows remain
balanced even when their episode counts differ. Optional `bumpers` and
`commercials` entries may be individual video files or folders and are inserted
between shows.

Set the bind address to the server's static or DHCP-reserved LAN address:

```bash
cp .env.example .env
editor .env
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs -f nostalgiabox
```

Open `http://SERVER_LAN_IP:8080`. Add a local DNS record for
`nostalgiabox.local` if the router supports it. The Compose default binds only
to `127.0.0.1`; it must be changed deliberately.

Also restrict TCP port 8080 to the private LAN in the host firewall. Do not add
router port forwarding or publish the service through an internet-facing
reverse proxy.

## Operations

Check health:

```bash
curl --fail http://SERVER_LAN_IP:8080/api/v1/health
```

Rescan configured media after adding or changing files:

```bash
curl --fail -X POST http://SERVER_LAN_IP:8080/api/v1/admin/refresh
```

Back up the configuration and SQLite database:

```bash
docker compose stop nostalgiabox
sudo tar -C /srv/nostalgiabox -czf nostalgiabox-backup.tgz config data
docker compose start nostalgiabox
```

The cache is disposable and should not be backed up. Playback sessions expire
after five idle minutes and their FFmpeg processes and segment directories are
removed.

Upgrade and roll back:

```bash
git pull
docker compose build
docker compose up -d
```

Record the previously deployed Git commit before upgrading. To roll back,
check out that commit, rebuild, and run `docker compose up -d`. Back up the
SQLite file before upgrades that introduce schema migrations.

## Episode rotation and ordered runs

The server remembers aired selections per channel and show in SQLite. It picks
unseen episodes first, then the least recently aired episode. Each show gets a
turn before another show repeats. History survives restarts, future schedule
rebuilds, and the seven-day schedule cleanup.

Use `ordered_shows` on a channel to play selected show folders in natural
season/episode filename order. Each run wraps after its last available episode
and resumes after the last aired episode when future programming is rebuilt.
Folders omitted from this list retain the no-early-repeat rotation.

```yaml
channels:
  - number: 10
    name: Nova Action
    shows:
      - /media/Shows/Dragon Ball Z (1989)
      - /media/Shows/Pokémon (1997)
    ordered_shows:
      - /media/Shows/Dragon Ball Z (1989)
```

The prepared `config.homelab.yaml` enables ordered runs for Dragon Ball Z and
Pokémon on Nova Action. Editing configuration takes effect on server restart.

## Bumper rotation

After each episode, the server plays a configured channel bumper. When a
channel has multiple bumpers, each plays before another repeats. Optional
configured commercials follow the bumper, then the next episode begins.
Add more completed bumper videos to a configured bumper folder to include
them through automatic media intake.

Future programming is rebuilt when the catalog changes or the server starts.
Existing TV API fields and delivery modes are preserved.

## Automatic media intake

The worker scans configured folders every 60 seconds. A new or changed file must
have the same size and modification time for at least 120 seconds across scans
before intake probes it. Files are validated for a real video stream, finite
positive duration, container, and codecs. The server chooses direct playback,
remuxing, or transcoding from that information. This check reads media metadata
and does not decode every frame.

Initial provisioning and newly configured channels are probed immediately.
Later additions to existing channels wait for stable observations, including
after a server restart. Keep downloads under temporary names or outside the
configured folders, then rename completed files into place for a reliable
completion boundary.

Invalid additions are skipped while valid additions enter rotation. Invalid
files are retried when their size or modification time changes. Transient probe
timeouts are retried on subsequent scans. A missing configured folder or an
empty show channel rejects the refresh and retains the previous catalog and
schedule. File probes run before the short database update, and file metadata
is checked again before committing the catalog and schedule together.

The manual `POST /api/v1/admin/refresh` endpoint validates immediately. Use it
for completed additions when you want to bypass the observation delay.

`GET /api/v1/health` includes `media_scan_status`, `last_media_scan_at`,
`pending_media`, and `rejected_media`. Scan failures are also written to the
server logs.

## Browser controls

- Up/down arrows or `CH+`/`CH-`: change channels
- Number keys or the channel field: direct tuning
- `M`: mute
- `F`: fullscreen

The browser is a validation client. The Fire TV application uses the same
`/api/v1` channel and playback-session contract, preferring direct media for
normalized files and HLS for incompatible media.

## Fire TV client

The client under `firetv/` is the primary TV interface. It direct-plays media
when the playback descriptor reports `delivery_mode: direct` and otherwise
uses the descriptor's HLS URL. Build and sideload instructions are in
`firetv/README.md`.

After installing, verify several normalized files at early, middle, and late
schedule offsets. During direct playback, `/api/v1/health` should report zero
active FFmpeg processes. An incompatible test file should report one active
FFmpeg process while its HLS session is playing.
