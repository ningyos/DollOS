# Voice scorecard — character_packs/powdur

- Engine: `qwen3-tts`
- Ref audio: `voice/transcripts/j3DAXXUiGJw.wav`
- Corpus: 10 sentences

## Summary

| Metric | Mean | Std | Min | Max |
|--------|------|-----|-----|-----|
| wavlm_sim | 0.933 | 0.038 | 0.824 | 0.964 |
| wer | 0.000 | 0.000 | 0.000 | 0.000 |
| prosody_distance | 6.330 | 6.148 | 1.767 | 24.052 |
| utmos | _skipped_ | _UTMOSv2 not available; install with: uv pip install git+https://github.com/sarulab-speech/UTMOSv2.git_ | — | — |
| nisqa | _skipped_ | _NISQA not available. Tried: nisqa-tts (not on PyPI), nisqa-toolkit (not on PyPI), git+https://github.com/gabrielmittag/NISQA.git (no pyproject.toml / setup.py). Leaving runner unavailable; drop the re_ | — | — |

## Per-sentence

| # | Sentence | prosody_distance | wavlm_sim | wer |
|---|----------|---|---|---|
| 0 | `Okay so let me tell you what happened to` | 5.236 | 0.918 | 0.000 |
| 1 | `Hey, look at my hat. Isn't it beautiful?` | 5.217 | 0.964 | 0.000 |
| 2 | `Wait, why would you even do that? That m` | 7.512 | 0.824 | 0.000 |
| 3 | `Dude this is actually crazy, like genuin` | 2.810 | 0.944 | 0.000 |
| 4 | `I had the weirdest dream last night, you` | 5.828 | 0.956 | 0.000 |
| 5 | `Stop, stop, stop. We are not doing that ` | 5.284 | 0.923 | 0.000 |
| 6 | `I genuinely cannot believe people are st` | 3.349 | 0.953 | 0.000 |
| 7 | `Hold on, that sentence was about to be w` | 1.767 | 0.949 | 0.000 |
| 8 | `Honestly, I think I might just go to bed` | 24.052 | 0.948 | 0.000 |
| 9 | `Oh my god, did you see what they posted?` | 2.245 | 0.947 | 0.000 |
