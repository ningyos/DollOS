# Wake Word Model Retrain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retrain gura.onnx wake word model with proper Japanese TTS data and negative samples so it detects "グラ" (gura) without false-triggering on other speech.

**Architecture:** Use fish-tts (native Japanese TTS) to generate diverse positive/adversarial samples, OWW's augmentation pipeline for data augmentation, OWW's pre-computed ACAV100M embeddings (2000 hrs) as negative data, and OWW's DNN training script to train and export ONNX model.

**Tech Stack:** fish-tts (Japanese TTS), openWakeWord (training pipeline), PyTorch (model), ONNX Runtime (inference), Python 3.12

**Working directory:** `/home/progcat/Projects/DollOS/wake_word_training/`

---

### Task 1: Set Up Training Environment

**Files:**
- Create: `wake_word_training/setup.sh`
- Create: `wake_word_training/gura_config.yaml`

- [ ] **Step 1: Create working directory**

```bash
mkdir -p ~/Projects/DollOS/wake_word_training
cd ~/Projects/DollOS/wake_word_training
```

- [ ] **Step 2: Create a venv with required dependencies**

```bash
python3 -m venv venv
source venv/bin/activate
pip install torch torchaudio torchinfo torchmetrics audiomentations torch-audiomentations speechbrain acoustics onnxruntime numpy scipy tqdm pyyaml mutagen
pip install -e /tmp/openWakeWord
pip install -e ~/Projects/fish-tts
```

- [ ] **Step 3: Download pre-computed negative features (ACAV100M)**

```bash
cd ~/Projects/DollOS/wake_word_training
# 2000 hours of pre-computed OWW embeddings (~2GB)
wget -c https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy
# Validation set (~11 hours)
wget -c https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy
```

- [ ] **Step 4: Download Room Impulse Responses (MIT)**

```bash
cd ~/Projects/DollOS/wake_word_training
source venv/bin/activate
python3 -c "
import os, scipy.io.wavfile, numpy as np
from datasets import load_dataset
from tqdm import tqdm

output_dir = './mit_rirs'
os.makedirs(output_dir, exist_ok=True)
rir_dataset = load_dataset('davidscripka/MIT_environmental_impulse_responses', split='train', streaming=True)
for row in tqdm(rir_dataset):
    name = row['audio']['path'].split('/')[-1]
    scipy.io.wavfile.write(os.path.join(output_dir, name), 16000, (row['audio']['array']*32767).astype(np.int16))
print('Done')
"
```

- [ ] **Step 5: Download background noise (AudioSet + FMA)**

```bash
cd ~/Projects/DollOS/wake_word_training
source venv/bin/activate
python3 -c "
import os, scipy.io.wavfile, numpy as np
from datasets import load_dataset, Audio
from tqdm import tqdm
from pathlib import Path

# AudioSet (1 tar file)
os.makedirs('audioset_16k', exist_ok=True)
ds = load_dataset('agkphysics/AudioSet', split='train', streaming=True)
ds = ds.cast_column('audio', Audio(sampling_rate=16000))
for i, row in enumerate(tqdm(ds, total=3000)):
    name = f'audioset_{i:05d}.wav'
    audio = row['audio']['array']
    scipy.io.wavfile.write(f'audioset_16k/{name}', 16000, (audio*32767).astype(np.int16))
    if i >= 3000:
        break

# FMA (1 hour of music)
os.makedirs('fma', exist_ok=True)
fma = load_dataset('rudraml/fma', name='small', split='train', streaming=True)
fma = iter(fma.cast_column('audio', Audio(sampling_rate=16000)))
for i in tqdm(range(120)):  # 120 clips * 30s = 1 hour
    row = next(fma)
    name = f'fma_{i:04d}.wav'
    scipy.io.wavfile.write(f'fma/{name}', 16000, (row['audio']['array']*32767).astype(np.int16))
print('Done')
"
```

- [ ] **Step 6: Verify all data downloaded**

```bash
ls -lh openwakeword_features_ACAV100M_2000_hrs_16bit.npy  # ~2GB
ls -lh validation_set_features.npy                          # ~100MB
ls mit_rirs/ | wc -l                                        # ~270 files
ls audioset_16k/ | wc -l                                    # ~3000 files
ls fma/ | wc -l                                             # ~120 files
```

---

### Task 2: Generate Positive Samples with fish-tts

**Files:**
- Create: `wake_word_training/generate_positive.py`

- [ ] **Step 1: Write positive sample generation script**

```python
#!/usr/bin/env python3
"""Generate positive 'gura' wake word samples using fish-tts."""

import os
import io
import wave
import numpy as np
import scipy.io.wavfile
from pathlib import Path

OUTPUT_DIR = Path("gura_model/positive_train")
OUTPUT_DIR_TEST = Path("gura_model/positive_test")
TARGET_SR = 16000
N_TRAIN = 3000
N_TEST = 500

# Variations of "gura" to synthesize
POSITIVE_TEXTS = [
    "グラ",
    "ぐら",
    "グラー",     # elongated
    "ぐらぁ",    # elongated casual
    "グラッ",     # clipped
]


def wav_bytes_to_int16(wav_bytes: bytes, target_sr: int = 16000) -> np.ndarray:
    """Convert WAV bytes to 16kHz int16 numpy array."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    # Resample to 16kHz if needed
    if sr != target_sr:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=target_sr)

    return (audio * 32767).astype(np.int16)


def generate_samples(synth, n_samples: int, output_dir: Path):
    """Generate n_samples WAV files of 'gura' with variation."""
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(output_dir.glob("*.wav")))
    if existing >= n_samples:
        print(f"  Already have {existing}/{n_samples} samples, skipping")
        return

    temperatures = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    top_ps = [0.7, 0.8, 0.9]

    for i in range(existing, n_samples):
        text = POSITIVE_TEXTS[i % len(POSITIVE_TEXTS)]
        temp = temperatures[i % len(temperatures)]
        top_p = top_ps[i % len(top_ps)]

        try:
            wav_bytes = synth.synthesize(
                text,
                temperature=temp,
                top_p=top_p,
            )
            audio_int16 = wav_bytes_to_int16(wav_bytes, TARGET_SR)

            # Skip if too short (<0.2s) or too long (>2s)
            duration = len(audio_int16) / TARGET_SR
            if duration < 0.2 or duration > 2.0:
                continue

            out_path = output_dir / f"gura_{i:05d}.wav"
            scipy.io.wavfile.write(str(out_path), TARGET_SR, audio_int16)

            if i % 100 == 0:
                print(f"  [{i}/{n_samples}] {text} temp={temp} dur={duration:.2f}s")
        except Exception as e:
            print(f"  Error at {i}: {e}")
            continue

    print(f"  Generated {len(list(output_dir.glob('*.wav')))} samples in {output_dir}")


def main():
    from fish_tts import get_instance, VoiceProfile

    print("Loading fish-tts...")
    synth = get_instance(device="cuda", precision="bf16")

    # Load gura voice profile for voice cloning
    gura_voice_path = os.path.expanduser("~/Projects/fish-tts/gura_voice.npy")
    if os.path.exists(gura_voice_path):
        profile = VoiceProfile.load(gura_voice_path, text="")
        synth.set_references([profile])
        print("Using gura voice profile for cloning")
    else:
        print("No voice profile found, using default voice")

    print(f"Generating {N_TRAIN} training samples...")
    generate_samples(synth, N_TRAIN, OUTPUT_DIR)

    print(f"Generating {N_TEST} test samples...")
    generate_samples(synth, N_TEST, OUTPUT_DIR_TEST)

    print("Done!")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run positive sample generation**

```bash
cd ~/Projects/DollOS/wake_word_training
source venv/bin/activate
python3 generate_positive.py
```

Expected: `gura_model/positive_train/` with ~3000 WAV files, `gura_model/positive_test/` with ~500 WAV files.

- [ ] **Step 3: Verify samples**

```bash
ls gura_model/positive_train/ | wc -l   # ~3000
ls gura_model/positive_test/ | wc -l    # ~500
# Spot check a few files
python3 -c "
import scipy.io.wavfile
sr, data = scipy.io.wavfile.read('gura_model/positive_train/gura_00000.wav')
print(f'SR={sr}, duration={len(data)/sr:.2f}s, dtype={data.dtype}')
"
```

Expected: SR=16000, duration 0.3-1.5s, dtype int16

---

### Task 3: Generate Adversarial Negative Samples

**Files:**
- Create: `wake_word_training/generate_negative.py`

- [ ] **Step 1: Write adversarial negative generation script**

```python
#!/usr/bin/env python3
"""Generate adversarial negative samples — Japanese words similar to 'gura'."""

import os
import io
import wave
import numpy as np
import scipy.io.wavfile
from pathlib import Path

OUTPUT_DIR = Path("gura_model/negative_train")
OUTPUT_DIR_TEST = Path("gura_model/negative_test")
TARGET_SR = 16000
N_TRAIN = 3000
N_TEST = 500

# Japanese words/syllables that sound similar to "gura" or could confuse the model
ADVERSARIAL_TEXTS = [
    # Similar-sounding Japanese words
    "クラ",       # kura
    "グル",       # guru
    "ムラ",       # mura
    "スラ",       # sura
    "フラ",       # fura
    "ブラ",       # bura
    "プラ",       # pura
    "ツラ",       # tsura
    "クラス",     # kurasu (class)
    "グルメ",     # gurume (gourmet)
    "グループ",   # guruupu (group)
    "グリ",       # guri
    "グレ",       # gure
    "グロ",       # guro
    # Common short Japanese words (general false positive sources)
    "ああ",       # aa
    "うん",       # un
    "ええ",       # ee
    "おい",       # oi
    "ねえ",       # nee
    "はい",       # hai
    "いいえ",     # iie
    "すみません",  # sumimasen
    "ありがとう",  # arigatou
    "おはよう",    # ohayou
    "こんにちは",  # konnichiwa
    # Chinese words (user speaks Chinese too)
    "好的",
    "嗯",
    "喂",
    "你好",
    "是的",
    "不是",
    "什麼",
    "怎麼",
]


def wav_bytes_to_int16(wav_bytes: bytes, target_sr: int = 16000) -> np.ndarray:
    """Convert WAV bytes to 16kHz int16 numpy array."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    if sr != target_sr:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=target_sr)

    return (audio * 32767).astype(np.int16)


def generate_samples(synth, n_samples: int, output_dir: Path):
    """Generate adversarial negative samples."""
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(output_dir.glob("*.wav")))
    if existing >= n_samples:
        print(f"  Already have {existing}/{n_samples} samples, skipping")
        return

    temperatures = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    top_ps = [0.7, 0.8, 0.9]

    for i in range(existing, n_samples):
        text = ADVERSARIAL_TEXTS[i % len(ADVERSARIAL_TEXTS)]
        temp = temperatures[i % len(temperatures)]
        top_p = top_ps[i % len(top_ps)]

        try:
            wav_bytes = synth.synthesize(
                text,
                temperature=temp,
                top_p=top_p,
            )
            audio_int16 = wav_bytes_to_int16(wav_bytes, TARGET_SR)

            duration = len(audio_int16) / TARGET_SR
            if duration < 0.1 or duration > 3.0:
                continue

            out_path = output_dir / f"neg_{i:05d}.wav"
            scipy.io.wavfile.write(str(out_path), TARGET_SR, audio_int16)

            if i % 100 == 0:
                print(f"  [{i}/{n_samples}] {text} temp={temp} dur={duration:.2f}s")
        except Exception as e:
            print(f"  Error at {i}: {e}")
            continue

    print(f"  Generated {len(list(output_dir.glob('*.wav')))} samples in {output_dir}")


def main():
    from fish_tts import get_instance, VoiceProfile

    print("Loading fish-tts...")
    synth = get_instance(device="cuda", precision="bf16")

    # Use gura voice for adversarial samples too (same voice saying wrong words)
    gura_voice_path = os.path.expanduser("~/Projects/fish-tts/gura_voice.npy")
    if os.path.exists(gura_voice_path):
        profile = VoiceProfile.load(gura_voice_path, text="")
        synth.set_references([profile])
        print("Using gura voice profile")

    print(f"Generating {N_TRAIN} adversarial training samples...")
    generate_samples(synth, N_TRAIN, OUTPUT_DIR)

    print(f"Generating {N_TEST} adversarial test samples...")
    generate_samples(synth, N_TEST, OUTPUT_DIR_TEST)

    print("Done!")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run adversarial sample generation**

```bash
cd ~/Projects/DollOS/wake_word_training
source venv/bin/activate
python3 generate_negative.py
```

- [ ] **Step 3: Verify directory structure**

```bash
find gura_model/ -type f -name "*.wav" | wc -l  # ~7000 total
ls gura_model/positive_train/ | wc -l  # ~3000
ls gura_model/positive_test/ | wc -l   # ~500
ls gura_model/negative_train/ | wc -l  # ~3000
ls gura_model/negative_test/ | wc -l   # ~500
```

---

### Task 4: Create Training Config and Run Training

**Files:**
- Create: `wake_word_training/gura_config.yaml`

- [ ] **Step 1: Write training config**

```yaml
# gura_config.yaml — openWakeWord training config for Japanese "gura" wake word

model_name: "gura"

target_phrase:
  - "gura"

custom_negative_phrases: []

n_samples: 3000
n_samples_val: 500

tts_batch_size: 50
augmentation_batch_size: 16

piper_sample_generator_path: "./piper-sample-generator"

output_dir: "./gura_model"

rir_paths:
  - "./mit_rirs"

background_paths:
  - "./audioset_16k"
  - "./fma"

background_paths_duplication_rate:
  - 1
  - 1

false_positive_validation_data_path: "./validation_set_features.npy"

augmentation_rounds: 2

feature_data_files:
  "ACAV100M_sample": "./openwakeword_features_ACAV100M_2000_hrs_16bit.npy"

batch_n_per_class:
  "ACAV100M_sample": 1024
  "adversarial_negative": 50
  "positive": 50

model_type: "dnn"
layer_size: 64

steps: 50000

max_negative_weight: 1500
target_false_positives_per_hour: 0.2
```

Note: `layer_size: 64` (increased from default 32 for better discrimination of a short word).

- [ ] **Step 2: Run augmentation (skip generate_clips since we provided our own)**

```bash
cd ~/Projects/DollOS/wake_word_training
source venv/bin/activate
python3 /tmp/openWakeWord/openwakeword/train.py \
    --training_config gura_config.yaml \
    --augment_clips
```

Expected: Creates `gura_model/positive_features_train.npy`, `negative_features_train.npy`, etc.

- [ ] **Step 3: Verify feature files**

```bash
python3 -c "
import numpy as np
for name in ['positive_features_train', 'positive_features_test', 'negative_features_train', 'negative_features_test']:
    f = f'gura_model/{name}.npy'
    arr = np.load(f, mmap_mode='r')
    print(f'{name}: shape={arr.shape}, dtype={arr.dtype}')
"
```

Expected: shape=(N, frames, 96), dtype=float32

- [ ] **Step 4: Run model training**

```bash
cd ~/Projects/DollOS/wake_word_training
source venv/bin/activate
python3 /tmp/openWakeWord/openwakeword/train.py \
    --training_config gura_config.yaml \
    --train_model
```

Expected: Training runs for up to 50000 steps. Outputs `gura_model/gura.onnx`.

- [ ] **Step 5: Verify trained model**

```bash
python3 -c "
import onnxruntime as ort
import numpy as np

session = ort.InferenceSession('gura_model/gura.onnx')
for inp in session.get_inputs():
    print(f'Input: {inp.name}, shape: {inp.shape}, type: {inp.type}')
for out in session.get_outputs():
    print(f'Output: {out.name}, shape: {out.shape}, type: {out.type}')

# Test with zeros (should be low)
x = np.zeros((1, 16, 96), dtype=np.float32)
r = session.run(None, {session.get_inputs()[0].name: x})
print(f'Score (zeros): {r[0][0][0]:.6f}')

# Test with random (should be moderate, not saturated)
x = np.random.randn(1, 16, 96).astype(np.float32) * 20
r = session.run(None, {session.get_inputs()[0].name: x})
print(f'Score (random): {r[0][0][0]:.6f}')
"
```

Expected: Input shape [batch, 16, 96], output [batch, 1]. Zeros score < 0.3, random score 0.1-0.6.

---

### Task 5: Test Model with Live Audio Pipeline

**Files:**
- Create: `wake_word_training/test_live.py`

- [ ] **Step 1: Write live microphone test script**

```python
#!/usr/bin/env python3
"""Test the trained gura.onnx model with live microphone input using OWW streaming."""

import sys
sys.path.insert(0, "/tmp/openWakeWord")
import openwakeword
from openwakeword.model import Model
import pyaudio
import numpy as np

CHUNK = 1280  # 80ms at 16kHz
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000

print("Loading model...")
oww_model = Model(
    wakeword_model_paths=["gura_model/gura.onnx"],
    inference_framework="onnx"
)
print("Ready! Speak 'gura' or other words. Ctrl+C to quit.\n")

p = pyaudio.PyInterface()
stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                input=True, frames_per_buffer=CHUNK)

try:
    while True:
        audio = np.frombuffer(stream.read(CHUNK), dtype=np.int16)
        prediction = oww_model.predict(audio)
        for key, score in prediction.items():
            if score > 0.1:
                marker = " <<<< DETECTED!" if score > 0.5 else ""
                print(f"  {key}: {score:.4f}{marker}")
except KeyboardInterrupt:
    print("\nStopped.")
finally:
    stream.stop_stream()
    stream.close()
    p.terminate()
```

- [ ] **Step 2: Run live test**

```bash
cd ~/Projects/DollOS/wake_word_training
source venv/bin/activate
pip install pyaudio
python3 test_live.py
```

Test by saying "gura" and other words. Check:
- "gura" → score > 0.5 (should trigger)
- Other words → score < 0.3 (should NOT trigger)
- Silence → score < 0.1

If false positive rate is too high, increase `target_false_positives_per_hour` in config and retrain.

---

### Task 6: Deploy to Device

**Files:**
- Modify: `~/Projects/DollOS/gura.onnx`

- [ ] **Step 1: Copy trained model to DollOS repo**

```bash
cp ~/Projects/DollOS/wake_word_training/gura_model/gura.onnx ~/Projects/DollOS/gura.onnx
```

- [ ] **Step 2: Push to device**

```bash
export ADB=~/Android/Sdk/platform-tools/adb

# Push gura.onnx as the character's wake_word.onnx
CHARACTER_ID=$($ADB shell "ls /data/user/0/org.dollos.ai/files/characters/" | head -1 | tr -d '\r')
$ADB push ~/Projects/DollOS/gura.onnx "/data/user/0/org.dollos.ai/files/characters/${CHARACTER_ID}/wake_word.onnx"

# Also ensure correct embedding_model.onnx is on device
$ADB root && $ADB remount
$ADB push /home/progcat/.cache/uv/archive-v0/fXAACauG3erLVqBHWTUTN/openwakeword/resources/models/embedding_model.onnx /system_ext/dollos/models/voice/oww/embedding_model.onnx
```

- [ ] **Step 3: Restart AI service and test**

```bash
$ADB shell "kill \$(pidof org.dollos.ai)"
sleep 5
$ADB logcat -c
# Say "gura" to phone, wait 15 seconds
sleep 15
$ADB logcat -d -s WakeWordEngine | grep -E "(score|detected)"
```

Expected: "gura" triggers with score > 0.5, other speech does NOT trigger.

- [ ] **Step 4: Revert debug logging in WakeWordEngine**

Restore the score logging to the original frequency:

In `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/WakeWordEngine.kt`, change:

```kotlin
// FROM:
if (score > 0.1f || feedCount % 50 == 0) {
    Log.d(TAG, "OWW score=${"%.4f".format(score)}")
}

// TO:
if (feedCount % 100 == 0) {
    Log.d(TAG, "OWW score=${"%.4f".format(score)}")
}
```

Build and deploy the updated APK.

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOS
git add gura.onnx
git commit -m "feat: retrain gura.onnx with fish-tts Japanese data + ACAV100M negatives"
```
