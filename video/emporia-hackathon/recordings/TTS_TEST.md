# TTS for hackathon VO — Hermes `text_to_speech` tool

Docs: [Tool Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-gateway.md)  
Runtime: `/opt/hermes/tools/tts_tool.py` + `managed_tool_gateway.py` (`openai-audio` vendor).

## Order to try

### 1. Built-in Edge (default) — **PASS**

Profile has `tts.provider: edge`. No Nous billing, no keys.

- `recordings/tts_test_edge.mp3` — short test
- `recordings/narration_edge.mp3` — full script
- `recordings/narration.wav` — convert for HyperFrames

Regenerate anytime: ask the agent to call **`text_to_speech`** with `recordings/narration.txt` and your `output_path`.

Better documentary voice (you run — agent cannot edit `config.yaml`):

```bash
hermes config set tts.edge.voice en-US-GuyNeural
```

---

### 2. OpenAI voices via **Nous Tool Gateway** (not `OPENAI_API_KEY`)

When `tts.provider` is **`openai`**, Hermes uses OpenAI **audio** (e.g. `gpt-4o-mini-tts`, voices `onyx`, `ash`, `cedar`) through the managed gateway if:

- You are logged in with **Nous Portal** (`hermes auth` / `hermes model`), and  
- Your account has **tool gateway** entitlement (`tool_gateway_entitled`), and  
- Either gateway auto-routes managed TTS **or** you set:

```yaml
tts:
  provider: openai
  use_gateway: true
  openai:
    model: gpt-4o-mini-tts
    voice: onyx
```

CLI equivalent:

```bash
hermes config set tts.provider openai
hermes config set tts.use_gateway true
hermes config set tts.openai.voice onyx
hermes config set tts.openai.model gpt-4o-mini-tts
```

Then one short test via agent **`text_to_speech`** → `recordings/tts_test_openai.mp3`.

If it fails, the error should mention **managed OpenAI audio** / run `hermes model` to refresh Nous login — not “set OPENAI_API_KEY”.

Direct key path (only if you *want* BYOK): `VOICE_TOOLS_OPENAI_KEY` or `OPENAI_API_KEY`, with `tts.use_gateway: false`.

---

### 3. Local HyperFrames Kokoro (optional)

```bash
pip install kokoro-onnx soundfile
npx hyperframes tts recordings/narration.txt --voice am_michael -o recordings/narration_kokoro.wav
```

Separate from Hermes; good offline backup.

---

## What we already proved

| Check | Result |
|--------|--------|
| `text_to_speech` + Edge | OK (`provider: edge` in tool result) |
| Full narration length | ~97s WAV |
| Nous gateway OpenAI | Not tested yet — needs your `tts.use_gateway` + Portal entitlement |

## After VO is final

`npx hyperframes transcribe recordings/narration.wav` (or gateway-backed STT if configured) for caption timings.