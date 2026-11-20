"""
J.A.R.V.I.S Core  -  The Silent Exploit Base Project for learning
Signature: AR-Jatala

Voice assistant with a Flask backend and a pywebview HUD (index.html).

Run:    python app.py
Build:  build.bat
"""
import ctypes
import datetime
import ast
import io
import json
import operator
import random
import logging
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

import psutil
import pyttsx3
import requests
import speech_recognition as sr
from flask import Flask, Response, abort, jsonify, request, send_file

try:
    import webview
except ImportError:  # falls back to the default browser
    webview = None

# ---------------------------------------------------------------------------
# Configuration (override with environment variables)
# ---------------------------------------------------------------------------
APP_NAME = "JARVIS"
HOME = os.path.expanduser("~")
IS_WINDOWS = os.name == "nt"
START_PORT = int(os.environ.get("JARVIS_PORT", "5000"))
DEFAULT_CITY = os.environ.get("JARVIS_CITY", "Peshawar")
STT_MODE = os.environ.get("JARVIS_STT", "auto").lower()   # auto | google | whisper
VOICE_HINT = os.environ.get("JARVIS_VOICE", "").lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:1b")
MAX_MESSAGES = 300
# Spoken (and shown) once when the loading screen appears. Edit this line to change it.
STARTUP_TEXT = os.environ.get("JARVIS_STARTUP", "The Silent Exploit. Signature, A R Jatala.")

# ---------------------------------------------------------------------------
# Logging (the frozen --noconsole build has no console, so log to a file)
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", HOME), APP_NAME)
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=os.path.join(LOG_DIR, "jarvis.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
except OSError:
    logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jarvis")
logging.getLogger("werkzeug").setLevel(logging.ERROR)   # no per-request log spam

if getattr(sys, "frozen", False) or sys.stdout is None or sys.stderr is None:
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")


def resource_path(relative_path):
    """Resolve a bundled resource (works from source and from PyInstaller)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


def known_folder(csidl, fallback_name):
    """Real Windows folder path (handles OneDrive-redirected Desktop/Documents)."""
    if IS_WINDOWS:
        try:
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(HOME, fallback_name)


DESKTOP = known_folder(0x10, "Desktop")
DOCUMENTS = known_folder(0x05, "Documents")
PICTURES = known_folder(0x27, "Pictures")
MUSIC = known_folder(0x0D, "Music")
VIDEOS = known_folder(0x0E, "Videos")
DOWNLOADS = os.path.join(HOME, "Downloads")


def find_free_port(start):
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free local port available")


PORT = find_free_port(START_PORT)
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
API_TOKEN = secrets.token_hex(16)   # injected into the HUD page; required on POST requests

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
app = Flask(__name__)

state_lock = threading.Lock()
command_lock = threading.RLock()
mic_active = False
listening = False
processing = 0
speech_pending = 0          # replies queued for speech but not finished yet
messages = []
message_id_counter = 0
last_poll = 0.0
startup_spoken = False
speak_queue = queue.Queue()
voice = {"enabled": True, "rate": 1, "volume": 100, "engine": "starting",
         "voice": os.environ.get("JARVIS_NEURAL_VOICE", "en-US-AriaNeural")}   # or "windows" for offline voices
TTS_CACHE = os.path.join(LOG_DIR, "tts_cache")


def current_status():
    with state_lock:
        if speech_pending > 0:
            return "SPEAKING"
        if processing > 0:
            return "PROCESSING"
        if listening:
            return "LISTENING"
        if mic_active:
            return "STANDBY"
        return "ONLINE"


def add_message(user_msg=None, jarvis_msg=None):
    global message_id_counter, speech_pending
    with state_lock:
        message_id_counter += 1
        item = {"id": message_id_counter}
        if user_msg:
            item["user"] = user_msg
        if jarvis_msg:
            item["jarvis"] = jarvis_msg
            if voice["enabled"]:
                speech_pending += 1
                speak_queue.put(jarvis_msg)
        messages.append(item)
        del messages[:-MAX_MESSAGES]


# ---------------------------------------------------------------------------
# Text to speech (single worker thread owns the engine)
# ---------------------------------------------------------------------------
def play_mp3(path, volume):
    """Play an mp3 with Windows MCI (no extra packages). Blocks until finished."""
    if not IS_WINDOWS:
        raise RuntimeError("mp3 playback needs Windows")
    mci = ctypes.windll.winmm.mciSendStringW
    mci("close jarvis_tts", None, 0, 0)
    if mci(f'open "{path}" type mpegvideo alias jarvis_tts', None, 0, 0):
        raise RuntimeError("could not open audio")
    try:
        mci(f"setaudio jarvis_tts volume to {int(volume) * 10}", None, 0, 0)
        if mci("play jarvis_tts wait", None, 0, 0):
            raise RuntimeError("could not play audio")
    finally:
        mci("close jarvis_tts", None, 0, 0)


def tts_worker():
    """Speech thread. Order: neural female voice (edge-tts, online) > Windows SAPI > pyttsx3 > PowerShell."""
    global speech_pending
    engines = []          # (name, callable)
    skip_until = {}
    hints = [VOICE_HINT] if VOICE_HINT else ["zira", "hazel", "susan", "catherine", "eva", "aria", "jenny", "david"]
    os.makedirs(TTS_CACHE, exist_ok=True)

    def say_neural(t):
        import asyncio
        import hashlib
        import edge_tts
        rate = f"{int(voice['rate']) * 5:+d}%"
        path = os.path.join(TTS_CACHE, hashlib.sha1(f"{voice['voice']}|{rate}|{t}".encode()).hexdigest()[:20] + ".mp3")
        if not os.path.exists(path):          # first time only; repeats (e.g. the greeting) play from the cache
            tmp = path + ".part"
            asyncio.run(asyncio.wait_for(edge_tts.Communicate(t, voice["voice"], rate=rate).save(tmp), 12))
            os.replace(tmp, path)
            files = sorted((os.path.join(TTS_CACHE, f) for f in os.listdir(TTS_CACHE)), key=os.path.getmtime)
            for old in files[:-80]:
                os.remove(old)
        play_mp3(path, voice["volume"])
    engines.append(("Neural voice", say_neural))

    try:
        import pythoncom
        pythoncom.CoInitialize()
        import win32com.client
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        voices = sp.GetVoices()
        for hint in hints:
            idx = next((i for i in range(voices.Count) if hint and hint in voices.Item(i).GetDescription().lower()), None)
            if idx is not None:
                sp.Voice = voices.Item(idx)
                break

        def say_sapi(t):
            sp.Volume, sp.Rate = voice["volume"], voice["rate"]
            sp.Speak(t)
        engines.append(("Windows SAPI", say_sapi))
    except Exception:
        log.exception("SAPI unavailable")

    py = {}

    def say_py(t):
        if "e" not in py:
            py["e"] = pyttsx3.init()
        py["e"].setProperty("rate", 150 + voice["rate"] * 12)
        py["e"].setProperty("volume", voice["volume"] / 100)
        py["e"].say(t)
        py["e"].runAndWait()
    engines.append(("pyttsx3", say_py))

    def say_ps(t):
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.Rate = {int(voice['rate'])}; $s.Volume = {int(voice['volume'])}; $s.Speak($env:JARVIS_SAY)")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], env={**os.environ, "JARVIS_SAY": t},
                       check=True, timeout=90, creationflags=0x08000000 if IS_WINDOWS else 0)
    engines.append(("PowerShell", say_ps))

    while True:
        text = speak_queue.get()
        if text is None:
            break
        try:
            if voice["enabled"]:
                spoke = False
                for name, fn in engines:
                    if name == "Neural voice" and voice["voice"] == "windows":
                        continue
                    if time.time() < skip_until.get(name, 0):
                        continue
                    try:
                        fn(str(text))
                        voice["engine"], spoke = name, True
                        break
                    except Exception:
                        log.exception("TTS engine %s failed", name)
                        if name == "Neural voice":
                            skip_until[name] = time.time() + 60     # offline or not installed: retry in a minute
                if not spoke:
                    voice["engine"] = "none"
                    notify_once("tts", "Voice output failed on every engine. See the log file for details.")
        finally:
            with state_lock:
                speech_pending = max(0, speech_pending - 1)
            speak_queue.task_done()


# ---------------------------------------------------------------------------
# Speech to text: Google (online) with automatic offline fallback to Whisper
# ---------------------------------------------------------------------------
_whisper = None
_last_warning = {}


def notify_once(key, text, cooldown=120):
    now = time.time()
    if now - _last_warning.get(key, 0) > cooldown:
        _last_warning[key] = now
        add_message(jarvis_msg=text)


def whisper_transcribe(audio):
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("tiny.en", device="cpu", compute_type="int8",
                                cpu_threads=min(8, os.cpu_count() or 4))
    segments, _ = _whisper.transcribe(io.BytesIO(audio.get_wav_data()), beam_size=1, language="en")
    return " ".join(s.text.strip() for s in segments).strip()


def transcribe(recognizer, audio):
    if STT_MODE in ("auto", "google"):
        try:
            return recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as exc:
            log.warning("Google speech unavailable: %s", exc)
            if STT_MODE == "google":
                notify_once("stt", "Speech service unreachable. Check your internet connection.")
                return ""
    try:
        return whisper_transcribe(audio)
    except ImportError:
        notify_once("stt", "Speech service unreachable and the offline engine is not installed. "
                           "Install faster-whisper for offline voice input.")
    except Exception:
        log.exception("Whisper transcription failed")
    return ""


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------
def press_key(vk_code, count=1):
    if not IS_WINDOWS:
        return False
    for _ in range(count):
        ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)
        time.sleep(0.05)
    return True


def open_path(path):
    os.startfile(path)  # Windows only


def shell_start(command):
    """Only ever called with commands from the whitelists below."""
    subprocess.Popen(command, shell=True)


def google_url(query):
    return "https://www.google.com/search?q=" + urllib.parse.quote(query)


# ---------------------------------------------------------------------------
# Whitelists (apps, system locations, folders, sites)
# ---------------------------------------------------------------------------
# (spoken aliases, launch command, process image names)
APPS = [
    (("edge", "microsoft edge"), "msedge", ("msedge.exe",)),
    (("chrome", "google chrome"), "chrome", ("chrome.exe",)),
    (("firefox",), "firefox", ("firefox.exe",)),
    (("notepad",), "notepad", ("notepad.exe",)),
    (("calculator", "calc"), "calc", ("calculatorapp.exe", "calc.exe", "calculator.exe")),
    (("paint",), "mspaint", ("mspaint.exe",)),
    (("word", "microsoft word"), "winword", ("winword.exe",)),
    (("excel",), "excel", ("excel.exe",)),
    (("powerpoint",), "powerpnt", ("powerpnt.exe",)),
    (("spotify",), "spotify", ("spotify.exe",)),
    (("vscode", "vs code", "visual studio code", "code"), "code", ("code.exe",)),
    (("task manager", "taskmgr"), "taskmgr", ("taskmgr.exe",)),
]
APP_LOOKUP = {alias: (launch, procs) for aliases, launch, procs in APPS for alias in aliases}

SYSTEM_LOCATIONS = {
    "this pc": "explorer.exe shell:::{20D04FE0-3AEA-1069-A2D8-08002B30309D}",
    "pc": "explorer.exe shell:::{20D04FE0-3AEA-1069-A2D8-08002B30309D}",
    "computer": "explorer.exe shell:::{20D04FE0-3AEA-1069-A2D8-08002B30309D}",
    "recycle bin": "explorer.exe shell:::{645FF040-5081-101B-9F08-00AA002F954E}",
    "trash": "explorer.exe shell:::{645FF040-5081-101B-9F08-00AA002F954E}",
    "control panel": "control.exe",
    "settings": "start ms-settings:",
    "pc settings": "start ms-settings:",
    "windows settings": "start ms-settings:",
    "command prompt": "start cmd",
    "cmd": "start cmd",
    "terminal": "start cmd",
}

FOLDERS = {
    "downloads": DOWNLOADS, "download": DOWNLOADS,
    "documents": DOCUMENTS, "desktop": DESKTOP,
    "pictures": PICTURES, "photos": PICTURES,
    "videos": VIDEOS, "movies": VIDEOS,
    "music": MUSIC,
}

SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "github": "https://github.com",
    "whatsapp": "https://web.whatsapp.com", "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
}

DOMAIN_RX = re.compile(r"^(?:https?://)?((?:[a-z0-9-]+\.)+(?:com|org|net|io|gov|edu|ai|pk|co|dev|app|info))(/\S*)?$")

PROTECTED_PROCESSES = {
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "explorer.exe", "dwm.exe", "fontdrvhost.exe",
    "python.exe", "pythonw.exe", "jarvis.exe", "msedgewebview2.exe",
}

# ---------------------------------------------------------------------------
# Command routing
# ---------------------------------------------------------------------------
ROUTES = []
pending = {"fn": None, "expires": 0.0}
active_timers = []
FILLER_RX = re.compile(r"\b(jarvis|please|kindly|can you|could you|would you|will you|for me)\b")
QUESTION_RX = re.compile(r"^(what|who|whom|whose|when|where|why|how|which|is|are|was|were|do|does|did|can|"
                         r"could|will|would|should|tell me|explain|define|meaning of)\b")
YES_RX = re.compile(r"(yes|confirm|confirmed|do it|proceed|go ahead|affirmative)")
NO_RX = re.compile(r"(no|cancel|negative|abort|stop|never mind|nevermind)")
llm_history = [{"role": "system", "content": "You are J.A.R.V.I.S, a direct and efficient AI assistant. "
                                              "Answer in one or two short sentences."}]
_ollama_state = (0.0, False)


def route(pattern):
    rx = re.compile(pattern)

    def decorator(fn):
        ROUTES.append((rx, fn))
        return fn
    return decorator


def normalize(raw):
    t = raw.lower().strip()
    t = re.sub(r"\bwhat'?s\b", "what is", t)
    t = re.sub(r"\s+dot\s+", ".", t)
    t = re.sub(r"[^\w\s.:/\-+*()^%!,]", " ", t)
    t = FILLER_RX.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" .?,")
    return re.sub(r"(?<!\d)!+$", "", t).strip()


def ask_confirm(prompt, fn):
    pending["fn"] = fn
    pending["expires"] = time.time() + 20
    return f"{prompt} Say confirm to proceed."


def run_command(raw):
    """Thread-safe entry point; never raises."""
    with command_lock:
        try:
            return execute_command(raw)
        except Exception:
            log.exception("Command failed: %r", raw)
            return "That command failed. The details are in the log file."


def execute_command(raw):
    text = normalize(raw)
    if not text:
        return "At your service."

    if pending["fn"]:
        fn, still_valid = pending["fn"], time.time() < pending["expires"]
        pending["fn"] = None
        if still_valid and YES_RX.fullmatch(text):
            return fn()
        if still_valid and NO_RX.fullmatch(text):
            return "Cancelled."
        # anything else: the confirmation is dropped and the new command is handled normally

    for rx, fn in ROUTES:
        m = rx.search(text)
        if m:
            result = fn(m, text)
            if result:
                return result
    return fallback(raw, text)


# --- conversation / identity -----------------------------------------------
@route(r"^(?:hello|hi|hey|good (?:morning|afternoon|evening))\b")
def _greeting(m, text):
    return "Hello. How can I assist?"


@route(r"\b(?:what can you do|help|commands)\b")
def _help(m, text):
    return ("I can control volume and media, open apps, folders and websites, close programs, search the web "
            "or YouTube, set timers, take notes, report system status and weather, lock or shut down this PC, "
            "do math, keep a task list, tell jokes, and answer questions.")


@route(r"\bwho are you\b|\byour name\b|\bwho (?:made|created) you\b|\bsignature\b")
def _identity(m, text):
    return ("I am J.A.R.V.I.S., core operating intelligence for The Silent Exploit. Signature, A R Jatala.")


@route(r"\b(?:stop|turn off|disable) listening\b|^go to sleep$")
def _stop_listening(m, text):
    global mic_active
    with state_lock:
        mic_active = False
    return "Audio input deactivated."


@route(r"\b(?:clear|reset|forget|delete|erase|wipe) (?:the |my |all )?(?:memory|conversation|chat|messages|history)\b|\bforget everything\b")
def _clear_memory(m, text):
    clear_chat()
    return "Chat and memory cleared."


# --- calculator, fun, tasks, network ------------------------------------------
import cmath
import math
import statistics


def _fact(n):
    if n != int(n) or not 0 <= n <= 1000:
        raise ValueError("bad factorial")
    return math.factorial(int(n))


_FUNCS = {
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "sin": lambda x: math.sin(math.radians(x)), "cos": lambda x: math.cos(math.radians(x)),
    "tan": lambda x: math.tan(math.radians(x)), "log": math.log10, "ln": math.log, "log2": math.log2,
    "exp": math.exp, "abs": abs, "floor": math.floor, "ceil": math.ceil, "round": round,
    "gcd": math.gcd, "lcm": math.lcm, "comb": math.comb, "perm": math.perm, "min": min, "max": max,
    "factorial": _fact,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}


def _calc(node):
    """Safe evaluator: numbers, constants, + - * / // % **, and whitelisted functions. No eval."""
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _calc(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        x, y = _calc(node.left), _calc(node.right)
        if isinstance(node.op, ast.Pow) and (abs(y) > 1000 or (isinstance(x, int) and abs(x) > 1 and y > 0
                                                               and y * math.log10(abs(x)) > 400)):
            raise ValueError("too large")
        return _BIN[type(node.op)](x, y)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
            and not node.keywords and len(node.args) <= 4):
        return _FUNCS[node.func.id](*[_calc(arg) for arg in node.args])
    raise ValueError("unsupported")


def _fmt(v):
    if isinstance(v, int):
        s = str(v)
        return s if len(s) <= 15 else f"{s[0]}.{s[1:6]} times 10 to the power {len(s) - 1}"
    if v != v or v in (math.inf, -math.inf):
        raise ValueError("not finite")
    r = round(v, 8)
    return str(int(r)) if r.is_integer() and abs(r) < 1e15 else f"{r:.10g}"


_ARG = r"(-?\d+(?:\.\d+)?|\([^)]*\))"
_PHRASES = [
    (r"\bfactorial(?: of)?\s*(\d+)", r"factorial(\1)"), (r"\b(\d+)\s*(?:factorial|!)", r"factorial(\1)"),
    (r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*of\s*(\d+(?:\.\d+)?)", r"(\1/100*\2)"),
    (r"\b(\d+)\s*choose\s*(\d+)", r"comb(\1,\2)"),
    (r"\b(gcd|lcm)(?: of)?\s*(\d+)\s*(?:and|,)\s*(\d+)", r"\1(\2,\3)"),
] + [(rf"\b{w}(?: of)?\s*{_ARG}", rf"{f}(\1)") for w, f in (
    ("square root", "sqrt"), ("cube root", "cbrt"), ("sqrt", "sqrt"), ("natural log(?:arithm)?", "ln"),
    ("ln", "ln"), ("log(?:arithm)?(?: base 10)?", "log"), ("sine", "sin"), ("sin", "sin"), ("cosine", "cos"),
    ("cos", "cos"), ("tangent", "tan"), ("tan", "tan"), ("exp", "exp"))] + [
    (r"(\d+(?:\.\d+)?)\s*squared\b", r"(\1)**2"), (r"(\d+(?:\.\d+)?)\s*cubed\b", r"(\1)**3"),
    (r"\bto the power of\b|\bpower\b|\^", "**"), (r"\bplus\b", "+"), (r"\bminus\b", "-"),
    (r"\b(?:times|multiplied by|into)\b", "*"), (r"\b(?:divided by|over)\b", "/"), (r"\bmod(?:ulo)?\b", "%"),
    (r"(?<=\d)\s*x\s*(?=\d)", "*"),
]


@route(r"^(?:(?:calculate|compute|calc|evaluate|what is|how much is)\s+)?(.+)$")
def _calculator(m, text):
    e = m.group(1)
    for pat, rp in _PHRASES:
        e = re.sub(pat, rp, e)
    e = e.strip()
    if re.search(r"[^a-z0-9_\s.+\-*/%(),]", e) or not re.search(r"[+\-*/%(]", e):
        return None   # not maths: let other routes handle it
    if any(n not in _FUNCS and n not in _CONSTS for n in re.findall(r"[a-z_]+", e)):
        return None
    try:
        return f"The answer is {_fmt(_calc(ast.parse(e, mode='eval')))}."
    except SyntaxError:
        return None
    except Exception:
        return "I could not compute that."


def _factors(n):
    out, d = [], 2
    while d * d <= n:
        while n % d == 0:
            out.append(d)
            n //= d
        d += 1
    return out + ([n] if n > 1 else [])


@route(r"\bis (\d+) (?:a )?prime\b")
def _is_prime(m, text):
    n = int(m.group(1))
    if n > 10 ** 12:
        return "That number is too large for me to test."
    return f"{n} is {'' if _factors(n) == [n] else 'not '}a prime number."


@route(r"\bprime factors? of (\d+)")
def _prime_factors(m, text):
    n = int(m.group(1))
    if not 1 < n <= 10 ** 12:
        return "Give me a number between 2 and one trillion."
    return f"{n} equals " + " times ".join(map(str, _factors(n))) + "."


@route(r"\b(\d+)(?:st|nd|rd|th)?\s*fibonacci\b|\bfibonacci(?: number)?(?: of)?\s*(\d+)")
def _fibonacci(m, text):
    n = int(m.group(1) or m.group(2))
    if n > 1000:
        return "Please keep it at or below one thousand."
    a_, b_ = 0, 1
    for _ in range(n):
        a_, b_ = b_, a_ + b_
    return f"Fibonacci number {n} is {_fmt(a_)}."


@route(r"\bquadratic\b.*?(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)")
def _quadratic(m, text):
    qa, qb, qc = (float(g) for g in m.groups())
    if qa == 0:
        return "That is not a quadratic, because a is zero."
    disc = qb * qb - 4 * qa * qc
    if disc >= 0:
        r1, r2 = (-qb + math.sqrt(disc)) / (2 * qa), (-qb - math.sqrt(disc)) / (2 * qa)
        return f"The roots are {_fmt(r1)} and {_fmt(r2)}." if disc else f"The double root is {_fmt(r1)}."
    z = cmath.sqrt(disc) / (2 * qa)
    return f"The roots are complex: {_fmt(-qb / (2 * qa))} plus or minus {_fmt(abs(z.imag))} i."


@route(r"^(?:what is (?:the )?)?(average|mean|median|sum|total|product|standard deviation|variance|maximum|"
       r"minimum|largest|smallest)(?: of)?\s+(.+)$")
def _stats(m, text):
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(2))]
    if not nums:
        return None
    kind = m.group(1)
    fn = {"average": statistics.mean, "mean": statistics.mean, "median": statistics.median, "sum": sum,
          "total": sum, "product": math.prod, "maximum": max, "largest": max, "minimum": min,
          "smallest": min}.get(kind)
    if fn is None:
        if len(nums) < 2:
            return "I need at least two numbers."
        fn = statistics.stdev if kind == "standard deviation" else statistics.variance
    return f"The {kind} is {_fmt(float(fn(nums)))}."


_UNITS = {
    "length": {"m": 1, "meter": 1, "meters": 1, "km": 1000, "kilometer": 1000, "kilometers": 1000, "cm": .01,
               "mm": .001, "mile": 1609.344, "miles": 1609.344, "foot": .3048, "feet": .3048, "ft": .3048,
               "inch": .0254, "inches": .0254, "yard": .9144, "yards": .9144},
    "mass": {"kg": 1, "kilogram": 1, "kilograms": 1, "g": .001, "gram": .001, "grams": .001, "lb": .45359237,
             "lbs": .45359237, "pound": .45359237, "pounds": .45359237, "oz": .0283495, "ounce": .0283495},
    "volume": {"l": 1, "liter": 1, "liters": 1, "litre": 1, "litres": 1, "ml": .001, "gallon": 3.78541,
               "gallons": 3.78541, "cup": .24, "cups": .24},
}
_TEMPS = {"c": "c", "celsius": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}


@route(r"\bconvert\s+(-?\d+(?:\.\d+)?)\s*([a-z]+)\s+(?:to|into|in)\s+([a-z]+)")
def _convert(m, text):
    v, src, dst = float(m.group(1)), m.group(2), m.group(3)
    if src in _TEMPS and dst in _TEMPS:
        c = {"c": v, "f": (v - 32) * 5 / 9, "k": v - 273.15}[_TEMPS[src]]
        out = {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[_TEMPS[dst]]
        return f"{_fmt(v)} {src} is {_fmt(out)} {dst}."
    for table in _UNITS.values():
        if src in table and dst in table:
            return f"{_fmt(v)} {src} is {_fmt(v * table[src] / table[dst])} {dst}."
    return None


JOKES = ["Why do programmers prefer dark mode? Because light attracts bugs.",
         "There are ten kinds of people: those who understand binary and those who do not.",
         "I would tell you a UDP joke, but you might not get it.",
         "A SQL query walks into a bar, sees two tables and asks: can I join you?",
         "Why did the developer go broke? He used up all his cache."]


@route(r"\bjoke\b")
def _joke(m, text):
    return random.choice(JOKES)


@route(r"\b(?:flip|toss) a coin\b")
def _coin(m, text):
    return f"It landed on {random.choice(['heads', 'tails'])}."


@route(r"\broll (?:a |the )?(?:dice|die)\b")
def _dice(m, text):
    return f"You rolled a {random.randint(1, 6)}."


@route(r"\bip address\b")
def _ip(m, text):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))     # no packet is sent; just picks the active interface
            return f"Your local IP address is {s.getsockname()[0]}."
    except OSError:
        return "I could not determine the network address."


TODO_FILE = os.path.join(LOG_DIR, "todo.json")


def _load_todos():
    try:
        with open(TODO_FILE, encoding="utf-8") as f:
            return list(json.load(f))
    except (OSError, ValueError):
        return []


def _save_todos(items):
    with open(TODO_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f)


@route(r"^(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:my\s+)?(?:to[- ]?do|tasks?)(?:\s+list)?$")
def _todo_add(m, text):
    items = _load_todos()
    items.append(m.group(1))
    _save_todos(items)
    return f"Added {m.group(1)} to your task list."


@route(r"^(?:read|show|list)\s+(?:my\s+)?(?:to[- ]?do|tasks?)(?:\s+list)?$|^my tasks$")
def _todo_read(m, text):
    items = _load_todos()
    if not items:
        return "Your task list is empty."
    return (f"You have {len(items)} task{'s' if len(items) != 1 else ''}: "
            + ". ".join(f"{i + 1}, {t}" for i, t in enumerate(items)) + ".")


@route(r"^(?:done|complete|finish|remove)\s+task\s+(\d+)$")
def _todo_done(m, text):
    items, i = _load_todos(), int(m.group(1)) - 1
    if not 0 <= i < len(items):
        return "There is no such task number."
    done = items.pop(i)
    _save_todos(items)
    return f"Completed {done}."


@route(r"^(?:clear|empty|delete)\s+(?:my\s+)?(?:to[- ]?do|tasks?)(?:\s+list)?$")
def _todo_clear(m, text):
    return ask_confirm("Clear your whole task list?", lambda: (_save_todos([]), "Task list cleared.")[1])


# --- volume and media -------------------------------------------------------
def _media(vk, count, message):
    if not press_key(vk, count):
        return "Media keys are only supported on Windows."
    return message


@route(r"\bvolume up\b|\bincrease volume\b|\braise volume\b|\bvolume higher\b|\blouder\b")
def _vol_up(m, text):
    return _media(0xAF, 5, "Increasing audio output level.")


@route(r"\bvolume down\b|\bdecrease volume\b|\blower volume\b|\bvolume lower\b|\bquieter\b")
def _vol_down(m, text):
    return _media(0xAE, 5, "Decreasing audio output level.")


@route(r"\b(?:un)?mute\b")
def _mute(m, text):
    return _media(0xAD, 1, "Toggling master audio mute.")


@route(r"^(?:next|skip)(?: the)?(?: track| song)?$|\bnext (?:track|song)\b")
def _next(m, text):
    return _media(0xB0, 1, "Skipping to the next media item.")


@route(r"^previous(?: track| song)?$|\b(?:previous|last) (?:track|song)\b")
def _prev(m, text):
    return _media(0xB1, 1, "Returning to the previous media item.")


@route(r"^(?:(?:pause|resume|stop|play)(?: the)?(?: music| video| media| song| playback)|pause|resume|play)$")
def _play_pause(m, text):
    return _media(0xB3, 1, "Toggling media playback.")


# --- power ------------------------------------------------------------------
@route(r"\block (?:the )?(?:pc|computer|screen|workstation|laptop)\b")
def _lock(m, text):
    if not IS_WINDOWS:
        return "Locking is only supported on Windows."
    ctypes.windll.user32.LockWorkStation()
    return "Workstation secured. Screen locked."


def _do_power(args, message):
    subprocess.run(["shutdown", *args, "/t", "10"], check=False)
    return message + " Say cancel shutdown to abort."


@route(r"\b(?:cancel|abort) (?:the )?(?:shutdown|restart|reboot)\b")
def _cancel_power(m, text):
    subprocess.run(["shutdown", "/a"], check=False)
    return "Power sequence aborted."


@route(r"\b(?:shut ?down|power off|turn off) (?:the )?(?:system|computer|pc|laptop)\b|^shut ?down$")
def _shutdown(m, text):
    return ask_confirm("Shut down this computer?",
                       lambda: _do_power(["/s"], "Shutting down in 10 seconds."))


@route(r"\b(?:restart|reboot)(?: the)?(?: system| computer| pc| laptop)?$")
def _restart(m, text):
    return ask_confirm("Restart this computer?",
                       lambda: _do_power(["/r"], "Restarting in 10 seconds."))


# --- timers and notes -------------------------------------------------------
@route(r"\btimer\b|\bremind me\b")
def _timer(m, text):
    d = re.search(r"\b(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", text)
    if not d:
        return "How long should the timer run?"
    amount, unit = int(d.group(1)), d.group(2)
    seconds = amount * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    if not 0 < seconds <= 86400:
        return "Timers must be between one second and twenty four hours."
    note = re.search(r"\bto (.+)$", text)
    message = f"Reminder: {note.group(1)}." if note else "Your timer is complete."
    t = threading.Timer(seconds, lambda: add_message(jarvis_msg=message))
    t.daemon = True
    t.start()
    active_timers.append(t)
    return f"Timer set for {amount} {unit.rstrip('s')}{'s' if amount != 1 else ''}."


@route(r"^(?:take a note|take note|make a note|note that|remember that)\s*(.*)$")
def _note(m, text):
    body = m.group(1).strip()
    if not body:
        return "What should I note?"
    with open(os.path.join(DESKTOP, "jarvis_notes.txt"), "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] {body}\n")
    return "Noted."


@route(r"\bread (?:my )?notes\b")
def _read_notes(m, text):
    path = os.path.join(DESKTOP, "jarvis_notes.txt")
    if not os.path.exists(path):
        return "You have no notes yet."
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()][-3:]
    return "Your latest notes: " + " ".join(re.sub(r"^\[[^\]]*\]\s*", "", ln) + "." for ln in lines)


# --- time, date, diagnostics, weather --------------------------------------
@route(r"\bwhat time\b|\bcurrent time\b|\bthe time\b|\bclock\b|^time$")
def _time(m, text):
    return f"Current local time is {datetime.datetime.now():%I:%M %p}."


@route(r"\b(?:what|which) day\b|\bwhat is (?:the )?date\b|\btoday s date\b|\bdate today\b|^(?:date|today)$")
def _date(m, text):
    return f"Today is {datetime.datetime.now():%A, %B %d, %Y}."


@route(r"\b(?:cpu usage|cpu load|processor load|ram usage|memory usage|memory status|system stats|"
       r"system status|system diagnostics|battery(?: level| status)?|disk space)\b|\bhow is the system\b")
def _diagnostics(m, text):
    cpu = psutil.cpu_percent(interval=0.3)
    ram = psutil.virtual_memory().percent
    parts = [f"CPU utilization is {cpu:.0f} percent.", f"Memory allocation is {ram:.0f} percent."]
    try:
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        parts.append(f"Main disk has {disk.free / 1e9:.0f} gigabytes free.")
    except Exception:
        pass
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery:
        plugged = "charging" if battery.power_plugged else "on battery"
        parts.append(f"Battery is at {battery.percent:.0f} percent, {plugged}.")
    return "System diagnostics: " + " ".join(parts)


@route(r"\bweather\b|\btemperature\b")
def _weather(m, text):
    place = re.search(r"\b(?:in|at|for)\s+([a-z ]+)$", text)
    city = place.group(1).strip() if place else DEFAULT_CITY
    try:
        r = requests.get(f"https://wttr.in/{urllib.parse.quote(city)}?format=%C+%t+%w", timeout=6)
        body = r.text.strip()
        if r.status_code != 200 or not body or "unknown location" in body.lower():
            return f"I could not find weather data for {city}."
        return f"Weather in {city.title()}: {body}."
    except requests.RequestException:
        return "I could not reach the weather service."


# --- closing programs (always confirmed) ------------------------------------
def _kill_all(procs):
    killed = 0
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(procs, timeout=2)
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    killed = len(procs)
    return f"Terminated {killed} process{'es' if killed != 1 else ''}."


@route(r"^(?:close|kill|terminate|quit|exit|end)$")
def _kill_missing(m, text):
    return "Specify the target application to terminate."


@route(r"^(?:close|kill|terminate|quit|exit|end)\s+(?:the\s+)?(.+?)(?:\s+(?:app|application|program|process|window))?$")
def _kill(m, text):
    target = m.group(1).strip()
    if target in APP_LOOKUP:
        names = set(APP_LOOKUP[target][1])
    else:
        clean = re.sub(r"[^a-z0-9._ -]", "", target)
        if not clean:
            return "I could not interpret that process name."
        names = {clean, clean if clean.endswith(".exe") else clean + ".exe"}
    if names & PROTECTED_PROCESSES:
        return f"{target.title()} is protected and cannot be terminated."
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info["name"] or "").lower()
            if name in names and name not in PROTECTED_PROCESSES and p.info["pid"] != os.getpid():
                procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not procs:
        return f"No running process matches {target}."
    count = len(procs)
    return ask_confirm(f"Terminate {count} {target} process{'es' if count != 1 else ''}?",
                       lambda: _kill_all(procs))


# --- files and folders ------------------------------------------------------
@route(r"\bcreate (?:a )?(?:log|file)\b")
def _create_log(m, text):
    with open(os.path.join(DESKTOP, "jarvis_log.txt"), "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] Log entry by J.A.R.V.I.S.\n")
    return "Log file written to the Desktop."


@route(r"\b(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?(?:folder|directory)(?:\s+(?:called|named))?\s*(.*)$")
def _make_folder(m, text):
    name = re.sub(r"[^\w \-]", "", m.group(1)).strip().title() or "New_JARVIS_Folder"
    os.makedirs(os.path.join(DESKTOP, name), exist_ok=True)
    return f"Created folder '{name}' on the Desktop."


def _desktop_item(name):
    name = name.strip().lower()
    try:
        for entry in os.listdir(DESKTOP):
            if entry.lower() == name or os.path.splitext(entry)[0].lower() == name:
                return entry
    except OSError:
        pass
    return None


@route(r"\b(?:delete|remove|trash)\s+(?:the\s+)?(?:folder|file|directory)\s+(?:called\s+|named\s+)?(.+)$")
def _delete(m, text):
    entry = _desktop_item(m.group(1))
    if not entry:
        return f"I could not find '{m.group(1)}' on the Desktop."
    path = os.path.join(DESKTOP, entry)

    def do_delete():
        try:
            from send2trash import send2trash
        except ImportError:
            return "The send2trash module is missing, so I will not delete permanently."
        send2trash(path)
        return f"Moved '{entry}' to the Recycle Bin."
    return ask_confirm(f"Move '{entry}' from the Desktop to the Recycle Bin?", do_delete)


@route(r"\bscreenshot\b|\bscreen capture\b")
def _screenshot(m, text):
    try:
        from PIL import ImageGrab
    except ImportError:
        return "Pillow is not installed, so I cannot take screenshots."
    path = os.path.join(DESKTOP, f"jarvis_shot_{datetime.datetime.now():%Y%m%d_%H%M%S}.png")
    ImageGrab.grab().save(path)
    return "Screenshot saved to the Desktop."


# --- web --------------------------------------------------------------------
@route(r"\byoutube\b|^(?:play|watch)\s+.+")
def _youtube(m, text):
    query = re.sub(r"\b(play|watch|on|youtube|video|videos|search|for|open|launch|go to)\b", " ", text)
    query = re.sub(r"\s+", " ", query).strip()
    if query:
        webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query))
        return f"Searching YouTube for {query}."
    webbrowser.open("https://www.youtube.com")
    return "Opening YouTube."


@route(r"^(?:search|google|look up|find online|browse)(?: for| about| up)?\s+(.+)$")
def _search(m, text):
    query = m.group(1).strip()
    webbrowser.open(google_url(query))
    return f"Searching the web for {query}."


# --- open anything (whitelisted) ---------------------------------------------
def open_target(target):
    t = re.sub(r"^(?:the|my|up)\s+", "", target).strip()
    t = re.sub(r"\s+(?:folder|app|application|program|window)$", "", t).strip()
    if not t:
        return None

    if t in SYSTEM_LOCATIONS:
        shell_start(SYSTEM_LOCATIONS[t])
        return f"Opening {t.title()}."

    drive = re.fullmatch(r"([a-z])\s*(?:drive|disk)", t)
    if drive:
        path = f"{drive.group(1).upper()}:\\"
        if os.path.exists(path):
            open_path(path)
            return f"Accessing disk partition {drive.group(1).upper()}."
        return f"Partition {drive.group(1).upper()} was not detected."

    if t in FOLDERS:
        if os.path.exists(FOLDERS[t]):
            open_path(FOLDERS[t])
            return f"Accessing {t.title()}."
        return f"The {t} folder does not exist."

    if t in APP_LOOKUP:
        shell_start(f'start "" {APP_LOOKUP[t][0]}')
        return f"Launching {t.title()}."

    if t in SITES:
        webbrowser.open(SITES[t])
        return f"Opening {t.title()}."

    domain = DOMAIN_RX.match(t.replace(" ", ""))
    if domain:
        webbrowser.open("https://" + domain.group(1) + (domain.group(2) or ""))
        return f"Navigating to {domain.group(1)}."
    return None


@route(r"^(?:open|launch|start|run|go to|navigate to|take me to|show me)\s+(.+)$")
def _open(m, text):
    result = open_target(m.group(1))
    if result:
        return result
    return f"I do not have an application or location called {m.group(1)}."


# --- fallback: local LLM, then web search for questions ----------------------
def ollama_available():
    global _ollama_state
    now = time.time()
    if now - _ollama_state[0] < 30:
        return _ollama_state[1]
    try:
        ok = requests.get(f"{OLLAMA_URL}/api/tags", timeout=0.7).ok
    except requests.RequestException:
        ok = False
    _ollama_state = (now, ok)
    return ok


def ask_ollama(prompt):
    llm_history.append({"role": "user", "content": prompt})
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json={
            "model": OLLAMA_MODEL,
            "messages": [llm_history[0]] + llm_history[-10:],
            "stream": False,
            "keep_alive": "30m",
        }, timeout=(2, 60)).json()
        reply = r["message"]["content"].strip()
        llm_history.append({"role": "assistant", "content": reply})
        return reply
    except Exception:
        log.exception("Ollama request failed")
        llm_history.pop()
        return None


def fallback(raw, text):
    if ollama_available():
        reply = ask_ollama(raw)
        if reply:
            return reply
    if QUESTION_RX.match(text):
        webbrowser.open(google_url(raw))
        return f"Searching the web for: {raw}"
    return "I did not understand that command. Say help to hear what I can do."


# ---------------------------------------------------------------------------
# Input pipeline (voice and typed commands share it)
# ---------------------------------------------------------------------------
def process_input(text):
    global processing
    with state_lock:
        processing += 1
    try:
        add_message(user_msg=text)
        response = run_command(text)
        if response:
            add_message(jarvis_msg=response)
    finally:
        with state_lock:
            processing -= 1


def handle_audio(recognizer, audio):
    global processing
    with state_lock:
        processing += 1
    try:
        text = transcribe(recognizer, audio)
    except Exception:
        log.exception("Transcription error")
        text = ""
    finally:
        with state_lock:
            processing -= 1
    if text:
        process_input(text)


def listening_worker():
    global mic_active, listening
    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = True
    while True:
        with state_lock:
            want = mic_active and speech_pending == 0
        if not want:
            time.sleep(0.2)
            continue
        try:
            # The microphone is closed while JARVIS speaks so it never hears itself.
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.4)
                while True:
                    with state_lock:
                        if not mic_active or speech_pending > 0:
                            break
                        listening = True
                    try:
                        audio = recognizer.listen(source, timeout=3, phrase_time_limit=8)
                    except sr.WaitTimeoutError:
                        continue
                    finally:
                        with state_lock:
                            listening = False
                    handle_audio(recognizer, audio)
        except Exception:
            log.exception("Microphone error")
            with state_lock:
                mic_active = False
                listening = False
            add_message(jarvis_msg="Microphone unavailable. Check that one is connected and allowed "
                                   "in Windows privacy settings.")


# ---------------------------------------------------------------------------
# HTTP API (same-origin only; no CORS)
# ---------------------------------------------------------------------------
@app.before_request
def guard():
    if request.host not in ALLOWED_HOSTS:
        abort(403)
    if request.method == "POST" and request.headers.get("X-JARVIS-Token") != API_TOKEN:
        abort(403)


@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def serve_frontend():
    with open(resource_path("index.html"), "r", encoding="utf-8") as f:
        html = f.read().replace("__JARVIS_TOKEN__", API_TOKEN)
    return Response(html, mimetype="text/html")


_sys_cache = {"t": 0.0, "data": {}}


_net_last = {"t": 0.0, "rx": 0, "tx": 0}
chat_epoch = 0


def clear_chat():
    global chat_epoch
    with state_lock:
        messages.clear()
        chat_epoch += 1
    del llm_history[1:]


def sys_stats():
    """System readings, recomputed at most every 2 seconds."""
    now = time.time()
    if now - _sys_cache["t"] > 2:
        batt = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        vm, du, nio = psutil.virtual_memory(), psutil.disk_usage(os.path.abspath(os.sep)), psutil.net_io_counters()
        dt = max(now - _net_last["t"], 0.001) if _net_last["t"] else 0
        down = (nio.bytes_recv - _net_last["rx"]) / dt / 1024 if dt else 0.0
        up = (nio.bytes_sent - _net_last["tx"]) / dt / 1024 if dt else 0.0
        _net_last.update(t=now, rx=nio.bytes_recv, tx=nio.bytes_sent)
        freq = psutil.cpu_freq() if hasattr(psutil, "cpu_freq") else None
        _sys_cache["data"] = {
            "t": int(now), "cpu": psutil.cpu_percent(interval=None), "ram": vm.percent, "disk": du.percent,
            "battery": round(batt.percent) if batt else None, "uptime": int(now - psutil.boot_time()),
            "mem_used": vm.used / 1e9, "mem_total": vm.total / 1e9, "disk_used": du.used / 1e9,
            "disk_total": du.total / 1e9, "down": round(down, 1), "up": round(up, 1),
            "cores": psutil.cpu_count(logical=True), "freq": round(freq.current) if freq else None}
        _sys_cache["t"] = now
    return _sys_cache["data"]


def hero_path():
    """The centre-display photo: hero.jpg / .png / .webp next to app.py (or next to JARVIS.exe)."""
    dirs = [os.environ.get("JARVIS_HERO_DIR"), os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None,
            os.path.dirname(os.path.abspath(__file__)), getattr(sys, "_MEIPASS", None)]
    for d in dirs:
        for name in ("hero.jpg", "hero.jpeg", "hero.png", "hero.webp"):
            if d and os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
    return None


@app.route("/hero")
def hero():
    path = hero_path()
    if not path:
        abort(404)
    return send_file(path, max_age=0)


@app.route("/api/poll", methods=["GET"])
def poll():
    global last_poll, startup_spoken
    last_poll = time.time()
    if not startup_spoken:        # first page load = the loading screen is showing
        startup_spoken = True
        add_message(jarvis_msg=STARTUP_TEXT)
    since_id = request.args.get("since", 0, type=int)
    with state_lock:
        items = [m for m in messages if m["id"] > since_id]
        mic = mic_active
    return jsonify({"status": current_status(), "mic_active": mic, "items": items, "sys": sys_stats(), "epoch": chat_epoch, "voice": dict(voice)})


@app.route("/api/sysinfo", methods=["GET"])
def sysinfo():
    batt = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    return jsonify({"cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent,
                    "disk": psutil.disk_usage(os.path.abspath(os.sep)).percent,
                    "battery": round(batt.percent) if batt else None,
                    "uptime": int(time.time() - psutil.boot_time())})


@app.route("/api/toggle_mic", methods=["POST"])
def toggle_mic():
    global mic_active
    with state_lock:
        mic_active = not mic_active
        now_active = mic_active
    add_message(jarvis_msg="Audio input active. Listening for commands." if now_active
                else "Audio input deactivated.")
    return jsonify({"mic_active": now_active})


@app.route("/api/clear_chat", methods=["POST"])
def api_clear_chat():
    clear_chat()
    return jsonify({"ok": True})


@app.route("/api/voice", methods=["POST"])
def api_voice():
    d = request.get_json(silent=True) or {}
    voice["enabled"] = bool(d.get("enabled", True))
    voice["rate"] = max(-5, min(8, int(d.get("rate", 1))))
    voice["volume"] = max(0, min(100, int(d.get("volume", 100))))
    name = str(d.get("voice", voice["voice"]))
    if name == "windows" or re.fullmatch(r"[a-z]{2}-[A-Z]{2}-[A-Za-z]+Neural", name):
        voice["voice"] = name
    return jsonify(voice)


@app.route("/api/voice_test", methods=["POST"])
def api_voice_test():
    add_message(jarvis_msg="Voice check. If you can hear this, speech is working." if voice["enabled"]
                else "Spoken replies are turned off in settings.")
    return jsonify({"ok": True})


@app.route("/api/command", methods=["POST"])
def api_command():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()[:300]
    if not text:
        return jsonify({"ok": False}), 400
    threading.Thread(target=process_input, args=(text,), daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def start_flask():
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False)


def wait_for_server(timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def wait_while_browser_alive():
    """Browser fallback: exit once the page stops polling."""
    started = time.time()
    while True:
        time.sleep(2)
        if last_poll and time.time() - last_poll > 180:
            return
        if not last_poll and time.time() - started > 120:
            return


def main():
    psutil.cpu_percent(interval=None)   # prime the counter so the first reading is not 0
    threading.Thread(target=tts_worker, daemon=True).start()
    threading.Thread(target=listening_worker, daemon=True).start()
    threading.Thread(target=start_flask, daemon=True).start()
    if not wait_for_server():
        log.error("Flask server did not start on port %s", PORT)
        return

    url = f"http://127.0.0.1:{PORT}/"
    if webview is not None:
        try:
            webview.create_window("JARVIS Core", url, width=1366, height=768, background_color="#050a10")
            webview.start()
            return
        except Exception:
            log.exception("pywebview failed; falling back to the default browser")
    webbrowser.open(url)
    wait_while_browser_alive()


if __name__ == "__main__":
    main()
