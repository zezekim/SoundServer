# SoundServer × Home Assistant

Two ways to drive SoundServer from Home Assistant. The **custom integration** is
recommended (native services with a UI); the **rest_command package** is a
no-custom-component alternative.

Both assume SoundServer is reachable — ideally through the Caddy portal at
`http://<pi>/sound` (everything on port 80), or directly at `http://<pi>:5000`.

---

## Option A — Custom integration (recommended)

Adds three services: `soundserver.play`, `soundserver.speak`,
`soundserver.set_volume`, with field descriptions in the Developer Tools UI.

### Install

1. Copy the component into your HA config folder:
   ```
   <config>/custom_components/soundserver/
   ```
   (i.e. copy `homeassistant/custom_components/soundserver/` from this repo so
   that `<config>/custom_components/soundserver/manifest.json` exists.)

2. Add to `configuration.yaml`:
   ```yaml
   soundserver:
     url: "http://10.0.14.50/sound"   # your Pi, via the Caddy portal
     default_speaker: "2,0"           # optional — used when a call omits `speaker`
   ```
   > Find speaker ids in the Sound Server dashboard (API tab → `GET /api/speakers`)
   > or at `http://<pi>/sound/api/speakers`.

3. Restart Home Assistant.

### Use it

```yaml
# Announce a detection with a repeat and a background bed
service: soundserver.play
data:
  sound: detected_garage.wav
  speaker: "2,0"
  count: 2
  background: sprinkler.wav

# Text-to-speech
service: soundserver.speak
data:
  text: "Someone is at the front gate"
  # speaker omitted -> uses default_speaker

# Set a speaker's volume
service: soundserver.set_volume
data:
  card: "2"
  control: Speaker
  volume: 80
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
