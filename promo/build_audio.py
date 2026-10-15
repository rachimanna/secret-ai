"""Voice-over (Piper), the timeline for the video and the full soundtrack: original beat, whooshes, pops, impacts."""
import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
SR = 44100
TTS = HERE / "tts"
LINES = [  # scene, what is said, what the captions show
    ("s1", "Стоп! В твоём Телеграме может жить своя нейросеть.", "Стоп! В твоём Телеграме может жить своя нейросеть"),
    ("s2", "Знакомься: Сикрет Эй Ай.", "Знакомься: Secret AI"),
    ("s3", "Пишешь как другу — он сразу отвечает.", "Пишешь как другу — он сразу отвечает"),
    ("s4", "Нарисует что угодно за десять секунд.", "Нарисует что угодно за 10 секунд"),
    ("s5", "Снимет видео с озвучкой.", "Снимет видео с озвучкой"),
    ("s6", "Ответит голосом.", "Ответит голосом"),
    ("s7", "А пока ты спишь, ответит за тебя!", "А пока ты спишь, ответит за тебя!"),
    ("s8", "В группах: викторины и мемы.", "В группах: викторины и мемы"),
    ("s9", "Плюс две с половиной тысячи приложений!", "Плюс 2500 приложений!"),
    ("s10", "Сикрет Эй Ай — бесплатно. Ссылка в профиле!", "Secret AI — бесплатно. Ссылка в профиле!"),
]
MIN_SCENE = {"s1": 2.6, "s2": 2.1, "s3": 2.5, "s4": 2.6, "s5": 2.4, "s6": 2.1, "s7": 2.9, "s8": 2.8, "s9": 2.8, "s10": 3.8}
LEAD, TAIL = 0.12, 0.22  # silence before and after each line inside its scene


def read_wav(path):
    with wave.open(str(path)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        rate = w.getframerate()
    if rate != SR:  # simple resample
        x = np.linspace(0, len(data) - 1, int(len(data) * SR / rate))
        data = np.interp(x, np.arange(len(data)), data)
    return data


def trim(x, threshold=0.012):
    idx = np.where(np.abs(x) > threshold)[0]
    return x[max(0, idx[0] - 600): idx[-1] + 1200] if len(idx) else x


def speak(text, path):
    subprocess.run([str(TTS / "piper" / "piper"), "-m", str(TTS / "ru-irinia-medium.onnx"), "-f", str(path),
                    "--length_scale", "0.8", "--sentence_silence", "0.12"], input=text.encode(), check=True,
                   capture_output=True)
    return trim(read_wav(path))


def weights(word):
    return len(re.sub(r"[^\wё]", "", word, flags=re.I)) + 2


# ---------- voice and timeline ----------
voice_parts, scenes, words, t = [], [], [], 0.0
for group, (scene, say, cap) in enumerate(LINES):
    audio = speak(say, HERE / f"line_{scene}.wav")
    speech = len(audio) / SR
    length = max(MIN_SCENE[scene], LEAD + speech + TAIL)
    start = t
    voice_parts.append((start + LEAD, audio))
    caps = cap.split()
    total_w = sum(weights(w) for w in caps)
    pos = start + LEAD
    for w in caps:
        share = speech * weights(w) / total_w
        words.append({"w": w, "s": round(pos, 3), "e": round(pos + share, 3), "g": group})
        pos += share
    scenes.append({"id": scene, "start": round(start, 3), "end": round(start + length, 3)})
    t += length
TOTAL = round(t + 0.6, 3)
# word index of a group start, for captions chunking
first = {}
for i, w in enumerate(words):
    first.setdefault(w["g"], i)
for w in words:
    w["g0"] = first[w["g"]]

# ---------- music: an original 124 bpm beat ----------
n = int(TOTAL * SR)
mix_music = np.zeros(n)
bpm, beat = 124, 60 / 124
tt = np.arange(n) / SR
rng = np.random.default_rng(7)


def add(buf, at, sig, gain=1.0):
    i = int(at * SR)
    if i >= len(buf):
        return
    end = min(len(buf), i + len(sig))
    buf[i:end] += sig[:end - i] * gain


def env(length, attack=0.002, decay=0.2):
    x = np.arange(int(length * SR)) / SR
    return np.minimum(1, x / attack) * np.exp(-x / decay)


def kick():
    x = np.arange(int(0.45 * SR)) / SR
    freq = 50 + 110 * np.exp(-x * 30)
    return np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-x * 7) * 0.95


def clap():
    x = np.arange(int(0.25 * SR)) / SR
    noise = rng.normal(0, 1, len(x))
    band = noise - np.concatenate([[0], noise[:-1]])  # bright
    return band * (np.exp(-x * 22) + 0.4 * np.exp(-((x - .012) ** 2) / 1e-5)) * 0.35


def hat(open_=False):
    x = np.arange(int((0.18 if open_ else 0.05) * SR)) / SR
    noise = rng.normal(0, 1, len(x))
    hp = noise - np.concatenate([[0], noise[:-1]])
    return hp * np.exp(-x * (18 if open_ else 70)) * 0.12


def tone(freq, length, kind="saw", decay=0.25, attack=0.004):
    x = np.arange(int(length * SR)) / SR
    if kind == "saw":
        sig = sum(np.sin(2 * np.pi * freq * k * x) / k for k in range(1, 9))
    elif kind == "square":
        sig = sum(np.sin(2 * np.pi * freq * k * x) / k for k in range(1, 9, 2))
    else:
        sig = np.sin(2 * np.pi * freq * x)
    return sig * np.minimum(1, x / attack) * np.exp(-x / decay)


notes = {"A": 55.0, "F": 43.65, "C": 65.41, "G": 49.0}
prog = ["A", "F", "C", "G"]  # Am F C G
chords = {"A": [220, 261.63, 329.63], "F": [174.61, 220, 261.63], "C": [196, 261.63, 329.63], "G": [196, 246.94, 293.66]}
start_beat = scenes[0]["end"]  # hook: impact and riser only, the beat drops after "Stop"
bars = int((TOTAL - start_beat) / (4 * beat)) + 1
for bar in range(bars):
    root = prog[bar % 4]
    for b in range(4):
        at = start_beat + (bar * 4 + b) * beat
        if at > TOTAL - 0.8:
            break
        add(mix_music, at, kick(), 0.9)
        if b in (1, 3):
            add(mix_music, at, clap(), 1.0)
        for half in (0, 0.5):
            add(mix_music, at + half * beat, hat(open_=half == 0.5 and b == 3), 1.0)
        for eighth in (0, 0.5):  # rolling sub bass, ducked by the kick
            add(mix_music, at + eighth * beat + 0.06, tone(notes[root], beat * 0.45, "sine", 0.18), 0.55)
            add(mix_music, at + eighth * beat + 0.06, tone(notes[root] * 2, beat * 0.45, "saw", 0.08), 0.06)
        if b == 0:  # pluck chord stabs
            for k, f in enumerate(chords[root]):
                for step in (0, 0.75, 1.5, 2.5):
                    add(mix_music, at + step * beat + k * 0.004, tone(f * 2, 0.3, "square", 0.09), 0.035)
# hook: riser into a sub drop
riser_len = scenes[0]["end"]
x = np.arange(int(riser_len * SR)) / SR
riser = rng.normal(0, 1, len(x)) * (x / riser_len) ** 2 * 0.18
riser += np.sin(2 * np.pi * np.cumsum(200 + 1400 * (x / riser_len) ** 2) / SR) * (x / riser_len) ** 2 * 0.05
add(mix_music, 0, riser)
xs = np.arange(int(1.2 * SR)) / SR
impact = np.sin(2 * np.pi * np.cumsum(70 * np.exp(-xs * 2) + 30) / SR) * np.exp(-xs * 3) * 0.9 + rng.normal(0, 1, len(xs)) * np.exp(-xs * 20) * 0.3
add(mix_music, 0.0, impact)
add(mix_music, start_beat, impact, 0.8)

# ---------- sound effects ----------
sfx = np.zeros(n)


def whoosh(length=0.45):
    x = np.arange(int(length * SR)) / SR
    noise = rng.normal(0, 1, len(x))
    out, y = np.zeros(len(x)), 0.0
    for i in range(len(x)):  # sweeping low-pass
        a = 0.02 + 0.5 * np.sin(np.pi * x[i] / length) ** 2
        y += a * (noise[i] - y)
        out[i] = y
    return out * np.sin(np.pi * x / length) ** 1.5 * 0.5


def pop(freq=900):
    x = np.arange(int(0.12 * SR)) / SR
    return np.sin(2 * np.pi * np.cumsum(freq * (1 + 1.2 * np.exp(-x * 40))) / SR) * np.exp(-x * 38) * 0.35


def click():
    x = np.arange(int(0.03 * SR)) / SR
    return rng.normal(0, 1, len(x)) * np.exp(-x * 300) * 0.5 + np.sin(2 * np.pi * 2400 * x) * np.exp(-x * 200) * 0.3


woosh = whoosh()
for s in scenes[1:]:
    add(sfx, s["start"] - 0.2, woosh, 0.9)
POPS = {"s2": [.35, .75], "s3": [.3, 1.05], "s4": [.05, .25, .75], "s5": [.05, .25], "s6": [.05, .25, .85],
        "s7": [.2, .5, 1.35], "s8": [.05, .2, .75], "s9": [.05, .3] + [.4 + i * .09 for i in range(8)],
        "s10": [.3, .65, 1.05]}
for s in scenes:
    for k, d in enumerate(POPS.get(s["id"], [])):
        add(sfx, s["start"] + d, pop(700 + 120 * (k % 4)), 0.9)
add(sfx, scenes[-1]["start"] + 0.62, click(), 1.4)
for d in (1.6, 2.4):  # button presses at the end
    add(sfx, scenes[-1]["start"] + d, click(), 1.2)

# ---------- voice track and mix ----------
voice = np.zeros(n)
for at, audio in voice_parts:
    add(voice, at, audio * 1.0)
# duck the music under the voice
level = np.abs(voice)
win = int(0.15 * SR)
smooth = np.convolve(level, np.ones(win) / win, mode="same")
duck = 1 - 0.55 * np.clip(smooth * 12, 0, 1)
music = mix_music * duck * 0.55
mix = voice * 1.15 + music + sfx * 0.7
fade = int(0.6 * SR)
mix[-fade:] *= np.linspace(1, 0, fade)
mix = np.tanh(mix * 1.1) * 0.92  # soft limiter
stereo = np.stack([mix, mix], axis=1)
with wave.open(str(HERE / "mix.wav"), "wb") as w:
    w.setnchannels(2), w.setsampwidth(2), w.setframerate(SR)
    w.writeframes((np.clip(stereo, -1, 1) * 32767).astype(np.int16).tobytes())
json.dump({"scenes": scenes, "words": words, "total": TOTAL, "vidframes": 540}, open(HERE / "timeline.json", "w"),
          ensure_ascii=False, indent=1)
print("total", TOTAL, "scenes", [(s["id"], round(s["end"] - s["start"], 2)) for s in scenes])
