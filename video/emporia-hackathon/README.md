# Emporia hackathon video — quick start

Narrative: **retro-modern intro / interstitials / outro** (HyperFrames) ↔ **clean browser plates** (OBS).

Read first:

- `DESIGN.md` — colors, fonts, imaging rules  
- `NARRATIVE.md` — beat sheet & assembly  
- `SCRIPT.md` — VO  

```bash
# One-time
bash /opt/data/profiles/hackathon_hermes/skills/creative/hyperframes/scripts/setup.sh
npx hyperframes doctor

# Relay + demo data
cd /opt/data/profiles/hackathon_hermes/emporia
.venv/bin/python installer/install.py --bootstrap-test

# Scaffold HyperFrames project (after intro/interstitial/outro dirs exist)
cd video/emporia-hackathon
npx hyperframes init . --non-interactive --example kinetic-type
# Then replace example comps with compositions/ from this repo folder
```

Record browser clips into `recordings/browser/` per `NARRATIVE.md`.  
Place VO in `recordings/narration.wav`.

Skills: `hyperframes`, `emporia-dev`, `srcl-terminal-ui`.

## Narration (AI voice)

**Done:** Hermes built-in `text_to_speech` (config `tts.provider: edge`, no API cost).

- Source text: `recordings/narration.txt`
- MP3: `recordings/narration_edge.mp3`
- WAV for HyperFrames: `recordings/narration.wav` (~97s)

Regenerate or change voice in `~/.hermes/profiles/hackathon_hermes/config.yaml`:

```yaml
tts:
  provider: edge
  edge:
    voice: en-US-GuyNeural   # calm technical; or ChristopherNeural, DavisNeural
```

Then in Hermes chat: use the `text_to_speech` tool with `recordings/narration.txt` content and `output_path` under `recordings/`.

**Alternative (HyperFrames skill, local Kokoro):**

```bash
pip install kokoro-onnx soundfile   # once
npx hyperframes tts recordings/narration.txt --voice am_michael --output recordings/narration_kokoro.wav
```

Caption timings: `npx hyperframes transcribe recordings/narration.wav --model small.en --language en`