"""Synthesize the reels' music bed: a soft tanpura-style drone (Pa Sa Sa Sa, low Sa), made from scratch,
so it is the project's own sound with no licence to track. Run once; writes src/tgnews/assets/reel/bed.m4a."""
import subprocess, sys, wave
from pathlib import Path
import numpy as np

SR = 44100
SA = 130.81  # C3: a comfortable drone for a male Telugu voice
PLUCK = 0.62  # seconds between plucks; four plucks a cycle
CYCLES = 8


def pluck(freq: float, seconds: float = 3.2) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    out = np.zeros_like(t)
    for h in range(1, 14):  # jawari: a long, buzzy row of overtones that decays slowly
        amp = (1 / h ** 0.9) * (1.0 if h < 6 else 0.6)
        detune = 1 + 0.0007 * np.sin(2 * np.pi * 0.3 * t + h)
        out += amp * np.sin(2 * np.pi * freq * h * detune * t) * np.exp(-t * (0.55 + 0.11 * h))
    return out * np.minimum(1, t / 0.015)


def main(out: Path) -> None:
    notes = [SA * 1.5 / 2, SA, SA, SA / 2]  # low Pa, Sa, Sa, low Sa
    total = int(SR * PLUCK * 4 * CYCLES + SR * 3.2)
    mix = np.zeros(total)
    for c in range(CYCLES):
        for i, f in enumerate(notes):
            start = int(SR * PLUCK * (c * 4 + i))
            p = pluck(f)
            mix[start:start + len(p)] += p
    loop = int(SR * PLUCK * 4 * CYCLES)
    mix[:total - loop] += mix[loop:]  # fold the tail back in so the loop is seamless
    mix = mix[:loop]
    mix = mix / np.max(np.abs(mix)) * 0.5
    wav = out.with_suffix(".wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((mix * 32767).astype("<i2").tobytes())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-af", "lowpass=f=5000",
                    "-c:a", "aac", "-b:a", "96k", str(out)], check=True)
    wav.unlink()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("src/tgnews/assets/reel/bed.m4a"))
