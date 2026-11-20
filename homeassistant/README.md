# Home Assistant — "Now Playing" + phone media notification

These files add a synced now-playing experience for the media center: a media
notification on your phone with working transport controls, and (optionally) a
full media_player card with a live scrubber inside HA.

## How it works

The bridge publishes discrete now-playing fields over MQTT and auto-discovers
them as sensors on the "Media Center" device:

| Sensor entity | Topic | Meaning |
|---|---|---|
| `sensor.media_center_media_state` | `avr-tv-bridge/media/state` | playing / paused / idle |
| `sensor.media_center_media_title` | `.../media/title` | show/movie/app title |
| `sensor.media_center_media_subtitle` | `.../media/subtitle` | S1·E3 — episode, or artist |
| `sensor.media_center_media_art` | `.../media/art` | poster URL (Plex; empty for Netflix) |
| `sensor.media_center_media_position` | `.../media/position` | seconds |
| `sensor.media_center_media_duration` | `.../media/duration` | seconds |
| `sensor.media_center_media_source` | `.../media/source` | `plex` or `mpris` |
| `sensor.media_center_media_type` | `.../media/media_type` | tvshow / movie / music / video |

Source is automatic: when **Plex** is the active player the bridge queries the
Plex Media Server API for the real episode/movie title, poster art, and live
position. Otherwise it uses **MPRIS** for Netflix and other Chrome web-apps
(state + a scrubber; note Netflix's web player only reports "Netflix" as the
title with no artwork — a limitation of the site, not the bridge). The snap
Plex HTPC exposes no MPRIS at all, which is exactly why the Plex API path
exists.

These field sensors are discovered `enabled_by_default: false` to keep the UI
tidy — **enable them once** under Settings → Devices → Media Center before the
templates/notification can read them.

## Files

- **`now_playing_package.yaml`** — core HA, no custom components. Transport
  scripts + the phone now-playing notification (title/art/state + prev /
  play-pause / next action buttons) + the action handler. Drop it in
  `<config>/packages/` (see the header for the one-time `packages:` include).
  Swap `notify.mobile_app_your_phone` for your device if the slug differs.
- **`media_player_hacs.yaml`** — OPTIONAL. A real `media_player` entity with a
  live scrubber card. Requires the HACS "Template Media Player" integration
  (core HA has no template/MQTT media_player platform). Reuses the transport
  scripts from the core package.

## Bridge config (Plex source)

To get the rich Plex metadata, set these in `config.env` on the host (they
are secrets — `config.env` is gitignored):

```
PLEX_BASE_URL=http://<your-pms>:32400
PLEX_TOKEN=<an X-Plex-Token>
```

Leave them empty to run MPRIS-only. See `config.env.example` for details.

## Android note

The companion-app notification shows title/art/state and the transport buttons
sync playback, but Android's native lock-screen *MediaSession* scrubber is
driven by an app's own media session, not by HA — so the HA notification is a
rich actionable notification, not a native MediaSession widget. Play/pause/next
state syncs; a live lock-screen seek bar is not something HA can inject.
