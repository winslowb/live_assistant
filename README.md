# Live Assistant (TUI)

Self-contained terminal UI for live transcription (Vosk/ffmpeg) and AI analysis.

This directory is intended to be the project root for the Python app. In this repo it currently boots the main implementation that lives one directory up; to fully decouple into a separate repo, copy `live_assistant_main.py` and the `prompt_library/` directory into this folder (see Standalone mode below).

## Quick Start

Dependencies: ffmpeg, pactl (PulseAudio), Python 3, and optionally Vosk model.

- Run: `python3 live_assistant/live_assistant.py`
- Or: `bash live_assistant/install_and_run.sh`

The app will prompt for audio devices and (optionally) an LLM model. Notes are saved under `~/recordings/session_YYYYmmdd_HHMMSS/`.

## CLI flags

- `--source` <pulse_source_name>
- `--sink` <pulse_sink_name>
- `--vosk-model-path` <dir>
- `--llm-model` <model>
- `--openai-base-url` <url>

Flags override env and skip interactive prompts.

## Standalone mode (decoupled repo)

To make this directory fully standalone (e.g., its own `live_assistant` repo):

1) Move/copy into this directory from the parent project:
   - `../live_assistant_main.py`
   - `../prompt_library/` (entire folder)
2) After copying, running `python3 live_assistant/live_assistant.py` will use the local `live_assistant_main.py` automatically.

Until then, the launcher falls back to the parent directory’s `live_assistant_main.py` and `prompt_library/`.

