# Live Assistant (TUI)

Self‑contained terminal UI for live audio capture, optional on‑device transcription (Vosk/ffmpeg), and AI analysis in a split‑pane curses interface. Sessions are recorded to disk and summarized into Markdown.

This folder is the project root. The launcher prefers a local `live_assistant_main.py` and falls back to the repository‑root copy if present.

**What it does**
- Captures audio from a PulseAudio source via `ffmpeg` and saves `audio.wav`.
- Performs live ASR with Vosk (if a model is available), showing transcript on the left.
- Streams rolling analysis on the right: actions, questions, decisions, and topics.
- Adds markers and free‑form notes during recording.
- Supports an “Interview” mode to capture a question, then auto‑answer with an LLM.
- Offers an interactive chatbot so you can query the meeting in real time.
- Lets you mount extra context files or URLs on the fly while the session runs.
- Writes Markdown notes and an optional executive summary at the end of the session.
- Can take external pdfs, markdown or other text files for ai context

**Outputs**
- `~/recordings/session_YYYYmmdd_HHMMSS/`
  - `audio.wav` – raw mono 16 kHz PCM recording
  - `notes_YYYYmmdd_HHMMSS.md` – session report (metadata, lists, Q&A, summary)
  - `assistant.log` – internal log

**Requirements**
- `python3`, `ffmpeg`, `pactl` (PulseAudio), terminal that supports curses.
- Python packages: `vosk` (optional, for live ASR), `requests` (for LLM calls).
- Optional: Vosk model directory (e.g., `~/.cache/vosk-model-small-en-us-0.15`).

**Quick Start**
- Run directly: `python3 live_assistant/live_assistant.py`
- Or installer: `bash live_assistant/install_and_run.sh`
  - Installs system and pip deps (Debian/Ubuntu), downloads a Vosk model by default.

The app will prompt for capture/playback devices and an optional LLM model. Notes are saved under `~/recordings/session_YYYYmmdd_HHMMSS/`.

**CLI Flags**
- `--source` `<pulse_source_name>` – capture source (e.g., a monitor for system audio)
- `--sink` `<pulse_sink_name>` – playback sink (optional)
- `--vosk-model-path` `<dir>` – Vosk model directory (enables live ASR)
- `--llm-model` `<name>` – model name for analysis (e.g., `gpt-4o-mini`)
- `--openai-base-url` `<url>` – OpenAI‑compatible API base URL
- `-C`, `--context` `<path>` – file or directory used as context; repeatable. Supports `.pdf`, `.md`, `.txt`.
- `--config` `<path>` – path to a TOML config (default: `~/.config/live_assistant/config.toml` or `$XDG_CONFIG_HOME/live_assistant/config.toml`)
- `--profile` `<name>` – profile inside the config (default/env: `LIVE_ASSISTANT_PROFILE` or `default`)

Flags override environment and skip interactive prompts. View help: `python3 live_assistant/live_assistant.py --help`.

Context files are loaded at startup and used to ground both the rolling analysis and interview answers. PDFs are extracted via `pdftotext` if available (install `poppler-utils`), otherwise convert to `.txt`/`.md` first.

Need more context mid-meeting? Press `C` in the TUI to add another `.pdf`/`.md`/`.txt` resource or an `http(s)` URL without stopping the recording.

Examples:
- `python3 live_assistant/live_assistant.py -C ~/resume.pdf`
- `python3 live_assistant/live_assistant.py -C ./prev_notes/ -C ./agenda.md`

**Environment Variables**
- `OPENAI_API_KEY` – enables LLM analysis and executive summary
- `OPENAI_BASE_URL` – override base URL (optional)
- `LLM_MODEL` / `OPENAI_MODEL` – default model if not passed via CLI
- `VOSK_MODEL_PATH` – default Vosk model directory
- `PROMPT_DIR` – extra directory to scan for prompt `.md` files
- `SUMMARY_PROMPT` – path to a specific summary prompt `.md`
- `CHAT_PROMPT` – path to a chatbot system prompt `.md`
- `CONTEXT_PATHS` – colon‑separated list of context paths (files or directories)
- `LIVE_ASSISTANT_CONFIG` – overrides default config path
- `LIVE_ASSISTANT_PROFILE` – selects config profile if `--profile` not provided

**Config & Profiles**
- Create `~/.config/live_assistant/config.toml` (or set `LIVE_ASSISTANT_CONFIG`). See `live_assistant/config.example.toml` for a template.
- Define profiles under `[profiles.<name>]` with keys:
  - `source`, `sink` – PulseAudio device names
  - `vosk_model_path` – directory to a Vosk model
  - `llm_model`, `openai_base_url` – LLM defaults (keep API keys in env)
  - `context` – list of files/dirs/URLs to preload as grounding context
  - `prompt_dir`, `summary_prompt`, `chat_prompt` – prompt discovery/overrides
  - `debug` – boolean to enable verbose logging
- Select a profile via `--profile <name>` or `LIVE_ASSISTANT_PROFILE`.
- Precedence (highest → lowest): CLI flags → environment → config profile.
- Examples:
  - `python3 live_assistant.py --profile work`
  - `LIVE_ASSISTANT_PROFILE=default python3 live_assistant.py`

**Prompt Library**
- Prompts are discovered in `prompt_library/` and any directory containing “prompt”.
- At startup, you can pick a summary/analysis template. Setting `SUMMARY_PROMPT` bypasses the picker.
- Choosing an “interview” prompt enables interview mode (see Shortcuts).
- The chatbot uses `prompt_library/chatbot/system.md` by default; override with `CHAT_PROMPT`.

**Shortcuts (TUI)**
- `q` – quit
- `m` – add a timestamped marker
- `n` – add a free‑form note (Enter save, Esc cancel, Backspace delete)
- `j` / `k` – scroll transcript pane
- `/` – search transcript; then `n`/`N` to jump next/prev
- `\` – filter transcript lines by substring
- `i` – interview mode: start/stop capturing a question; answer is generated via LLM
- `c` – ask the chatbot a question grounded in the recent transcript and context
- `C` – add a context file or URL for immediate use by analysis/chatbot
- `Tab` – switch focus between transcript and analysis/chat panes
- `Esc` – dismiss sticky alerts and return focus to the transcript pane

**Audio Devices (PulseAudio)**
- List sources: `pactl list short sources`
- List sinks: `pactl list short sinks`
- Use `--source` and `--sink` to avoid interactive selection in non‑TTY contexts.

**Installation Notes**
- Script‑based: `install_and_run.sh` handles `apt`, `pip`, and Vosk model download.
- Optional: for PDF context extraction install `pdftotext` (`sudo apt-get install poppler-utils`).
- Manual pip: `python3 -m pip install --upgrade vosk requests`
- Vosk models (examples):
  - Small: `~/.cache/vosk-model-small-en-us-0.15`
  - Medium: `~/.cache/vosk-model-en-us-0.22`

**Executive Summary**
- After quitting the TUI, a longer summary can be generated if `OPENAI_API_KEY` is set.
- Uses the full transcript (last slice) with a structured prompt; output is added to the notes file.

**Troubleshooting**
- “No PulseAudio sources found” or connection refused:
  - Ensure PulseAudio is running and accessible from your environment.
  - In containers/WSL, you may need to forward PulseAudio or use a null source.
- No live transcript but recording works:
  - Verify `vosk` is installed and `--vosk-model-path` points to a valid model dir.
- TUI fails to draw in non‑interactive environments:
  - Run from a real terminal; pass flags to skip interactive prompts when needed.
