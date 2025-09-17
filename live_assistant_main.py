#!/usr/bin/env python3
import os
import sys
import time
import json
import threading
import queue
import subprocess
from datetime import datetime
import curses
import textwrap
import re
from typing import Optional, List, Tuple, Dict
from pathlib import Path
import wave
import argparse
from html import unescape
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import urllib.error

# Debug/verbose logging toggle
DEBUG = False

# Optional Vosk import
try:
    import vosk  # type: ignore
except Exception:
    vosk = None


def list_pulse_devices(kind: str) -> List[Tuple[str, str]]:
    try:
        out = subprocess.check_output(["pactl", "list", "short", kind], text=True)
    except Exception as e:
        print(f"[!] Failed to list PulseAudio {kind}: {e}")
        return []
    items: List[Tuple[str, str]] = []
    for line in out.strip().splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            idx, name = parts[0], parts[1]
            items.append((idx, name))
    dbg(f"Listed {kind}: {len(items)} items")
    return items


# ----------------------
# Context loading helpers
# ----------------------

ACCEPTED_CONTEXT_EXTS = {'.txt', '.md', '.markdown', '.pdf'}
_WARNED_NO_PDFTOTEXT = False
_CONTEXT_FALLBACK_BULLET = "- No information available; meeting dialogue lacked references to provided external context sources or citations currently recorded."


def canonical_context_id(raw: str) -> Optional[str]:
    candidate = (raw or '').strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme in {'http', 'https'}:
        return candidate
    try:
        p = Path(candidate).expanduser().resolve()
        return str(p)
    except Exception:
        return candidate


def _label_for_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or url
    path = parsed.path.rstrip('/')
    label = host + (path if path else '')
    label = label or url
    if len(label) > 80:
        return label[:77] + '...'
    return label


def _html_to_text(html: str) -> str:
    """Best-effort HTML → plain text conversion without extra deps."""
    cleaned = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', html)
    cleaned = re.sub(r'(?is)<head.*?>.*?</head>', ' ', cleaned)
    cleaned = re.sub(r'(?is)</?(br|p|div|li|tr|table|h[1-6]|section|article)[^>]*>', '\n', cleaned)
    cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
    text = unescape(cleaned)
    text = re.sub(r'\r', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def _read_url(url: str, limit: int = 200_000, timeout: float = 12.0) -> str:
    try:
        req = Request(url, headers={"User-Agent": "live-assistant-context/0.1"})
        with urlopen(req, timeout=timeout) as resp:
            content_type = (resp.headers.get('Content-Type') or '').lower()
            charset = resp.headers.get_content_charset() or 'utf-8'
            raw = resp.read(limit + 4096)
            if len(raw) > limit:
                raw = raw[:limit]
            text = raw.decode(charset, errors='ignore')
            if 'html' in content_type or ('text' in content_type and '<' in text and '>' in text):
                text = _html_to_text(text)
            elif 'text' not in content_type and content_type:
                return f"[Unsupported content type for {url}: {content_type}]"
            return text[:limit]
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', errors='ignore')
        except Exception:
            pass
        msg = body[:200] if body else str(e)
        return f"[HTTP error for {url}: {msg}]"
    except Exception as e:
        return f"[Could not fetch {url}: {e}]"


def _read_text_file(p: Path, limit: int = 200_000) -> str:
    try:
        txt = p.read_text(encoding='utf-8', errors='ignore')
        return txt[:limit]
    except Exception as e:
        return f"[Could not read text file {p.name}: {e}]"


def _read_pdf_file(p: Path, limit: int = 200_000) -> str:
    # Prefer external 'pdftotext' if available (no extra Python deps)
    try:
        import shutil
        if shutil.which('pdftotext'):
            # Use layout to preserve lines reasonably; output to stdout
            proc = subprocess.run(['pdftotext', '-layout', '-q', str(p), '-'], capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout[:limit]
            else:
                return f"[pdftotext failed for {p.name}: rc={proc.returncode}]\n{(proc.stderr or '')[:400]}"
    except Exception:
        pass
    # Fallback minimal message
    global _WARNED_NO_PDFTOTEXT
    if not _WARNED_NO_PDFTOTEXT:
        try:
            print("[!] 'pdftotext' not found; PDF context won't be extracted. Install 'poppler-utils' or convert PDFs to .txt/.md.")
            _log("pdftotext not found; PDF context will not be extracted")
        except Exception:
            pass
        _WARNED_NO_PDFTOTEXT = True
    return f"[PDF not extracted; install 'pdftotext' (poppler-utils) or convert {p.name} to .txt/.md]"


def collect_context(paths: List[str], max_total: int = 40_000) -> tuple[str, List[str]]:
    """Load text context from files/dirs. Returns (combined_text, labels)."""
    seen: set[str] = set()
    texts: List[str] = []
    labels: List[str] = []
    for raw in paths:
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.scheme in {'http', 'https'}:
            if raw in seen:
                continue
            seen.add(raw)
            txt = _read_url(raw)
            if txt:
                label = _label_for_url(raw)
                labels.append(label)
                texts.append(f"\n\n# {label}\n\n" + txt)
            continue
        try:
            p = Path(raw).expanduser().resolve()
        except Exception:
            continue
        if not p.exists():
            continue
        if p.is_dir():
            for f in sorted(p.rglob('*')):
                if f.is_file() and f.suffix.lower() in ACCEPTED_CONTEXT_EXTS:
                    ap = str(f)
                    if ap in seen:
                        continue
                    seen.add(ap)
                    txt = _read_pdf_file(f) if f.suffix.lower() == '.pdf' else _read_text_file(f)
                    if txt:
                        labels.append(f.name)
                        texts.append(f"\n\n# {f.name}\n\n" + txt)
        else:
            if p.suffix.lower() not in ACCEPTED_CONTEXT_EXTS:
                continue
            ap = str(p)
            if ap in seen:
                continue
            seen.add(ap)
            txt = _read_pdf_file(p) if p.suffix.lower() == '.pdf' else _read_text_file(p)
            if txt:
                labels.append(p.name)
                texts.append(f"\n\n# {p.name}\n\n" + txt)
    combined = "".join(texts)
    if len(combined) > max_total:
        combined = combined[:max_total]
    return combined.strip(), labels


def choose_from_list(prompt: str, items: List[Tuple[str, str]]) -> Optional[str]:
    if not items:
        print("[!] No items available.")
        return None
    print(prompt)
    for i, (_, name) in enumerate(items):
        print(f"  [{i}] {name}")
    while True:
        sel = input("Enter number (or blank to cancel): ").strip()
        if sel == "":
            dbg("Selection canceled")
            return None
        if sel.isdigit():
            i = int(sel)
            if 0 <= i < len(items):
                choice = items[i][1]
                dbg(f"Selected: {choice}")
                return choice
        print("Invalid selection; try again.")


def detect_vosk_model() -> Optional[str]:
    mp = os.environ.get("VOSK_MODEL_PATH")
    if mp and os.path.isdir(mp):
        return mp
    default = os.path.expanduser("~/.cache/vosk-model-small-en-us-0.15")
    if os.path.isdir(default):
        return default
    return None


def choose_vosk_model_path() -> Optional[str]:
    """Prompt user to pick a Vosk model path.
    Returns a directory path or None to proceed without live ASR.
    """
    candidates: List[str] = []
    for p in [
        os.environ.get("VOSK_MODEL_PATH"),
        os.path.expanduser("~/.cache/vosk-model-en-us-0.22"),
        os.path.expanduser("~/.cache/vosk-model-small-en-us-0.15"),
    ]:
        if p and os.path.isdir(p) and p not in candidates:
            candidates.append(p)
    print("Select Vosk ASR model (Enter to skip for recording-only):")
    for i, p in enumerate(candidates):
        print(f"  [{i}] {p}")
    try:
        sel = input("Enter number, a custom path, or blank: ").strip()
    except EOFError:
        sel = ""
    if sel == "":
        dbg("Vosk model selection skipped (recording only)")
        return None
    if sel.isdigit():
        idx = int(sel)
        if 0 <= idx < len(candidates):
            path = candidates[idx]
            dbg(f"Vosk model selected: {path}")
            return path
        return None
    if os.path.isdir(sel):
        dbg(f"Vosk model selected (custom): {sel}")
        return sel
    print(f"[!] Not a directory: {sel}. Proceeding without live ASR.")
    return None


def discover_prompt_files() -> list[tuple[str, str]]:
    """Return (display_name, absolute_path) for .md prompts.
    - Scans typical locations and recursively searches for directories containing 'prompt' (case-insensitive).
    - Honors PROMPT_DIR env if set.
    """
    here = Path(__file__).resolve().parent
    roots = [here, Path.cwd()]
    env_dir = os.environ.get('PROMPT_DIR')
    if env_dir:
        roots.append(Path(env_dir))
    candidates: set[Path] = set()
    # Direct candidates
    for r in roots:
        for name in ('prompt_library', 'prompt library', 'prompts', 'Prompt Library'):
            d = (r / name)
            if d.is_dir():
                candidates.add(d)
    # Recursive: any directory with 'prompt' in its name (depth <= 2)
    for r in roots:
        for d in r.rglob('*'):
            if d.is_dir() and 'prompt' in d.name.lower():
                # limit depth to avoid scanning huge trees
                try:
                    if len(d.relative_to(r).parts) <= 3:
                        candidates.add(d)
                except Exception:
                    pass
    seen=set()
    found: list[tuple[str,str]]=[]
    for d in sorted(candidates):
        try:
            for f in sorted(d.glob('*.md')):
                ap = str(f.resolve())
                if ap in seen:
                    continue
                seen.add(ap)
                found.append((f.name, ap))
        except Exception:
            pass
    return found



def choose_summary_prompt() -> str | None:
    # Env override takes precedence
    env_path = os.environ.get('SUMMARY_PROMPT')
    if env_path and Path(env_path).is_file():
        try:
            return Path(env_path).read_text(encoding='utf-8')
        except Exception:
            pass
    files = discover_prompt_files()
    print('Select a summary prompt (Enter to use default):')
    if files:
        for i, (name, _) in enumerate(files):
            print(f'  [{i}] {name}')
    else:
        print('  [no prompts found] (type a full path or press Enter)')
    try:
        sel = input('Enter number, a custom file path, or blank: ').strip()
    except EOFError:
        sel = ''
    if sel == '':
        return None
    path = None
    if sel.isdigit() and files:
        i = int(sel)
        if 0 <= i < len(files):
            path = files[i][1]
    else:
        if Path(sel).is_file():
            path = sel
    if not path:
        print('[!] Invalid selection; using default prompt.')
        return None
    try:
        return Path(path).read_text(encoding='utf-8')
    except Exception as e:
        print(f'[!] Failed to read prompt file: {e}')
        return None


def choose_summary_prompt_info() -> tuple[str|None,str|None]:
    # Env override takes precedence
    env_path = os.environ.get('SUMMARY_PROMPT')
    if env_path and Path(env_path).is_file():
        try:
            return Path(env_path).read_text(encoding='utf-8'), env_path
        except Exception:
            pass
    files = discover_prompt_files()
    print('Select a summary prompt (Enter to use default):')
    if files:
        for i,(name,_) in enumerate(files):
            print(f'  [{i}] {name}')
    else:
        print('  [no prompts found] (type a full path or press Enter)')
    try:
        sel = input('Enter number, a filename, a full path, or blank: ').strip()
    except EOFError:
        sel = ''
    if sel == '':
        return None, None
    # By number
    if sel.isdigit() and files:
        i=int(sel)
        if 0<=i<len(files):
            name, path = files[i]
            try:
                return Path(path).read_text(encoding='utf-8'), name
            except Exception:
                return None, None
    # By exact filename (case-insensitive)
    low = sel.lower()
    for name, path in files:
        if name.lower()==low:
            try:
                return Path(path).read_text(encoding='utf-8'), name
            except Exception:
                return None, None
    # By path
    if Path(sel).is_file():
        try:
            return Path(sel).read_text(encoding='utf-8'), Path(sel).name
        except Exception:
            return None, None
    print('[!] Invalid selection; using default prompt.')
    return None, None


DEFAULT_CHAT_PROMPT = (
    "You are a real-time meeting copilot.\n"
    "Answer the facilitator's questions using the latest transcript excerpt.\n"
    "If unsure, say you don't know. Cite speakers when possible."
)


def load_chat_prompt() -> tuple[str, str]:
    """Return (prompt_markdown, label) for the chatbot system prompt."""
    env_path = os.environ.get('CHAT_PROMPT')
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            try:
                return p.read_text(encoding='utf-8'), str(p)
            except Exception as e:
                print(f"[!] Failed to read CHAT_PROMPT at {p}: {e}")
    here = Path(__file__).resolve().parent
    candidates = [
        here / 'prompt_library' / 'chatbot.md',
        here / 'prompt_library' / 'chatbot' / 'system.md',
    ]
    for path in candidates:
        if path.is_file():
            try:
                return path.read_text(encoding='utf-8'), str(path)
            except Exception:
                pass
    return DEFAULT_CHAT_PROMPT, 'builtin.chatbot'


class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.transcript = []
        self.partial = ''
        self.analysis = ''
        self.actions = []
        self.questions = []
        self.decisions = []
        self.topics = []
        self._seen_actions = set()
        self._seen_questions = set()
        self._seen_decisions = set()
        self._seen_topics = set()

        # Interview mode capture and results
        self.segment_active = False
        self.segment_lines: List[str] = []
        self.segment_partial: str = ''
        self.qas: List[Tuple[str, str]] = []  # (question, answer)

        # Context bundle (mutable via runtime additions)
        self.context_text = ''
        self.context_labels: List[str] = []
        self._context_label_set: set[str] = set()
        self._context_entries: set[str] = set()

        # Chatbot exchanges (id, question, answer or None while pending)
        self._chat_history: List[Dict[str, object]] = []
        self._chat_seq = 0

    _STOPWORDS = set('a an and are as at be been being but by for from had has have how i if in into is it its of on or our over so than that the their them then there these they this to under up was we what when where which who will with you your'.split())

    @staticmethod
    def _normalize_key(s: str) -> str:
        import re as _re
        s = _re.sub(r"[^a-zA-Z0-9\s]", " ", s.lower()).strip()
        toks = [t for t in s.split() if t and t not in SharedState._STOPWORDS]
        return " ".join(toks[:10])

    def add_text(self, line: str):
        with self.lock:
            self.transcript.append(line)
            if self.segment_active:
                self.segment_lines.append(line)

    def set_partial(self, text: str):
        with self.lock:
            self.partial = text
            if self.segment_active:
                self.segment_partial = text

    def set_analysis(self, text: str):
        with self.lock:
            self.analysis = text

    def snapshot(self):
        with self.lock:
            return list(self.transcript), self.analysis, self.partial

    # Interview helpers
    def start_segment(self):
        with self.lock:
            self.segment_active = True
            self.segment_lines = []
            self.segment_partial = ''

    def stop_segment(self) -> str:
        with self.lock:
            self.segment_active = False
            text = "\n".join(self.segment_lines + ([self.segment_partial] if self.segment_partial else []))
            self.segment_lines = []
            self.segment_partial = ''
            return text.strip()

    def add_qa(self, question: str, answer: str):
        with self.lock:
            self.qas.append((question.strip(), answer.strip()))
            self.analysis = answer.strip()

    def get_qas(self) -> List[Tuple[str, str]]:
        with self.lock:
            return list(self.qas)

    # Context helpers
    def set_context_bundle(self, text: str, labels: List[str], entries: List[str]):
        with self.lock:
            self.context_text = text.strip()
            self.context_labels = []
            self._context_label_set = set()
            for label in labels:
                clean = (label or '').strip()
                if clean and clean not in self._context_label_set:
                    self._context_label_set.add(clean)
                    self.context_labels.append(clean)
            self._context_entries = {e for e in entries if e}

    def append_context_bundle(self, entry_id: str, text: str, labels: List[str]) -> bool:
        entry = (entry_id or '').strip()
        if not entry:
            return False
        added = False
        with self.lock:
            if entry in self._context_entries:
                return False
            chunk = (text or '').strip()
            if chunk:
                if self.context_text:
                    self.context_text += "\n\n"
                self.context_text += chunk
                added = True
            for label in labels:
                clean = (label or '').strip()
                if clean and clean not in self._context_label_set:
                    self._context_label_set.add(clean)
                    self.context_labels.append(clean)
                    added = True
            if added:
                self._context_entries.add(entry)
            return added

    def has_context_entry(self, entry_id: str) -> bool:
        with self.lock:
            return entry_id in self._context_entries

    def get_context(self) -> Tuple[str, List[str]]:
        with self.lock:
            return self.context_text, list(self.context_labels)

    # Chatbot helpers
    def add_chat_question(self, question: str) -> int:
        q = (question or "").strip()
        if not q:
            return -1
        with self.lock:
            cid = self._chat_seq
            self._chat_seq += 1
            self._chat_history.append({"id": cid, "question": q, "answer": None, "ts": time.time()})
            return cid

    def set_chat_answer(self, chat_id: int, answer: Optional[str]):
        with self.lock:
            for entry in reversed(self._chat_history):
                if entry.get("id") == chat_id:
                    entry["answer"] = (answer.strip() if answer else "")
                    if answer:
                        self.analysis = answer.strip()
                    break

    def get_chat_history(self) -> List[Tuple[str, Optional[str], bool]]:
        """Returns (question, answer, pending) tuples."""
        with self.lock:
            out: List[Tuple[str, Optional[str], bool]] = []
            for entry in self._chat_history:
                question = str(entry.get("question", ""))
                answer = entry.get("answer")
                pending = answer is None
                out.append((question, answer if not pending else None, pending))
            return out

    def has_pending_chat(self) -> bool:
        with self.lock:
            return any(entry.get("answer") is None for entry in self._chat_history)

    def _add_unique(self, lst, seen, items):
        for it in items:
            s = it.strip()
            if not s:
                continue
            key = SharedState._normalize_key(s)
            if not key or key in seen:
                continue
            seen.add(key)
            lst.append(s)

    def add_analysis_chunks(self, actions, questions, decisions, topics):
        with self.lock:
            self._add_unique(self.actions, self._seen_actions, actions)
            self._add_unique(self.questions, self._seen_questions, questions)
            self._add_unique(self.decisions, self._seen_decisions, decisions)
            self._add_unique(self.topics, self._seen_topics, topics)
            parts = []
            if self.actions:
                parts.append('Action Items:')
                parts.extend([f'- {a}' for a in self.actions])
                parts.append('')
            if self.questions:
                parts.append('Questions:')
                parts.extend([f'- {q}' for q in self.questions])
                parts.append('')
            if self.decisions:
                parts.append('Decisions:')
                parts.extend([f'- {d}' for d in self.decisions])
                parts.append('')
            if self.topics:
                parts.append('Key Topics:')
                parts.extend([f'- {t}' for t in self.topics])
                parts.append('')
            self.analysis = "\n".join(parts).strip()

    def get_lists(self):
        with self.lock:
            return (list(self.actions), list(self.questions), list(self.decisions), list(self.topics))
class LiveTranscriber:
    def __init__(self, source: str, session_dir: str, model_path: Optional[str], on_text=None, on_partial=None):
        self.source = source
        self.session_dir = session_dir
        self.model_path = model_path
        self.stop_event = threading.Event()
        self.transcript_lines: List[str] = []
        self.markers: List[Tuple[float, str]] = []
        self.notes: List[Tuple[float, str]] = []
        self.start_time = time.time()
        self.engine_label = "none"
        self.proc: Optional[subprocess.Popen] = None
        self.writer: Optional[wave.Wave_write] = None
        self.on_text = on_text
        self.on_partial = on_partial

        self.has_vosk = False
        self.recognizer = None
        if vosk and self.model_path and os.path.isdir(self.model_path):
            try:
                model = vosk.Model(self.model_path)
                self.recognizer = vosk.KaldiRecognizer(model, 16000)
                self.has_vosk = True
                self.engine_label = f"vosk:{os.path.basename(self.model_path)}"
            except Exception as e:
                print(f"[!] Failed to init Vosk: {e}")
                self.has_vosk = False
        dbg(f"LiveTranscriber init: source={self.source} has_vosk={self.has_vosk} engine={self.engine_label}")

    def _open_wave(self):
        wav_path = os.path.join(self.session_dir, "audio.wav")
        wf = wave.open(wav_path, 'wb')
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        self.writer = wf
        dbg(f"Opened WAV for writing: {wav_path}")

    def _spawn_ffmpeg(self):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "pulse", "-i", self.source,
            "-ac", "1", "-ar", "16000",
            "-f", "s16le", "-"
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        dbg(f"Spawned ffmpeg: {' '.join(cmd)} (pid={self.proc.pid if self.proc else 'n/a'})")

    def _sec(self) -> float:
        return time.time() - self.start_time

    def add_marker(self):
        t = self._sec()
        self.markers.append((t, f"marker at {t:0.1f}s"))

    def add_note(self, text: str):
        t = self._sec()
        self.notes.append((t, text))

    def run(self) -> threading.Thread:
        os.makedirs(self.session_dir, exist_ok=True)
        self._open_wave()
        self._spawn_ffmpeg()
        if not self.proc or not self.proc.stdout:
            print("[!] Failed to start ffmpeg.")
            _log("ffmpeg start failed: no proc/stdout")
            return threading.Thread(target=lambda: None)

        def reader():
            try:
                dbg("Reader thread started")
                while not self.stop_event.is_set():
                    chunk = self.proc.stdout.read(4000)
                    if not chunk:
                        break
                    if self.writer:
                        self.writer.writeframes(chunk)
                    if self.has_vosk and self.recognizer:
                        try:
                            if self.recognizer.AcceptWaveform(chunk):
                                res = json.loads(self.recognizer.Result())
                                txt = res.get("text", "").strip()
                                if txt:
                                    self.transcript_lines.append(txt)
                                    dbg(f"ASR final line len={len(txt)}")
                                    if self.on_text:
                                        try:
                                            self.on_text(txt)
                                        except Exception:
                                            pass
                                if self.on_partial:
                                    try:
                                        self.on_partial("")
                                    except Exception:
                                        pass
                            else:
                                try:
                                    pres = json.loads(self.recognizer.PartialResult())
                                    ptxt = pres.get("partial", "").strip()
                                except Exception:
                                    ptxt = ""
                                if self.on_partial is not None:
                                    try:
                                        self.on_partial(ptxt)
                                        if ptxt:
                                            dbg(f"ASR partial len={len(ptxt)}")
                                    except Exception:
                                        pass
                        except Exception as e:
                            print(f"[!] ASR error: {e}")
                            _log(f"ASR error: {e}")
            finally:
                try:
                    if self.writer:
                        self.writer.close()
                        dbg("Closed WAV writer")
                except Exception:
                    pass

        t_reader = threading.Thread(target=reader, daemon=True)
        t_reader.start()
        dbg("Reader thread launched")
        return t_reader

    def write_notes(self, source_label: str, sink_label: Optional[str], llm_model: Optional[str], analysis_text: Optional[str], lists: Optional[Tuple[List[str], List[str], List[str], List[str]]] = None, executive_summary: Optional[str] = None, prompt_label: Optional[str] = None, qas: Optional[List[Tuple[str, str]]] = None, chats: Optional[List[Tuple[str, str]]] = None, context_files: Optional[List[str]] = None):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        notes_path = os.path.join(self.session_dir, f"notes_{ts}.md")
        duration = time.time() - self.start_time
        with open(notes_path, "w", encoding="utf-8") as f:
            f.write(f"# Session Notes - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write("## Metadata\n")
            f.write(f"- Source: `{source_label}`\n")
            if sink_label:
                f.write(f"- Sink: `{sink_label}`\n")
            f.write(f"- Engine: `{self.engine_label}`\n")
            if llm_model:
                f.write(f"- LLM: `{llm_model}`\n")
            if prompt_label:
                f.write(f"- Prompt: `{prompt_label}`\n")
            if context_files:
                f.write("- Context Files:\n")
                for nm in context_files:
                    f.write(f"  - {nm}\n")
            f.write(f"- Duration: `{time.strftime('%H:%M:%S', time.gmtime(duration))}`\n")
            f.write(f"- Generated: `{datetime.now().isoformat(timespec='seconds')}`\n\n")
            if executive_summary:
                f.write("## Executive Summary\n")
                f.write(executive_summary.strip() + "\n\n")
            if analysis_text:
                f.write("## Live Analysis (final snapshot)\n")
                f.write(analysis_text)
                f.write("\n\n")
            if qas:
                f.write("## Interview Q&A\n")
                for i, (q, a) in enumerate(qas, start=1):
                    f.write(f"\n**Q{i}.** {q}\n\n")
                    f.write(f"{a}\n")
                f.write("\n")
            if chats:
                f.write("## Live Chatbot Exchanges\n")
                for i, (q, a) in enumerate(chats, start=1):
                    f.write(f"\n**You {i}.** {q}\n\n")
                    f.write(f"**Assistant.** {a}\n")
                f.write("\n")
            if lists:
                actions, questions, decisions, topics = lists
                f.write("## Action Items\n")
                if actions:
                    for a in actions:
                        f.write(f"- {a}\n")
                else:
                    f.write("- None captured.\n")
                f.write("\n## Questions\n")
                if questions:
                    for q in questions:
                        f.write(f"- {q}\n")
                else:
                    f.write("- None captured.\n")
                f.write("\n## Decisions\n")
                if decisions:
                    for d in decisions:
                        f.write(f"- {d}\n")
                else:
                    f.write("- None captured.\n")
                f.write("\n## Key Topics\n")
                if topics:
                    for t in topics:
                        f.write(f"- {t}\n")
                else:
                    f.write("- None captured.\n")
                f.write("\n")
            else:
                f.write("## Summary\n- Conversation captured.\n\n")
                f.write("## Key Topics\n—\n\n")
                f.write("## Action Items\n- None captured.\n\n")
                f.write("## Questions\n- None captured.\n\n")
                f.write("## Decisions\n- None captured.\n\n")
            if self.markers:
                f.write("## Markers\n")
                for t, label in self.markers:
                    f.write(f"- {t:0.1f}s: {label}\n")
                f.write("\n")
            if self.notes:
                f.write("## Notes\n")
                for t, text in self.notes:
                    f.write(f"- {t:0.1f}s: {text}\n")
                f.write("\n")
            f.write("## Full Transcript\n\n")
            for line in self.transcript_lines:
                f.write(line + "\n")
        print(f"[+] Notes saved: {notes_path}")


LOG_PATH: Optional[str] = None


def _log(msg: str):
    try:
        if LOG_PATH:
            from datetime import datetime as _dt
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(f"[{_dt.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def dbg(msg: str):
    """Verbose debug logger; active only when DEBUG is True."""
    if not DEBUG:
        return
    _log(f"DEBUG: {msg}")


def gpt_analyze(text: str, api_key: Optional[str], base_url: Optional[str], model: Optional[str], timeout: float = 12.0, context: Optional[str] = None, context_labels: Optional[List[str]] = None) -> Optional[str]:
    if not api_key or not model:
        return None
    try:
        import requests  # type: ignore
    except Exception:
        requests = None  # type: ignore
    url = (base_url.rstrip('/') if base_url else "https://api.openai.com/v1") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    prompt = (
        "You are a live meeting assistant. Analyze the provided transcript snippet and extract:\n"
        "- Action Items (owner if clear)\n- Questions\n- Decisions\n- Key Topics (keywords)\n"
        "Keep it concise and bulleted. If nothing, say 'None.'"
    )
    
    def _post(payload):
        try:
            if requests is not None:
                r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                return r.status_code, r.text, (r.json() if 'application/json' in r.headers.get('content-type','') else None)
            else:
                import urllib.request, urllib.error
                req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        body = resp.read().decode('utf-8')
                        try:
                            j = json.loads(body)
                        except Exception:
                            j = None
                        return 200, body, j
                except urllib.error.HTTPError as e:
                    body = e.read().decode('utf-8', errors='ignore')
                    return e.code, body, None
        except Exception as e:
            _log(f'GPT call failed: {e}')
            return 0, None, None

    base_messages = [
        {"role": "system", "content": prompt + ("\nUse the provided CONTEXT when relevant to improve precision." if context else "")},
    ]
    if context_labels:
        sources = "\n".join(f"- {lbl}" for lbl in context_labels[:8])
        base_messages.append({"role": "system", "content": "CONTEXT SOURCES:\n" + sources})
    if context:
        base_messages.append({"role": "system", "content": ("CONTEXT (may be partial):\n" + context[-8000:])})
    # Also include context once in the user channel to boost grounding on some providers
    if context:
        base_messages.append({"role": "user", "content": "Reference context (truncated):\n" + context[-6000:]})
    base_messages.append({"role": "user", "content": text[-6000:]})
    payload: Dict[str, object] = {"model": model, "messages": base_messages, "temperature": 0.2, "max_tokens": 300}

    def _maybe_retry(payload_dict: Dict[str, object], status_code: int, response_body: Optional[str], response_json: Optional[dict]) -> tuple[int, Optional[str], Optional[dict], Dict[str, object]]:
        nonlocal payload
        status_local, body_local, json_local = status_code, response_body, response_json
        if status_local == 400 and body_local and 'max_tokens' in body_local and 'max_completion_tokens' in body_local and 'max_completion_tokens' not in payload_dict:
            _log("Retrying with max_completion_tokens…")
            payload_dict = dict(payload_dict)
            payload_dict.pop('max_tokens', None)
            payload_dict['max_completion_tokens'] = 300
            status_local, body_local, json_local = _post(payload_dict)
            _log(f"GPT req status={status_local}")
        if status_local == 400 and body_local and '"param": "temperature"' in body_local and 'temperature' in payload_dict:
            _log("Retrying without temperature…")
            payload_dict = dict(payload_dict)
            payload_dict.pop('temperature', None)
            status_local, body_local, json_local = _post(payload_dict)
            _log(f"GPT req status={status_local}")
        return status_local, body_local, json_local, payload_dict

    status, body, j = _post(payload)
    _log(f"GPT req status={status}")
    status, body, j, payload = _maybe_retry(payload, status, body, j)
    if status != 200 or not j:
        if body:
            _log(f"GPT error body: {str(body)[:300]}")
        return None
    try:
        return j.get('choices', [{}])[0].get('message', {}).get('content')
    except Exception:
        return None

def gpt_with_prompt(prompt_md: str, user_input: str, api_key: Optional[str], base_url: Optional[str], model: Optional[str], timeout: float = 20.0, context: Optional[str] = None, context_labels: Optional[List[str]] = None) -> Optional[str]:
    if not api_key or not model:
        return None
    try:
        import requests  # type: ignore
    except Exception:
        requests = None  # type: ignore
    url = (base_url.rstrip('/') if base_url else "https://api.openai.com/v1") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages = [
        {"role": "system", "content": prompt_md + ("\nUse the provided CONTEXT to tailor answers." if context else "")},
    ]
    if context_labels:
        sources = "\n".join(f"- {lbl}" for lbl in context_labels[:8])
        messages.append({"role": "system", "content": "CONTEXT SOURCES:\n" + sources})
    if context:
        messages.append({"role": "system", "content": ("CONTEXT (may be partial):\n" + context[-10000:])})
        messages.append({"role": "user", "content": "Reference context (truncated):\n" + context[-8000:]})
    messages.append({"role": "user", "content": user_input})
    payload: Dict[str, object] = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 400}
    try:
        if requests is not None:
            attempt = 0
            while True:
                r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                if r.status_code == 200:
                    j = r.json()
                    return j.get('choices', [{}])[0].get('message', {}).get('content')
                if r.status_code == 400 and "max_tokens" in r.text and "max_completion_tokens" in r.text and 'max_completion_tokens' not in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens", 400)
                    attempt += 1
                    if attempt < 3:
                        continue
                if r.status_code == 400 and '"param": "temperature"' in r.text and 'temperature' in payload:
                    payload.pop("temperature", None)
                    attempt += 1
                    if attempt < 3:
                        continue
                return None
        else:
            import urllib.request, urllib.error
            def _do_request(data_dict: Dict[str, object]) -> tuple[int, str]:
                req = urllib.request.Request(url, data=json.dumps(data_dict).encode('utf-8'), headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        body = resp.read().decode('utf-8')
                        return 200, body
                except urllib.error.HTTPError as e:
                    body = e.read().decode('utf-8', errors='ignore')
                    return e.code, body

            attempt = 0
            while True:
                status, body = _do_request(payload)
                if status == 200:
                    try:
                        j = json.loads(body)
                    except Exception:
                        j = None
                    if j:
                        return j.get('choices', [{}])[0].get('message', {}).get('content')
                    return None
                if status == 400 and "max_tokens" in body and "max_completion_tokens" in body and 'max_completion_tokens' not in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens", 400)
                    attempt += 1
                    if attempt < 3:
                        continue
                if status == 400 and '"param": "temperature"' in body and 'temperature' in payload:
                    payload.pop("temperature", None)
                    attempt += 1
                    if attempt < 3:
                        continue
                return None
    except Exception:
        return None


def gpt_chat_response(prompt_md: str, question: str, transcript_lines: List[str], chat_history: List[Tuple[str, Optional[str], bool]], api_key: Optional[str], base_url: Optional[str], model: Optional[str], timeout: float = 25.0, context: Optional[str] = None, context_labels: Optional[List[str]] = None) -> Optional[str]:
    if not api_key or not model:
        return None
    try:
        import requests  # type: ignore
    except Exception:
        requests = None  # type: ignore

    url = (base_url.rstrip('/') if base_url else "https://api.openai.com/v1") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    system_prompt = prompt_md.strip() if prompt_md else "You are a real-time meeting copilot."
    system_prompt += "\nUse the latest transcript excerpts and context sources to keep answers grounded."

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if context_labels:
        sources = "\n".join(f"- {lbl}" for lbl in context_labels[:8])
        messages.append({"role": "system", "content": "CONTEXT SOURCES:\n" + sources})
    if context:
        messages.append({"role": "system", "content": "CONTEXT (truncated):\n" + context[-12000:]})

    if transcript_lines:
        snippet_lines = transcript_lines[-80:]
        if snippet_lines:
            snippet = "\n".join(snippet_lines)
            if len(snippet) > 6000:
                snippet = snippet[-6000:]
            messages.append({"role": "system", "content": "RECENT TRANSCRIPT:\n" + snippet})

    history_tail = chat_history[-6:]
    for q_prev, a_prev, pending in history_tail:
        if pending:
            continue
        if q_prev:
            messages.append({"role": "user", "content": q_prev})
        if a_prev:
            messages.append({"role": "assistant", "content": a_prev})

    messages.append({"role": "user", "content": question})
    payload: Dict[str, object] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 500,
    }

    try:
        if requests is not None:
            attempt = 0
            while True:
                r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                if r.status_code == 200:
                    j = r.json()
                    return j.get('choices', [{}])[0].get('message', {}).get('content')
                if r.status_code == 400 and "max_tokens" in r.text and "max_completion_tokens" in r.text and 'max_completion_tokens' not in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens", 500)
                    attempt += 1
                    if attempt < 3:
                        continue
                if r.status_code == 400 and '"param": "temperature"' in r.text and 'temperature' in payload:
                    _log("Chatbot retrying without temperature")
                    payload.pop("temperature", None)
                    attempt += 1
                    if attempt < 3:
                        continue
                _log(f"Chatbot request failed: status={r.status_code} body={r.text[:200]}")
                return None
        else:
            import urllib.request, urllib.error
            def _do_request(data_dict: Dict[str, object]) -> tuple[int, str]:
                req = urllib.request.Request(url, data=json.dumps(data_dict).encode('utf-8'), headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        body = resp.read().decode('utf-8')
                        return 200, body
                except urllib.error.HTTPError as e:
                    body = e.read().decode('utf-8', errors='ignore')
                    return e.code, body

            attempt = 0
            while True:
                status, body = _do_request(payload)
                if status == 200:
                    try:
                        j = json.loads(body)
                    except Exception:
                        j = None
                    if j:
                        return j.get('choices', [{}])[0].get('message', {}).get('content')
                    return None
                if status == 400 and "max_tokens" in body and "max_completion_tokens" in body and 'max_completion_tokens' not in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens", 500)
                    attempt += 1
                    if attempt < 3:
                        continue
                if status == 400 and '"param": "temperature"' in body and 'temperature' in payload:
                    payload.pop("temperature", None)
                    attempt += 1
                    if attempt < 3:
                        continue
                _log(f"Chatbot HTTPError body: {body[:200]}")
                return None
    except Exception as e:
        _log(f"Chatbot call exception: {e}")
        return None

def analyzer_loop(state: SharedState, api_key: Optional[str], base_url: Optional[str], model: Optional[str], stop_event: threading.Event, prompt_md: Optional[str] = None):
    def parse_blocks(s: str) -> Tuple[List[str], List[str], List[str], List[str]]:
        actions: List[str] = []
        questions: List[str] = []
        decisions: List[str] = []
        topics: List[str] = []
        current = None
        headers = {
            'action items': 'actions', 'actions': 'actions',
            'questions': 'questions', 'question': 'questions',
            'decisions': 'decisions', 'decision': 'decisions',
            'key topics': 'topics', 'topics': 'topics', 'keywords': 'topics',
        }
        for raw in s.splitlines():
            line = raw.strip()
            if not line:
                continue
            low = line.lower().rstrip(':')
            if low in headers:
                current = headers[low]
                continue
            text = line[2:].strip() if line.startswith(('- ', '* ')) else line
            if current == 'actions':
                actions.append(text)
            elif current == 'questions':
                questions.append(text)
            elif current == 'decisions':
                decisions.append(text)
            elif current == 'topics':
                topics.extend([t.strip() for t in text.split(',') if t.strip()]) if ',' in text else topics.append(text)
        return actions, questions, decisions, topics

    def fallback(snippet: str) -> Tuple[List[str], List[str], List[str], List[str]]:
        import re
        actions: List[str] = []
        questions: List[str] = []
        decisions: List[str] = []
        freq = {}
        for l in [x.strip() for x in snippet.splitlines() if x.strip()]:
            low = l.lower()
            if '?' in l or low.startswith((
                'who ', 'what ', 'why ', 'how ', 'when ', 'where ',
                'do ', 'does ', 'did ', 'is ', 'are ', 'have ', 'has '
            )):
                questions.append(l)
            if any(k in low for k in (
                'we decided', 'agreed', 'decision', 'we will', "we'll", 'we chose', 'proceed'
            )):
                decisions.append(l)
            if any(k in low for k in (
                'we need to', 'we should', 'todo', 'follow up', 'please ', 'can you', 'assign', 'schedule', 'send ', 'prepare '
            )):
                actions.append(l)
            for tok in [t.lower() for t in re.findall(r'[A-Za-z]+', l) if len(t) >= 4]:
                if tok in SharedState._STOPWORDS:
                    continue
                freq[tok] = freq.get(tok, 0) + 1
        topics = [t for t, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]
        return actions[:5], questions[:5], decisions[:5], topics

    while not stop_event.is_set():
        transcript, _, partial = state.snapshot()
        snippet = ("\n".join(transcript[-60:]) + ("\n" + partial if partial else "")).strip()
        if snippet:
            dbg(f"Analyzer tick: snippet_len={len(snippet)} use_prompt={bool(prompt_md)}")
            analysis = None
            ctx_text, ctx_labels = state.get_context()
            ctx_text = ctx_text or None
            ctx_labels = ctx_labels or None
            if prompt_md:
                analysis = gpt_with_prompt(prompt_md, snippet, api_key, base_url, model, context=ctx_text, context_labels=ctx_labels)
            if analysis is None:
                analysis = gpt_analyze(snippet, api_key, base_url, model, context=ctx_text, context_labels=ctx_labels)
            if analysis:
                a, q, d, t = parse_blocks(analysis)
                state.add_analysis_chunks(a, q, d, t)
                state.set_analysis(analysis)
                dbg(f"Analyzer updated: a={len(a)} q={len(q)} d={len(d)} t={len(t)}")
            else:
                a, q, d, t = fallback(snippet)
                state.add_analysis_chunks(a, q, d, t)
                dbg(f"Analyzer fallback used: a={len(a)} q={len(q)} d={len(d)} t={len(t)}")
        stop_event.wait(5.0)


def run_curses_ui(source: str, sink: Optional[str], vosk_label: str, llm_model: Optional[str], state: SharedState, tr: LiveTranscriber, reader_thread: threading.Thread, *, interview_mode: bool = False, interview_prompt: Optional[str] = None, api_key: Optional[str] = None, base_url: Optional[str] = None, chat_prompt: Optional[str] = None, chat_prompt_label: Optional[str] = None):
    def init_colors():
        if not curses.has_colors():
            return {}
        curses.start_color()
        try:
            curses.use_default_colors()
        except Exception:
            pass
        # Gruvbox-dark inspired (approximation with basic curses palette)
        pairs = {
            'default': 1,   # subtle body text
            'title': 2,     # high-contrast title bar
            'sep': 3,       # vertical divider
            'left': 4,      # transcript
            'right': 5,     # analysis
            'partial': 6,   # dimmed partial
            'footer': 7,    # high-contrast footer
        }
        curses.init_pair(pairs['default'], curses.COLOR_WHITE, -1)
        # Title/Footer as black on yellow for strong contrast
        curses.init_pair(pairs['title'], curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(pairs['footer'], curses.COLOR_BLACK, curses.COLOR_YELLOW)
        # Separator accent
        curses.init_pair(pairs['sep'], curses.COLOR_YELLOW, -1)
        # Bodies
        curses.init_pair(pairs['left'], curses.COLOR_WHITE, -1)
        curses.init_pair(pairs['right'], curses.COLOR_CYAN, -1)
        # Partial: dimmed yellow text on default
        curses.init_pair(pairs['partial'], curses.COLOR_YELLOW, -1)
        return pairs

    QUESTION_PHRASES = [
        'what are', 'what is', 'what do', 'what did', 'what can', 'what should', 'what would',
        'how do', 'how did', 'how can', 'how should', 'how would', 'how are', 'how is',
        'why is', 'why are', 'why do', 'why did',
        'when is', 'when are', 'when will',
        'where is', 'where are',
        'who is', 'who are', 'who will',
        'can we', 'can you', 'can i',
        'could we', 'could you',
        'should we', 'should you',
        'would we', 'would you',
        'did we', 'did you', 'do we', 'do you',
        'have we', 'have you', 'has anyone',
        'is that', 'is there', 'are we', 'are there',
        'will we', 'will you', 'will it',
    ]

    def looks_like_question(raw: str) -> bool:
        text = (raw or '').strip()
        if not text:
            return False
        if '?' in text:
            return True
        low = ' '.join(text.lower().split())
        for prefix in (
            'who', 'what', 'when', 'where', 'why', 'how',
            'do', 'does', 'did', 'is', 'are', 'can', 'could',
            'should', 'would', 'have', 'has', 'will'
        ):
            if low.startswith(prefix + ' '):
                return True
        padded = f' {low} '
        for phrase in QUESTION_PHRASES:
            if f' {phrase} ' in padded:
                return True
        return False

    def wrap_bulleted(lines: List[str], width: int, bullet: str = "- ") -> List[Tuple[str, bool]]:
        """Wrap lines as bulleted blocks with a blank spacer between blocks.

        Returns (line_text, is_question) tuples so the caller can style questions.
        """
        out: List[Tuple[str, bool]] = []
        indent = " " * len(bullet)
        for raw in lines:
            raw = (raw or "").strip()
            if raw == "":
                out.append(("", False))
                continue
            highlight = looks_like_question(raw)
            first = True
            wrap_width = max(1, width - len(bullet))
            wrapped = textwrap.wrap(raw, wrap_width) or [""]
            for seg in wrapped:
                if first:
                    out.append((bullet + seg, highlight))
                    first = False
                else:
                    out.append((indent + seg, highlight))
            out.append(("", False))
        return out

    def _ui(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)

        base_title = f"Src: {source}  Sink: {sink or '-'}  ASR: {vosk_label}  LLM: {llm_model or '-'}"
        pairs = init_colors()

        msg_queue: queue.Queue[Tuple[str, float, bool]] = queue.Queue()
        status_message = ""
        status_expire = 0.0
        status_requires_ack = False

        def push_status(msg: str, duration: float = 4.0, sticky: bool = False):
            msg_queue.put((msg, duration, sticky))

        note_mode = False
        note_buffer = ""
        left_offset = 0
        left_follow = False
        active_search = ""
        search_idx = -1  # index of last matched bullet line
        filter_query = ""
        capturing = False
        answering = False
        active_pane = 'left'
        right_offset = 0
        right_follow = True

        def prompt_line(prompt_text: str) -> str:
            h, w = stdscr.getmaxyx()
            curses.curs_set(1)
            curses.echo()
            stdscr.nodelay(False); stdscr.timeout(-1)
            stdscr.addnstr(h-1, 0, prompt_text[:w], w, curses.A_REVERSE)
            stdscr.clrtoeol(); stdscr.refresh()
            try:
                s = stdscr.getstr(h-1, len(prompt_text), max(1, w - len(prompt_text) - 1))
                return (s.decode(errors='ignore').strip() if s else "")
            except Exception:
                return ""
            finally:
                curses.noecho(); curses.curs_set(0)
                stdscr.nodelay(True); stdscr.timeout(200)

        dbg("TUI started")
        while not tr.stop_event.is_set():
            h, w = stdscr.getmaxyx()
            try:
                while True:
                    msg, duration, sticky = msg_queue.get_nowait()
                    status_message = msg
                    status_requires_ack = sticky
                    status_expire = float('inf') if sticky else time.time() + max(0.5, duration)
            except queue.Empty:
                pass
            if status_message and not status_requires_ack and time.time() > status_expire:
                status_message = ""

            context_text_current, context_label_list = state.get_context()
            title_h = 1
            footer_h = 1
            body_h = max(1, h - title_h - footer_h)
            # Keep both panes usable on small terminals
            min_pane = 10
            left_w = int(w * 0.58)
            left_w = max(min_pane, min(w - min_pane, left_w)) if w >= (min_pane * 2) else max(1, w - 1)
            right_w = max(1, w - left_w)

            stdscr.erase()
            focus_label = 'Transcript' if active_pane == 'left' else 'Analysis'
            title = f"{base_title}  Focus:{focus_label}"
            if pairs:
                stdscr.addnstr(0, 0, title[:w], w, curses.color_pair(pairs['title']) | curses.A_BOLD)
            else:
                stdscr.addnstr(0, 0, title[:w], w, curses.A_REVERSE)

            transcript, analysis, partial = state.snapshot()
            chat_history = state.get_chat_history()
            chat_available = bool(chat_prompt and api_key and llm_model)
            chat_pending = state.has_pending_chat() if chat_available else False

            # Left pane layout:
            # - Top: live partial stream (word-wrapped) with dim style
            # - Spacer
            # - Below: finalized transcript as bulleted, word-wrapped blocks

            def wrap_with_prefix(text: str, width: int, prefix: str = "… ") -> List[str]:
                if not text:
                    return []
                width = max(1, width)
                body_w = max(1, width - len(prefix))
                parts = textwrap.wrap(text, body_w) or [""]
                out = []
                for i, seg in enumerate(parts):
                    if i == 0:
                        out.append(prefix + seg)
                    else:
                        out.append(" " * len(prefix) + seg)
                return out

            y = 1
            # 1) Partial (live stream) at the top
            partial_lines: List[str] = wrap_with_prefix(partial or "", left_w - 2) if partial else []
            for pl in partial_lines:
                if y >= 1 + body_h:
                    break
                attr = (curses.color_pair(pairs['partial']) | curses.A_DIM) if pairs else curses.A_DIM
                stdscr.addnstr(y, 0, pl[: left_w - 2], left_w - 2, attr)
                y += 1

            # Spacer line between partial and bullets
            if partial_lines and y < 1 + body_h:
                y += 1

            # 2) Bulleted finalized transcript below
            available_rows = max(0, (1 + body_h) - y)
            bullet_lines = wrap_bulleted(transcript, left_w - 2, bullet="• ")
            max_offset = max(0, len(bullet_lines) - available_rows)
            if left_follow:
                left_offset = max_offset
            else:
                left_offset = max(0, min(left_offset, max_offset))
            view_lines = bullet_lines[left_offset:left_offset + available_rows]
            for seg, is_question in view_lines:
                if y >= 1 + body_h:
                    break
                attr = curses.color_pair(pairs['left']) if pairs else 0
                if is_question:
                    attr |= curses.A_BOLD | curses.A_UNDERLINE
                if active_search and active_search.lower() in seg.lower():
                    attr |= curses.A_REVERSE
                stdscr.addnstr(y, 0, seg[: left_w - 2], left_w - 2, attr)
                y += 1

            # Vertical separator (with color if available)
            try:
                if pairs:
                    stdscr.attron(curses.color_pair(pairs['sep']))
                stdscr.vline(1, left_w - 1, curses.ACS_VLINE, body_h)
                if pairs:
                    stdscr.attroff(curses.color_pair(pairs['sep']))
            except Exception:
                pass

            # Right pane: analysis
            right_x = left_w
            right_lines: List[Tuple[str, int]] = []

            def color(name: str, extra: int = 0) -> int:
                return (curses.color_pair(pairs[name]) | extra) if pairs else extra

            def add_wrapped_right(text: str, attr: int):
                for seg in textwrap.wrap(text, right_w - 1) or [""]:
                    right_lines.append((seg, attr))

            def add_right_blank():
                right_lines.append(("", color('right')))

            header_parts: List[str] = []
            if not tr.has_vosk:
                header_parts.append("ASR disabled (recording only)")
            if interview_mode:
                status = "capturing" if capturing else ("answering" if answering else "idle")
                header_parts.append(f"Interview: {status}")
            if context_text_current or context_label_list:
                header_parts.append(f"CTX: {len(context_label_list)}" if context_label_list else "CTX:on")
            if chat_available:
                status = 'pending' if chat_pending else ('ready' if chat_history else 'idle')
                header_parts.append(f"Chat: {status}")
            if header_parts:
                add_wrapped_right(" · ".join(header_parts), color('right', curses.A_BOLD))
                add_right_blank()

            if status_message:
                add_wrapped_right(status_message, color('right', curses.A_REVERSE))
                add_right_blank()

            initial_text = analysis or "Waiting for analysis..."
            for line in initial_text.splitlines():
                add_wrapped_right(line, color('right'))

            if chat_history:
                add_right_blank()
                header = "Chatbot"
                if chat_prompt_label and chat_prompt_label != 'builtin.chatbot':
                    label = chat_prompt_label
                    if os.path.sep in label:
                        label = os.path.basename(label)
                    header = f"Chatbot [{label}]"
                add_wrapped_right(header, color('right', curses.A_BOLD))
                for q, a, pending in chat_history[-12:]:
                    add_wrapped_right(f"You> {q}" if q else "You> (blank question)", color('right'))
                    ans_text = "…" if pending else (a or "(No answer)")
                    attr = color('right', curses.A_DIM) if pending else color('right')
                    add_wrapped_right(f"Bot> {ans_text}", attr)
                    add_right_blank()

            available_right_rows = body_h
            max_offset_right = max(0, len(right_lines) - available_right_rows)
            if right_follow:
                right_offset = max_offset_right
            else:
                right_offset = max(0, min(right_offset, max_offset_right))

            ry = 1
            for idx in range(right_offset, min(len(right_lines), right_offset + available_right_rows)):
                text, attr = right_lines[idx]
                stdscr.addnstr(ry, right_x, text[: right_w - 1], right_w - 1, attr)
                ry += 1
            while ry < 1 + body_h:
                stdscr.move(ry, right_x)
                stdscr.clrtoeol()
                ry += 1

            # Footer with elapsed time and key hints
            elapsed = time.strftime('%H:%M:%S', time.gmtime(time.time() - tr.start_time))
            footer = f"Focus:{focus_label}  q Quit  m Mark  n Note  c Chat  C Context  Tab focus  Esc back  / search (n/N next/prev)  \\ filter  j/k scroll   t={elapsed}"
            if note_mode:
                ft = f"note> {note_buffer}"[: w]
                if pairs:
                    stdscr.addnstr(h - 1, 0, ft, w, curses.color_pair(pairs['footer']))
                else:
                    stdscr.addnstr(h - 1, 0, ft, w, curses.A_REVERSE)
            else:
                if pairs:
                    stdscr.addnstr(h - 1, 0, footer[:w], w, curses.color_pair(pairs['footer']))
                else:
                    stdscr.addnstr(h - 1, 0, footer[:w], w, curses.A_REVERSE)

            stdscr.refresh()

            try:
                ch = stdscr.getch()
            except Exception:
                ch = -1

            if ch == -1:
                continue

            if note_mode:
                if ch in (10, 13):  # Enter
                    if note_buffer.strip():
                        tr.add_note(note_buffer.strip())
                    note_mode = False
                    note_buffer = ""
                elif ch in (27,):  # ESC
                    note_mode = False
                    note_buffer = ""
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    note_buffer = note_buffer[:-1]
                elif 32 <= ch <= 126:
                    note_buffer += chr(ch)
                continue

            if ch in (ord('q'), ord('Q')):
                tr.stop_event.set()
                dbg("Key 'q' pressed; exiting UI")
                break
            elif ch in (ord('m'), ord('M')):
                tr.add_marker()
                dbg("Marker added")
            elif ch in (ord('n'), ord('N')):
                if active_search:
                    # With an active search, 'n'/'N' navigate results
                    ql = active_search.lower()
                    total = len(bullet_lines)
                    if total > 0:
                        if ch == ord('n'):
                            start = (search_idx + 1) % total
                            idx = None
                            for i in range(total):
                                j = (start + i) % total
                                if ql in bullet_lines[j][0].lower():
                                    idx = j; break
                            if idx is not None:
                                search_idx = idx
                                left_follow = False
                                max_offset = max(0, len(bullet_lines) - available_rows)
                                left_offset = max(0, min(idx, max_offset))
                                dbg(f"Search next -> {idx}")
                        else:  # 'N'
                            start = (search_idx - 1) % total if search_idx != -1 else (total - 1)
                            idx = None
                            for i in range(total):
                                j = (start - i) % total
                                if ql in bullet_lines[j][0].lower():
                                    idx = j; break
                            if idx is not None:
                                search_idx = idx
                                left_follow = False
                                max_offset = max(0, len(bullet_lines) - available_rows)
                                left_offset = max(0, min(idx, max_offset))
                                dbg(f"Search prev -> {idx}")
                else:
                    note_mode = True
                    note_buffer = ""
                    dbg("Note mode entered")
            elif ch == ord('c'):
                user_msg = prompt_line('chat> ')
                if not user_msg:
                    continue
                history_before = state.get_chat_history()
                chat_id = state.add_chat_question(user_msg)
                if chat_id == -1:
                    continue
                if not chat_available:
                    state.set_chat_answer(chat_id, "Chatbot disabled. Set OPENAI_API_KEY and --llm-model.")
                    push_status('Chatbot disabled; set OPENAI_API_KEY and --llm-model.', sticky=True)
                    continue
                transcript_snapshot = list(transcript)
                ctx_text_snapshot, ctx_labels_snapshot = state.get_context()

                def _chat_worker():
                    ans = gpt_chat_response(
                        chat_prompt or DEFAULT_CHAT_PROMPT,
                        user_msg,
                        transcript_snapshot,
                        history_before,
                        api_key,
                        base_url,
                        llm_model,
                        context=(ctx_text_snapshot or None),
                        context_labels=(ctx_labels_snapshot if ctx_labels_snapshot else None)
                    )
                    if ans:
                        state.set_chat_answer(chat_id, ans)
                        push_status('Chat answer ready (press Esc).', sticky=True)
                    else:
                        state.set_chat_answer(chat_id, "(No answer returned)")
                        push_status('No chat answer returned (press Esc).', sticky=True)

                threading.Thread(target=_chat_worker, daemon=True).start()
            elif ch == ord('C'):
                raw_entry = prompt_line('context path/url> ')
                if not raw_entry:
                    push_status('Context entry cancelled.', 3.0)
                    continue
                raw_entry = raw_entry.strip()
                entry_id = canonical_context_id(raw_entry)
                if not entry_id:
                    push_status('Invalid context path or URL.', 4.0, sticky=True)
                    continue
                if state.has_context_entry(entry_id):
                    push_status('Context already loaded.', 4.0)
                    continue

                def _context_worker(source: str, entry_canonical: str):
                    try:
                        text_chunk, labels_chunk = collect_context([source])
                    except Exception as e:
                        push_status(f'Context load failed: {e}', 6.0, sticky=True)
                        return
                    if not (text_chunk and text_chunk.strip()):
                        push_status('No text extracted; check the source.', 5.0, sticky=True)
                        return
                    added = state.append_context_bundle(entry_canonical, text_chunk, labels_chunk)
                    if added:
                        label = labels_chunk[0] if labels_chunk else entry_canonical
                        push_status(f'Context added: {label}', 4.0, sticky=True)
                    else:
                        push_status('Context already loaded.', 4.0)

                push_status('Loading context…', 2.0)
                threading.Thread(target=_context_worker, args=(raw_entry, entry_id), daemon=True).start()
            elif interview_mode and ch in (ord('i'), ord('I')):
                if not capturing:
                    state.start_segment()
                    capturing = True
                    dbg("Interview capture started")
                else:
                    question = state.stop_segment()
                    capturing = False
                    dbg(f"Interview capture stopped; q_len={len(question) if question else 0}")
                    if question:
                        answering = True
                        ctx_text_snapshot, ctx_labels_snapshot = state.get_context()
                        def _answer():
                            nonlocal answering
                            try:
                                ans = gpt_with_prompt(
                                    interview_prompt or "You are an interview assistant.",
                                    question,
                                    api_key,
                                    base_url,
                                    llm_model,
                                    timeout=30.0,
                                    context=(ctx_text_snapshot or None),
                                    context_labels=(ctx_labels_snapshot if ctx_labels_snapshot else None)
                                )
                                if ans:
                                    state.add_qa(question, ans)
                                    push_status('Interview answer ready (press Esc).', sticky=True)
                                else:
                                    state.add_qa(question, "(No answer returned)")
                                    push_status('Interview answer missing (press Esc).', sticky=True)
                            finally:
                                answering = False
                        threading.Thread(target=_answer, daemon=True).start()
            elif ch == ord('j'):
                if active_pane == 'left':
                    left_follow = False
                    left_offset = min(max(0, len(bullet_lines) - available_rows), left_offset + 1)
                    if left_offset >= max(0, len(bullet_lines) - available_rows):
                        left_follow = True
                else:
                    right_follow = False
                    right_offset = min(max_offset_right, right_offset + 1)
            elif ch == ord('k'):
                if active_pane == 'left':
                    left_follow = False
                    left_offset = max(0, left_offset - 1)
                else:
                    right_follow = False
                    right_offset = max(0, right_offset - 1)
            elif ch == ord('/'):
                q = prompt_line('search> ')
                if not q:
                    active_search = ""
                    search_idx = -1
                else:
                    active_search = q
                    ql = q.lower()
                    # find first occurrence
                    idx = next((i for i, item in enumerate(bullet_lines) if ql in item[0].lower()), None)
                    if idx is not None:
                        search_idx = idx
                        left_follow = False
                        left_offset = max(0, min(idx, max(0, len(bullet_lines) - available_rows)))
                        dbg(f"Search set '{active_search}', first at {idx}")
                    else:
                        search_idx = -1
                        dbg(f"Search set '{active_search}', no matches")
            
            elif ch == ord('\\'):
                q = prompt_line('filter> ')
                filter_query = q
                left_follow = True
            elif ch in (9, curses.KEY_BTAB):
                if active_pane == 'left':
                    active_pane = 'right'
                    right_follow = True
                    push_status('Analysis pane focused (press Esc to return).', 3.0)
                else:
                    active_pane = 'left'
                    left_follow = True
                    push_status('Transcript pane focused.', 3.0)
            elif ch == 27:  # ESC
                if status_requires_ack:
                    status_message = ""
                    status_requires_ack = False
                    status_expire = 0.0
                elif active_pane == 'right':
                    active_pane = 'left'
                    left_follow = True
                    status_message = ""
                    status_expire = 0.0
                else:
                    status_message = ""
                    status_expire = 0.0

        tr.stop_event.set()

    try:
        curses.wrapper(_ui)
    finally:
        if tr.proc and tr.proc.poll() is None:
            try:
                tr.proc.terminate()
            except Exception:
                pass
        reader_thread.join(timeout=2)


def main():
    print("=== Live Assistant (TUI) ===")

    parser = argparse.ArgumentParser(description="Live Assistant TUI")
    parser.add_argument("--source", dest="source", help="PulseAudio source name (capture)")
    parser.add_argument("--sink", dest="sink", help="PulseAudio sink name (playback)")
    parser.add_argument("--vosk-model-path", dest="vosk_model_path", help="Path to Vosk model directory")
    parser.add_argument("--llm-model", dest="llm_model", help="LLM model name (e.g., gpt-4o-mini)")
    parser.add_argument("--openai-base-url", dest="openai_base_url", help="OpenAI-compatible base URL")
    parser.add_argument("-C", "--context", dest="context", action="append", help="File, directory, or http(s) URL to use as context (.pdf, .md, .txt, webpages). Can repeat.")
    parser.add_argument("--debug", dest="debug", action="store_true", help="Enable verbose logging to assistant.log")
    args, unknown = parser.parse_known_args()

    interactive = sys.stdin.isatty()

    # Set global debug toggle (log file will be created once session_dir is known)
    global DEBUG
    DEBUG = bool(args.debug or os.environ.get('DEBUG'))

    # Prepare context (from CLI or env var CONTEXT_PATHS=path1:path2,...)
    context_paths: List[str] = []
    if args.context:
        context_paths.extend(args.context)
    env_ctx = os.environ.get('CONTEXT_PATHS') or os.environ.get('CONTEXT')
    if env_ctx:
        context_paths.extend([p for p in env_ctx.split(':') if p])

    context_entry_ids: List[str] = []
    for raw in context_paths:
        cid = canonical_context_id(raw)
        if cid and cid not in context_entry_ids:
            context_entry_ids.append(cid)

    sources = list_pulse_devices("sources")
    sinks = list_pulse_devices("sinks")
    if not sources:
        print("[!] No PulseAudio sources found. Is Pulse running?\n    Try: pactl list short sources")
        sys.exit(1)

    src = args.source
    if not src:
        src = choose_from_list("Select capture source (e.g., a 'monitor' for system audio):", sources) if interactive else None
    if not src:
        print("Canceled.")
        return
    else:
        dbg(f"Using source: {src}")
    sink = args.sink
    if not sink and sinks and interactive:
        sink = choose_from_list("Select playback sink (optional):", sinks)
        if sink:
            dbg(f"Using sink: {sink}")

    model_path = args.vosk_model_path if args.vosk_model_path else (choose_vosk_model_path() if vosk else None)
    if vosk and model_path:
        print(f"[=] Using Vosk model at: {model_path}")
        dbg(f"Vosk model path: {model_path}")
    else:
        if not vosk:
            print("[!] 'vosk' not installed. Live transcription disabled; recording only.")
            dbg("vosk not installed; recording-only mode")
        else:
            print("[=] Proceeding without Vosk ASR (recording only).")
            dbg("No Vosk model selected; recording-only mode")

    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.expanduser(os.path.join("~", "recordings", f"session_{session_ts}"))
    os.makedirs(session_dir, exist_ok=True)

    llm_model = args.llm_model or os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = args.openai_base_url or os.environ.get("OPENAI_BASE_URL")
    dbg(f"LLM config: model={llm_model} api_key_set={bool(api_key)} base_url={'set' if base_url else 'default'}")
    if not args.llm_model and interactive:
        try:
            entered_model = input(f"Enter GPT model to use [{llm_model}]: ").strip()
        except EOFError:
            entered_model = ""
        if entered_model:
            llm_model = entered_model

    # Optional: choose a summary prompt from prompt library
    summary_prompt_label = None
    try:
        summary_prompt, summary_prompt_label = choose_summary_prompt_info()
    except Exception:
        summary_prompt = None
        summary_prompt_label = None

    chat_prompt, chat_prompt_label = load_chat_prompt()

    if not api_key and interactive:
        try:
            entered = input("Enter OPENAI_API_KEY (blank to skip GPT): ").strip()
        except EOFError:
            entered = ""
        if entered:
            api_key = entered
            os.environ["OPENAI_API_KEY"] = entered

    global LOG_PATH
    LOG_PATH = os.path.join(session_dir, "assistant.log")
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("Live Assistant log\n")
    except Exception:
        LOG_PATH = None
    if DEBUG:
        _log("Debug logging enabled")
        dbg(f"session_dir={session_dir}")
        dbg(f"interactive={interactive}")
        dbg(f"argv={sys.argv}")
        dbg(f"env OPENAI_BASE_URL={os.environ.get('OPENAI_BASE_URL','')} LLM_MODEL={os.environ.get('LLM_MODEL','')} OPENAI_MODEL={os.environ.get('OPENAI_MODEL','')}")

    # Load context text (do this after LOG_PATH setup for debug logging)
    context_text = ""
    context_labels: List[str] = []
    if context_paths:
        try:
            context_text, context_labels = collect_context(context_paths)
            if context_text:
                print(f"[=] Loaded context: {len(context_text)} chars from {len(context_labels)} source(s)")
                dbg(f"Loaded context chars={len(context_text)} from sources={len(context_labels)}")
            else:
                print("[!] No valid context text loaded from provided -C/CONTEXT_PATHS. Ensure files exist, URLs are reachable, and use .pdf/.md/.txt or webpage sources. For PDFs, install 'pdftotext' (poppler-utils).")
        except Exception as e:
            _log(f"Context load failed: {e}")

    state = SharedState()
    initial_entries = context_entry_ids if context_labels else []
    state.set_context_bundle(context_text or '', context_labels or [], initial_entries)
    tr = LiveTranscriber(src, session_dir, model_path, on_text=state.add_text, on_partial=state.set_partial)
    reader_thread = tr.run()

    # Determine interview mode
    interview_mode = bool(summary_prompt_label and 'interview' in summary_prompt_label.lower())

    analyzer_stop = tr.stop_event
    if not interview_mode:
        if api_key and llm_model:
            t_an = threading.Thread(target=analyzer_loop, args=(state, api_key, base_url, llm_model, analyzer_stop, (summary_prompt or None)), daemon=True)
            t_an.start()
        else:
            state.set_analysis("Set OPENAI_API_KEY to enable live analysis.")
    else:
        state.set_analysis("Interview mode: press 'i' to capture a question; press 'i' again to stop and generate an answer.")

    vosk_label = tr.engine_label
    run_curses_ui(src, sink, vosk_label, llm_model if api_key else None, state, tr, reader_thread, interview_mode=interview_mode, interview_prompt=summary_prompt or '', api_key=api_key, base_url=base_url, chat_prompt=chat_prompt, chat_prompt_label=chat_prompt_label)

    _, final_analysis, _ = state.snapshot()
    ctx_text_final, ctx_labels_final = state.get_context()
    # Full-transcript pass for report
    try:
        full_text = "\n".join(tr.transcript_lines)
    except Exception:
        full_text = None
    executive = None
    if api_key and llm_model and full_text:
        # Stronger final summary prompt (user-provided template) and larger budget
        try:
            import requests  # type: ignore
            url = (base_url.rstrip('/') if base_url else "https://api.openai.com/v1") + "/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            prompt = (
                "# IDENTITY and PURPOSE\n\n"
                "You are an AI assistant specialized in analyzing meeting transcripts and extracting key information. "
                "Your goal is to provide comprehensive yet concise summaries that capture the essential elements of meetings in a structured format.\n\n"
                "# STEPS\n\n"
                "- Extract a brief overview of the meeting in 25 words or less, including the purpose and key participants into a section called OVERVIEW.\n\n"
        "- Extract 10-20 of the most important discussion points from the meeting into a section called KEY POINTS. Focus on core topics, debates, and significant ideas discussed.\n\n"
        "- Extract all action items and assignments mentioned in the meeting into a section called TASKS. Include responsible parties and deadlines where specified.\n\n"
        "- Extract 5-10 of the most important decisions made during the meeting into a section called DECISIONS.\n\n"
        "- Extract any notable challenges, risks, or concerns raised during the meeting into a section called CHALLENGES.\n\n"
        "- Extract all deadlines, important dates, and milestones mentioned into a section called TIMELINE.\n\n"
        "- Extract all references to documents, tools, projects, or resources mentioned into a section called REFERENCES.\n\n"
        "- Compare meeting statements against any provided context sources and capture overlaps, confirmations, or conflicts in a section called CONTEXT ALIGNMENT, citing the relevant source label.\n\n"
        "- If no alignment exists, still include CONTEXT ALIGNMENT with the bullet `- No information available; meeting dialogue lacked references to provided external context sources or citations currently recorded.`\n\n"
        "- Extract 5-10 of the most important follow-up items or next steps into a section called NEXT STEPS.\n\n"
        "# OUTPUT INSTRUCTIONS\n\n"
        "- Only output Markdown.\n\n"
        "- Write the KEY POINTS bullets as exactly 16 words.\n\n"
        "- Write the TASKS bullets as exactly 16 words.\n\n"
        "- Write the DECISIONS bullets as exactly 16 words.\n\n"
        "- Write the NEXT STEPS bullets as exactly 16 words.\n\n"
        "- Write the CONTEXT ALIGNMENT bullets as exactly 16 words.\n\n"
        "- If no alignment exists, output the exact bullet `- No information available; meeting dialogue lacked references to provided external context sources or citations currently recorded.`\n\n"
        "- Use bulleted lists for all sections, not numbered lists.\n\n"
        "- Do not repeat information across sections.\n\n"
        "- Do not start items with the same opening words.\n\n"
        "- For any bullet that relies on context rather than transcript alone, append [context: LABEL] using the label shown in the context headers.\n\n"
        "- If information for a section is not available in the transcript, write \"No information available\".\n\n"
        "- Do not include warnings or notes; only output the requested sections.\n\n"
        "- Format each section header in bold using markdown.\n\n"
        "# INPUT\n\n"
        "INPUT:"
            )
            messages = [
                {"role": "system", "content": prompt + ("\nUse CONTEXT if provided to ground references, but do not invent details." if (ctx_text_final or None) else "")},
            ]
            if ctx_labels_final:
                src_blob = "\n".join(f"- {lbl}" for lbl in ctx_labels_final[:12])
                messages.append({"role": "system", "content": "CONTEXT SOURCES:\n" + src_blob})
            if ctx_text_final:
                messages.append({"role": "system", "content": ("CONTEXT (may be partial):\n" + ctx_text_final[-12000:])})
            messages.append({"role": "user", "content": full_text[-20000:]})
            data: Dict[str, object] = {
                "model": llm_model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 800,
            }
            attempt = 0
            while True:
                r = requests.post(url, headers=headers, json=data, timeout=35)
                if r.status_code == 200:
                    executive = r.json().get("choices", [{}])[0].get("message", {}).get("content")
                    break
                if r.status_code == 400 and "max_tokens" in r.text and "max_completion_tokens" in r.text and 'max_completion_tokens' not in data:
                    data["max_completion_tokens"] = data.pop("max_tokens", 800)
                    attempt += 1
                    if attempt < 3:
                        continue
                if r.status_code == 400 and '"param": "temperature"' in r.text and 'temperature' in data:
                    data.pop("temperature", None)
                    attempt += 1
                    if attempt < 3:
                        continue
                break
        except Exception:
            pass
        if executive:
            lines = executive.splitlines()
            header_idx = next((i for i, line in enumerate(lines) if line.strip().lower().startswith("**context alignment**")), None)
            if header_idx is None:
                executive = executive.rstrip() + "\n\n**CONTEXT ALIGNMENT**\n" + _CONTEXT_FALLBACK_BULLET + "\n"
            else:
                has_bullet = False
                for line in lines[header_idx + 1:]:
                    stripped = line.strip()
                    if stripped.startswith("**"):
                        break
                    if stripped.startswith("-"):
                        has_bullet = True
                        break
                if not has_bullet:
                    executive = executive.rstrip() + "\n" + _CONTEXT_FALLBACK_BULLET + "\n"
        if executive and not final_analysis:
            final_analysis = executive
    lists = state.get_lists()
    qas = state.get_qas()
    chat_pairs = [(q, a or "") for q, a, pending in state.get_chat_history() if not pending]
    tr.write_notes(
        source_label=src,
        sink_label=sink,
        llm_model=(llm_model if api_key else None),
        analysis_text=final_analysis or None,
        lists=lists,
        executive_summary=executive,
        prompt_label=summary_prompt_label,
        qas=qas if qas else None,
        chats=chat_pairs if chat_pairs else None,
        context_files=(ctx_labels_final if ctx_labels_final else None)
    )


if __name__ == "__main__":
    main()
