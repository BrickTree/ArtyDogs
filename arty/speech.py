"""Spoken callouts through Windows' own offline voice.

A hidden Windows PowerShell process hosts System.Speech (part of every Windows
10/11) and reads one command per line from us, so there is nothing to install
and a callout starts within a few milliseconds once the voice is warm. Each new
callout cuts off the previous one: a stale firing solution is worse than none.
"""
from __future__ import annotations

import base64
import queue
import subprocess
import threading

_HOST = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToDefaultAudioDevice()
while (($line = [Console]::In.ReadLine()) -ne $null) {
  if ($line.StartsWith('VOL ')) { $s.Volume = [Math]::Max(0, [Math]::Min(100, [int]$line.Substring(4))); continue }
  if ($line.StartsWith('RATE ')) { $s.Rate = [Math]::Max(-10, [Math]::Min(10, [int]$line.Substring(5))); continue }
  if ($line -eq 'STOP') { $s.SpeakAsyncCancelAll(); continue }
  if ($line.StartsWith('SAY ')) { $s.SpeakAsyncCancelAll(); [void]$s.SpeakAsync($line.Substring(4)) }
}
"""

_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
                "7": "seven", "8": "eight", "9": "niner", ".": "point", "-": "minus"}


def digits(text: str) -> str:
    """Radio style, one digit at a time: "1219" -> "one two one niner"."""
    return " ".join(_DIGIT_WORDS[c] for c in text if c in _DIGIT_WORDS)


class Speaker:
    def __init__(self, volume: int = 80, rate: int = 1) -> None:
        self.volume = max(0, min(100, int(volume)))
        self.rate = max(-10, min(10, int(rate)))
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._commands: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="voice")

    def start(self) -> None:
        """Start the voice in the background; the first callout is then instant."""
        self._thread.start()

    def _launch(self) -> bool:
        try:
            script = base64.b64encode(_HOST.encode("utf-16-le")).decode("ascii")
            self._proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", script],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, encoding="utf-8")
            self._write(f"VOL {self.volume}")
            self._write(f"RATE {self.rate}")
            self.error = None
            return True
        except OSError as e:
            self.error = f"Windows speech isn't available ({e})"
            self._proc = None
            return False

    def _write(self, line: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(line.replace("\n", " ").replace("\r", " ") + "\n")
        self._proc.stdin.flush()

    def _run(self) -> None:
        self._launch()
        while (line := self._commands.get()) is not None:
            if self._proc is None or self._proc.poll() is not None:
                if not self._launch():
                    continue
            try:
                self._write(line)
            except (OSError, ValueError):  # the voice process died; relaunch on the next command
                self._proc = None
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.close()  # the host loop ends on end-of-input
                self._proc.wait(timeout=1.5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()

    def say(self, text: str) -> None:
        if self.volume > 0 and text:
            self._commands.put(f"SAY {text}")

    def stop(self) -> None:
        self._commands.put("STOP")

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        self._commands.put(f"VOL {self.volume}")
        if self.volume == 0:
            self._commands.put("STOP")

    def set_rate(self, rate: int) -> None:
        self.rate = max(-10, min(10, int(rate)))
        self._commands.put(f"RATE {self.rate}")

    def close(self) -> None:
        self._commands.put(None)
