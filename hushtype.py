#!/usr/bin/env python3
"""HushType (formerly LocalFlow) - private, offline push-to-talk dictation for X11 Linux.

Hold the hotkey, speak, release: your words are transcribed locally by
whisper.cpp, cleaned up, and typed into whatever window has focus.

Usage:
  hushtype.py                  run the hotkey daemon
  hushtype.py --file x.wav     transcribe a file and print the result (testing)
"""
import argparse
import json
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
import uuid
import wave

from cleanup import clean

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
CTL_SOCK = os.path.join(RUNTIME, "hushtype.sock")     # other programs send "listen" here
BUSY_FILE = os.path.join(RUNTIME, "hushtype.busy")    # exists while we record/transcribe
HUD_SOCK = os.path.join(RUNTIME, "alisha-hud.sock")   # optional ALISHA ESP32 HUD


def hud(t, v=""):
    """Mirror state on the ALISHA HUD if its bridge is running; silently no-op otherwise."""
    try:
        import socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps({"t": t, "v": v})[:1000].encode(), HUD_SOCK)
    except OSError:
        pass


def set_busy(on):
    try:
        if on:
            open(BUSY_FILE, "w").close()
        elif os.path.exists(BUSY_FILE):
            os.unlink(BUSY_FILE)
    except OSError:
        pass


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for k in ("model", "server_bin"):
        p = cfg["whisper"][k]
        cfg["whisper"][k] = p if os.path.isabs(p) else os.path.join(HERE, p)
    return cfg


# ---------------------------------------------------------------- whisper
class Whisper:
    """Keeps a whisper.cpp server running so the model stays in memory."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.url = f"http://127.0.0.1:{cfg['port']}"
        self.proc = None

    def _alive(self):
        try:
            urllib.request.urlopen(self.url + "/", timeout=1)
            return True
        except Exception as e:
            return getattr(e, "code", None) is not None  # HTTP error = server is up

    def start(self):
        if self._alive():
            log("whisper server already running on", self.url)
            return
        for p in (self.cfg["server_bin"], self.cfg["model"]):
            if not os.path.exists(p):
                sys.exit(f"Missing {p}\nRun ./setup.sh first.")
        cmd = [self.cfg["server_bin"], "-m", self.cfg["model"],
               "--host", "127.0.0.1", "--port", str(self.cfg["port"]),
               "-t", str(self.cfg["threads"]), "-l", self.cfg["language"]]
        log("starting whisper server:", os.path.basename(self.cfg["model"]))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            if self._alive():
                log("whisper ready")
                return
            if self.proc.poll() is not None:
                sys.exit("whisper server exited; run it by hand to see why:\n  " + " ".join(cmd))
            time.sleep(0.25)
        sys.exit("whisper server did not come up")

    def stop(self):
        if self.proc:
            self.proc.terminate()

    def transcribe(self, wav_path):
        boundary = uuid.uuid4().hex
        fields = {"response_format": "json", "temperature": "0.0",
                  "language": self.cfg["language"]}
        if self.cfg.get("prompt"):
            fields["prompt"] = self.cfg["prompt"]
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        with open(wav_path, "rb") as f:
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                     f"filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + f.read() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url + "/inference", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read()).get("text", "")


# ---------------------------------------------------------------- llm polish
def llm_polish(text, cfg):
    prompt = ("Clean up this dictated text. Fix grammar and punctuation, keep the speaker's "
              "wording and meaning, do not add anything. Reply with only the cleaned text.\n\n" + text)
    body = json.dumps({"model": cfg["model"], "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = urllib.request.Request(cfg["url"], data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())["choices"][0]["message"]["content"].strip()
            return out or text
    except Exception as e:
        log("llm polish skipped:", e)
        return text


# ---------------------------------------------------------------- audio
class Recorder:
    def __init__(self):
        self.proc = None
        self.path = None
        if shutil.which("pw-record"):
            self.tool = "pw-record"
        elif shutil.which("parecord"):
            self.tool = "parecord"
        elif shutil.which("arecord"):
            self.tool = "arecord"
        else:
            sys.exit("No recorder found (need pw-record, parecord or arecord)")

    def start(self):
        fd, self.path = tempfile.mkstemp(suffix=".wav", prefix="hushtype-")
        os.close(fd)
        cmd = {
            "pw-record": ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", self.path],
            "parecord": ["parecord", "--rate=16000", "--channels=1", "--format=s16le",
                         "--file-format=wav", self.path],
            "arecord": ["arecord", "-q", "-r", "16000", "-c", "1", "-f", "S16_LE", self.path],
        }[self.tool]
        self.proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
        self.t0 = time.time()

    def stop(self):
        """Returns (wav_path, seconds) or (None, 0)."""
        if not self.proc:
            return None, 0
        time.sleep(0.15)  # catch the tail of the last word
        self.proc.send_signal(signal.SIGINT)  # lets the tool finalise the WAV header
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        return self.path, time.time() - self.t0


def record_until_silence(path, threshold, silence_secs=1.2, wait_secs=6.0, max_secs=60.0):
    """Record from the default mic until the speaker pauses. Returns seconds of audio kept,
    or 0 if nobody started speaking within `wait_secs`."""
    import array
    rate, chunk = 16000, 1600  # 0.1 s
    proc = subprocess.Popen(["parec", "--rate=16000", "--channels=1", "--format=s16le", "--raw"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames, started, quiet, t, loud = [], False, 0.0, 0.0, 0
    try:
        while t < max_secs:
            data = proc.stdout.read(chunk * 2)
            if not data:
                break
            t += 0.1
            a = array.array("h", data)
            rms = math.sqrt(sum(s * s for s in a) / len(a)) if a else 0
            frames.append(data)
            loud = loud + 1 if rms >= threshold else 0
            if loud >= 2 or (started and rms >= threshold):  # 0.2 s of sound, not a click
                started, quiet = True, 0.0
            elif started:
                quiet += 0.1
                if quiet >= silence_secs:
                    break
            elif t >= wait_secs:
                return 0
            if not started and len(frames) > 3:
                frames = frames[-3:]  # keep 0.3 s of lead-in so first syllable isn't clipped
    finally:
        proc.terminate()
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(frames))
    return len(frames) * 0.1


def wav_level(path):
    """Loudest 0.25 s window RMS - speech shows up here even if most of the clip is quiet."""
    try:
        with wave.open(path) as w:
            rate, data = w.getframerate(), w.readframes(w.getnframes())
    except Exception:
        return 0
    n = len(data) // 2
    if n < rate // 10:  # < 0.1 s
        return 0
    samples = struct.unpack(f"<{n}h", data[: n * 2])
    win = rate // 4
    best = 0
    for i in range(0, n, win):
        c = samples[i:i + win:2]
        if c:
            best = max(best, math.sqrt(sum(s * s for s in c) / len(c)))
    return best


def wav_is_silent(path, threshold=200):
    return wav_level(path) < threshold


# ---------------------------------------------------------------- output
def type_text(text, cfg):
    if cfg.get("add_trailing_space") and not text.endswith("\n"):
        text += " "
    if cfg.get("method") == "paste" and shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode())
        win = subprocess.run(["xdotool", "getactivewindow", "getwindowclassname"],
                             capture_output=True, text=True).stdout.lower()
        terminal = any(t in win for t in ("term", "kitty", "alacritty", "konsole", "tilix"))
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+v" if terminal else "ctrl+v"])
    else:
        # type line by line so newlines become Return presses
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "4", "--", line])
            if i < len(lines) - 1:
                subprocess.run(["xdotool", "key", "--clearmodifiers", "shift+Return"])


def notify(msg, cfg):
    if cfg.get("notify") and shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-t", "1200", "-a", "HushType", "HushType", msg],
                         stderr=subprocess.DEVNULL)


def beep(freq, cfg):
    if not cfg.get("sounds") or not shutil.which("paplay"):
        return
    path = os.path.join(tempfile.gettempdir(), f"hushtype-beep-{freq}.wav")
    if not os.path.exists(path):
        rate, dur = 22050, 0.07
        with wave.open(path, "w") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / rate)))
                                   for i in range(int(rate * dur))))
    subprocess.Popen(["paplay", path], stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- pipeline
def process(wav, cfg, whisper):
    t0 = time.time()
    raw = whisper.transcribe(wav).strip()
    text = clean(raw, cfg.get("dictionary"))
    if text and cfg["llm"].get("enabled"):
        text = llm_polish(text, cfg["llm"])
    log(f"[{time.time() - t0:.1f}s] raw: {raw!r}")
    log(f"        out: {text!r}")
    return text


class Daemon:
    def __init__(self, cfg):
        from pynput import keyboard  # imported here so --file works without X / pynput
        self.kb = keyboard
        self.cfg = cfg
        self.whisper = Whisper(cfg["whisper"])
        self.rec = Recorder()
        self.recording = False
        self.busy = threading.Lock()
        name = cfg["hotkey"]["key"]
        self.key = getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)
        self.toggle = cfg["hotkey"]["mode"] == "toggle"

    def begin(self):
        self.recording = True
        set_busy(True)
        hud("state", "listening")
        self.rec.start()
        beep(880, self.cfg["output"])
        log("● recording")

    def end(self):
        self.recording = False
        wav, secs = self.rec.stop()
        beep(440, self.cfg["output"])
        threading.Thread(target=self._finish, args=(wav, secs), daemon=True).start()

    # ---- voice trigger (e.g. "Alisha, type") via unix socket ----
    def _socket_loop(self):
        import socket
        try:
            if os.path.exists(CTL_SOCK):
                os.unlink(CTL_SOCK)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.bind(CTL_SOCK)
            os.chmod(CTL_SOCK, 0o600)
        except OSError as e:
            log("voice trigger socket unavailable:", e)
            return
        log("voice trigger listening on", CTL_SOCK)
        while True:
            msg = s.recv(4096).decode("utf-8", "replace").strip()
            if msg == "listen":
                threading.Thread(target=self._voice_dictate, daemon=True).start()
            elif msg.startswith("text:"):
                threading.Thread(target=self._type_given, args=(msg[5:],), daemon=True).start()

    def _type_given(self, text):
        """Text already recognised by the caller (e.g. 'Alisha, type hello world')."""
        with self.busy:
            text = clean(text, self.cfg.get("dictionary"))
            log(f"  trigger text: {text!r}")
            if text:
                type_text(text, self.cfg["output"])
                hud("heard", f"[typed] {text}")

    def _voice_dictate(self):
        if self.recording or not self.busy.acquire(blocking=False):
            log("  (trigger ignored: already busy)")
            return
        set_busy(True)
        wav = None
        try:
            hud("state", "listening")
            beep(880, self.cfg["output"])
            time.sleep(0.25)  # don't record our own beep
            log("● voice-triggered recording (pause to finish)")
            fd, wav = tempfile.mkstemp(suffix=".wav", prefix="hushtype-")
            os.close(fd)
            thr = self.cfg["output"].get("silence_threshold", 200)
            secs = record_until_silence(wav, thr, self.cfg["output"].get("end_silence", 1.2))
            beep(440, self.cfg["output"])
            if secs <= 0:
                log("  (no speech heard, cancelled)")
                return
            hud("state", "thinking")
            text = process(wav, self.cfg, self.whisper)
            if text:
                type_text(text, self.cfg["output"])
                hud("heard", f"[typed] {text}")
        except Exception as e:
            log("error:", e)
        finally:
            if wav and os.path.exists(wav):
                os.unlink(wav)
            set_busy(False)
            hud("state", "idle")
            self.busy.release()

    def _finish(self, wav, secs):
        with self.busy:
            hud("state", "thinking")
            try:
                level = wav_level(wav) if wav else 0
                thr = self.cfg["output"].get("silence_threshold", 200)
                if not wav or secs < 0.35 or level < thr:
                    log(f"  (ignored: {secs:.1f}s, level {level:.0f} < {thr})")
                    return
                log(f"  {secs:.1f}s audio, level {level:.0f}")
                text = process(wav, self.cfg, self.whisper)
                if text:
                    type_text(text, self.cfg["output"])
            except Exception as e:
                log("error:", e)
                notify(f"Error: {e}", self.cfg["output"])
            finally:
                if wav and os.path.exists(wav):
                    os.unlink(wav)
                set_busy(False)
                hud("state", "idle")

    def on_press(self, key):
        if key != self.key:
            return
        if self.toggle:
            self.end() if self.recording else self.begin()
        elif not self.recording:  # ignore auto-repeat while held
            self.begin()

    def on_release(self, key):
        if key == self.key and not self.toggle and self.recording:
            self.end()

    def run(self):
        self.whisper.start()
        set_busy(False)
        threading.Thread(target=self._socket_loop, daemon=True).start()
        how = "press" if self.toggle else "hold"
        log(f"ready - {how} [{self.cfg['hotkey']['key']}] to dictate. Ctrl+C to quit.")
        notify(f"Ready - {how} {self.cfg['hotkey']['key']} to dictate", self.cfg["output"])
        try:
            with self.kb.Listener(on_press=self.on_press, on_release=self.on_release) as l:
                l.join()
        except KeyboardInterrupt:
            pass
        finally:
            set_busy(False)
            self.whisper.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "config.toml"))
    ap.add_argument("--file", help="transcribe a WAV file (16 kHz mono) and print, then exit")
    args = ap.parse_args()
    cfg = load_config(args.config)
    cfg["whisper"]["prompt"] = cfg["whisper"].get("prompt", "")

    if args.file:
        w = Whisper(cfg["whisper"])
        w.start()
        try:
            print(process(args.file, cfg, w))
        finally:
            w.stop()
        return
    Daemon(cfg).run()


if __name__ == "__main__":
    main()
