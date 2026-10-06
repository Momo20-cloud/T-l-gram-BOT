"""
Bande-son de la vidéo de présentation, générée par programme (aucun droit d'auteur).
Ambiance électronique sobre à 100 BPM, avec des transitions calées sur les scènes.
Usage : python soundtrack.py sortie.wav [durée_en_secondes]
"""
import sys
import wave

import numpy as np

SR = 44100
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 31.0
BPM = 100
BEAT = 60 / BPM
SCENES = [3.2, 8.55, 14.2, 18.8, 22.8, 26.3]      # changements de scène (s)
N = int(SR * DUR)
t = np.arange(N) / SR
rng = np.random.default_rng(7)


def note(n):            # numéro MIDI -> fréquence
    return 440.0 * 2 ** ((n - 69) / 12)


def lowpass(x, cutoff):
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc = (1 - a) * v + a * acc
        y[i] = acc
    return y


def env(length, attack, release):
    e = np.ones(length)
    na, nr = int(attack * SR), int(release * SR)
    if na:
        e[:na] = np.linspace(0, 1, na)
    if nr:
        e[-nr:] *= np.linspace(1, 0, nr)
    return e


out = np.zeros(N)

# --- nappe d'accords (Am - F - C - G), 2 mesures chacun
chords = [[57, 60, 64], [53, 57, 60], [48, 55, 60], [55, 59, 62]]
bar = 4 * BEAT
pad = np.zeros(N)
k = 0
start = 0.0
while start < DUR:
    seg = min(2 * bar, DUR - start)
    i0, n = int(start * SR), int(seg * SR)
    tt = np.arange(n) / SR
    s = np.zeros(n)
    for m in chords[k % 4]:
        for det in (-0.12, 0.12):
            f = note(m) * 2 ** (det / 12)
            s += 2 * ((tt * f) % 1) - 1          # dent de scie légèrement désaccordée
    s *= env(n, 0.35, 0.35)
    pad[i0:i0 + n] += s[: N - i0]
    start += 2 * bar
    k += 1
pad = lowpass(pad, 900) * 0.055
out += pad

# --- basse + grosse caisse, à partir de la scène 2
def kick(at, gain=1.0):
    n = int(0.45 * SR)
    i0 = int(at * SR)
    if i0 >= N:
        return
    tt = np.arange(min(n, N - i0)) / SR
    f = 45 + 75 * np.exp(-tt * 28)
    ph = 2 * np.pi * np.cumsum(f) / SR
    out[i0:i0 + len(tt)] += np.sin(ph) * np.exp(-tt * 7) * 0.55 * gain


def hat(at, gain=1.0):
    n = int(0.06 * SR)
    i0 = int(at * SR)
    if i0 >= N:
        return
    m = min(n, N - i0)
    noise = rng.standard_normal(m)
    noise = noise - lowpass(noise, 6000)          # garde les aigus
    out[i0:i0 + m] += noise * np.exp(-np.arange(m) / SR * 60) * 0.10 * gain


beat_t = SCENES[0]
while beat_t < DUR - 1.2:
    kick(beat_t)
    if beat_t >= SCENES[1]:
        hat(beat_t + BEAT / 2)
    beat_t += BEAT

bass = np.zeros(N)
k, start = 0, 0.0
while start < DUR:
    seg = min(2 * bar, DUR - start)
    i0, n = int(start * SR), int(seg * SR)
    if start + seg > SCENES[0]:
        tt = np.arange(n) / SR
        root = note(chords[k % 4][0] - 12)
        pulse = np.exp(-((tt % BEAT) / BEAT) * 3)          # pompe au rythme
        s = np.sin(2 * np.pi * root * tt) * pulse * env(n, 0.02, 0.2)
        s[: max(0, int((SCENES[0] - start) * SR))] = 0
        bass[i0:i0 + n] += s[: N - i0]
    start += 2 * bar
    k += 1
out += bass * 0.22

# --- transitions : souffle montant avant chaque scène, impact final
for sc in SCENES:
    n = int(0.7 * SR)
    i0 = int((sc - 0.6) * SR)
    if i0 < 0 or i0 + n > N:
        continue
    noise = rng.standard_normal(n)
    sweep = lowpass(noise, 2500) * np.linspace(0, 1, n) ** 2 * np.concatenate([np.ones(n - int(.12 * SR)), np.linspace(1, 0, int(.12 * SR))])
    out[i0:i0 + n] += sweep * 0.16
kick(SCENES[-1], gain=1.6)

# --- arrangement : entrée douce, fin en fondu
fade_in = np.clip(t / 1.2, 0, 1)
fade_out = np.clip((DUR - t) / 2.0, 0, 1)
out *= fade_in * fade_out
out = np.tanh(out * 1.4) / np.tanh(1.4)                     # compression douce
out *= 0.89 / max(1e-9, np.max(np.abs(out)))               # ≈ -1 dBFS

stereo = np.stack([out, out], axis=1)
pcm = (stereo * 32767).astype(np.int16)
with wave.open(sys.argv[1] if len(sys.argv) > 1 else "soundtrack.wav", "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print("ok", DUR, "s")
