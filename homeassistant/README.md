# SoundServer × Home Assistant

Two ways to drive SoundServer from Home Assistant. The **custom integration** is
recommended — UI setup, speakers as `media_player` entities, and the sound library
in the media browser. The **rest_command package** is a no-custom-component
alternative.

Both assume SoundServer is reachable — ideally through the Caddy portal at
`http://<pi>/sound` (everything on port 80), or directly at `http://<pi>:5000`.

---

## Option A — Custom integration (recommended)

Every speaker becomes a **`media_player` entity**, so you pick speakers from
dropdowns, get a volume slider, and browse the sound library from the media
browser — all in the UI, no YAML.

### Install

1. Copy `homeassistant/custom_components/soundserver/` from this repo into your HA
   config folder, so that `<config>/custom_components/soundserver/manifest.json`
   exists.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration**, search
   **SoundServer**, and enter its URL when prompted:
   ```
   http://10.0.14.50/sound      # your Pi, via the Caddy portal (or http://10.0.14.50:5000)
   ```

That's it. The integration connects, and each speaker shows up as a media player
like `media_player.outdoor_dev_0`, grouped under one **SoundServer** device.

### Dropdowns everywhere

- **Speakers** are entities, so anywhere you target one — the `soundserver.play` /
  `soundserver.speak` services, a media-player card, `media_player.*` services —
  you get an **entity dropdown** of your speakers by name.
- **Sounds** are pickable from the **media browser**: add a *Media Control* card
  for a speaker (or open the Media panel), click browse, and choose from the live
  library. `media_player.play_media` fills the sound in for you.
- **Volume**: the media-player card's slider maps to the speaker's mixer.
- New speaker on the Pi? It appears automatically within ~5 min, or call
  **`soundserver.refresh_speakers`** to pick it up now.

### Use it

```yaml
# Announce a detection (speaker chosen from the entity dropdown in the UI)
service: soundserver.play
target:
  entity_id: media_player.outdoor_dev_0
data:
  sound: detected_garage.wav
  count: 2
  background: sprinkler.wav

# Text-to-speech to several speakers at once
service: soundserver.speak
target:
  entity_id:
    - media_player.outdoor_dev_0
    - media_player.indoor_dev_0
data:
  text: "Someone is at the front gate"

# Or play via the native media_player action (sound comes from the browser)
service: media_player.play_media
target:
  entity_id: media_player.outdoor_dev_0
data:
  media_content_type: music
  media_content_id: welcome.wav
```

Example automation:

```yaml
automation:
  - alias: "Announce person at gate"
    trigger:
      - platform: state
        entity_id: binary_sensor.gate_person
        to: "on"
    action:
      - service: soundserver.speak
        target:
          entity_id: media_player.outdoor_dev_0
        data:
          text: "Someone is at the gate"
```

---

## Option B — rest_command package (no custom component)

If you prefer plain config, use `packages/soundserver.yaml`:

1. Enable packages once in `configuration.yaml`:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. Copy `homeassistant/packages/soundserver.yaml` to `<config>/packages/`.
3. Edit the host in the URLs, then restart HA.

Call the generated services:

```yaml
service: rest_command.soundserver_play
data:
  sound: detected_garage.wav
  speaker: "2,0"

service: rest_command.soundserver_speak
data:
  text: "Someone is at the gate"
```

---

## Notes

- These call SoundServer's HTTP API; nothing runs inside HA. SoundServer itself
  keeps running as a systemd service on the Pi (it needs the sound cards).
- It is **not** a Supervisor "add-on" (those are containers that can't reach the
  Pi's ALSA devices / SIM800L cleanly) — a custom integration is the right fit
  for a networked appliance like this.
- Every endpoint the services use is documented, copy-paste ready, in the Sound
  Server dashboard's **API** tab.
