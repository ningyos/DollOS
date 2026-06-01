# Voice scorecard — character_packs/yesman

- Engine: `qwen3-tts`
- Ref audio: `voice/qwen3/ref.wav`
- Corpus: 10 sentences

## Summary

| Metric | Mean | Std | Min | Max |
|--------|------|-----|-----|-----|
| wavlm_sim | 0.930 | 0.024 | 0.897 | 0.959 |
| wer | 0.009 | 0.027 | 0.000 | 0.091 |
| prosody_distance | 17.073 | 1.530 | 14.714 | 19.687 |
| utmos | _skipped_ | _UTMOSv2 not available; install with: uv pip install git+https://github.com/sarulab-speech/UTMOSv2.git_ | — | — |
| nisqa | _skipped_ | _NISQA not available. Tried: nisqa-tts (not on PyPI), nisqa-toolkit (not on PyPI), git+https://github.com/gabrielmittag/NISQA.git (no pyproject.toml / setup.py). Leaving runner unavailable; drop the re_ | — | — |

## Per-sentence

| # | Sentence | prosody_distance | wavlm_sim | wer |
|---|----------|---|---|---|
| 0 | `Okay so let me tell you what happened to` | 14.714 | 0.913 | 0.000 |
| 1 | `Hey, look at my hat. Isn't it beautiful?` | 15.894 | 0.899 | 0.000 |
| 2 | `Wait, why would you even do that? That m` | 18.010 | 0.959 | 0.000 |
| 3 | `Dude this is actually crazy, like genuin` | 15.425 | 0.901 | 0.000 |
| 4 | `I had the weirdest dream last night, you` | 19.687 | 0.932 | 0.000 |
| 5 | `Stop, stop, stop. We are not doing that ` | 17.624 | 0.945 | 0.000 |
| 6 | `I genuinely cannot believe people are st` | 18.719 | 0.957 | 0.000 |
| 7 | `Hold on, that sentence was about to be w` | 15.495 | 0.951 | 0.000 |
| 8 | `Honestly, I think I might just go to bed` | 17.565 | 0.948 | 0.000 |
| 9 | `Oh my god, did you see what they posted?` | 17.594 | 0.897 | 0.091 |
