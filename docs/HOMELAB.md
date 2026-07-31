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
