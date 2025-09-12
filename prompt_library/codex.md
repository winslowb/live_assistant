You are my pair-programmer working inside this repo: ~/projects/live_assistant (or the live_assistant/ directory in this repo).

CONTEXT
- App: a terminal "Live Assistant" TUI for real-time ASR + analysis.
- Primary files to work in:
  - live_assistant/live_assistant_main.py  (curses 2-pane UI, Vosk capture, OpenAI analyzer)
  - live_assistant/live_assistant.py       (boots the main script in the same folder)
  - live_assistant/install_and_run.sh      (installs deps)
- Current capabilities:
  - Two panes: LEFT transcript, RIGHT rolling GPT analysis; title bar shows Source, Sink, ASR model, and LLM model. 
  - OpenAI-compatible POST /v1/chat/completions using env: OPENAI_API_KEY, optional OPENAI_BASE_URL, model via LLM_MODEL/OPENAI_MODEL.
  - Notes written under ~/recordings/session_YYYYmmdd_HHMMSS/. 
  - Vosk partial/final results supported; ffmpeg pulls 16k mono PCM from PulseAudio.
- Python 3.12, Linux (PulseAudio), curses UI. 
- Keep code production-ready, self-contained, and runnable.
- Write clean and efficent code.

TASK
1) Implement/verify these UX requirements end-to-end:
   - TRUE streaming: show Vosk partials live, replace line with finalized phrase when ready.
   - Visual vertical divider between panes.
   - Word wrap in BOTH panes.
   - LEFT pane UX:
     - Start scrolling at TOP of pane (no bottom-stick on first render).
     - Render finalized utterances as bullet points (e.g., "• text…").
     - Reserve the last row for the current partial (with an ellipsis).
   - Theme: apply a Gruvbox Dark-inspired scheme in curses (clear, high-contrast title/footer; subtle pane bodies; dim style for partials).
   - Title bar keeps: Src, Sink, ASR:<vosk-model>, LLM:<model>.
   - Keyboard: q quit, m mark, n note (prompt inline at footer), keep non-blocking input.

2) Add non-interactive flags so prompts can be skipped:
   - --source <pulse_source_name>
   - --sink <pulse_sink_name>
   - --vosk-model-path <dir> (overrides auto-detect)
   - --llm-model <model>
   - --openai-base-url <url>
   Behavior: flags override env and interactive chooser.

3) Reliability & structure:
   - Handle terminal resizes; recompute widths; never crash on small terminals.
   - Fail gracefully if Vosk or model missing (record-only mode with clear on-screen notice).
   - Analyzer thread: update RIGHT pane every ~5s; keep last snapshot for notes file.
   - Ensure clean shutdown: terminate ffmpeg, join
