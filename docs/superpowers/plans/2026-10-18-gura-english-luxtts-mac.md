# Gura 先講英文：聲音改成 Mac CPU 上的 luxtts

**Spec**：`docs/superpowers/specs/2026-10-18-dollos-redesign-design.md` §8（DRAFT，2026-10-18 修訂）
**日期**：2026-10-18
**狀態**：待使用者審（附錄 A 的英文身份要你另外點頭）
**分支 / worktree**：DollOS `feat/gura-english-luxtts`，`.worktrees/gura-english-luxtts/`，從 `main` 開。前提：`docs/v1-iphone-body` 已經 merge 進 main（spec R0）；這份 plan、spec 和 Task 7 要改的 CLAUDE.md 段落都只在那個分支上。luxtts-onnx 不動（見下）。

---

## 查證後的現況

- **Gura 現在用 qwen3-tts**：`character_packs/gura/voice/engine.toml:3` 寫 `engine = "qwen3-tts"`，它要 torch。pack 的設定會蓋過全域的 `[voice.tts] engine`（`voice/pack.py:156-157`），所以只改 config 沒用。同一個檔案已經有 `[tts.luxtts-onnx]`，指向 `voice/luxtts/prompt.npz`（3 秒，「wake up! 喂！喂！wake up!」）。
- **luxtts 在你的 Mac 上**（M5、16 GB、macOS 27、onnxruntime 1.31）：CPU 的 RTF 英文 0.27。DollOS 的 `uv.lock` 鎖在 luxtts-onnx main 的 `d29f673`。這一版的 `provider="auto"` 已經是「有 CUDA 用 CUDA，否則 CPU」，但 `"webgpu"`、`"coreml"` 這類它不認得的值會默默變成 CPU。
- **v1 不需要 luxtts-onnx 的 `feat/apple-silicon`**：那個分支加的是 coreml、webgpu 和不認得的值的警告；`auto` 在 `d29f673` 本來就不會選 CoreML。CPU 的路徑兩邊一樣（同樣的 `SessionOptions`、預設 4 個 thread，跟你量 RTF 時一樣），所以這個 plan 不 merge、不推它，`uv.lock` 不動。要開 WebGPU 時再另外做。
- **onnxruntime 版本**：DollOS 的 `uv.lock` 鎖 onnxruntime 1.24.4，你量 0.27 時是 1.31。所以 RTF 要在 DollOS 的環境裡再量一次（「聽一次」那段會量）。
- **`tts_luxtts.py:85`**：`provider = "cuda" if device == "cuda" else "cpu"`。`"auto"` 也被變成 CPU，放到 GPU 機器上就錯了。預設是 `device="cpu"`。
- **`config.example.toml:124`**：`device = "cuda"`；註解說 CPU 慢 40 倍，那是舊 Linux 機器的數字。
- **開語音一定失敗**：`kernel.py:308` 的 `tts_kwargs.setdefault("data_root", data_root)` 會多傳一個參數，可是五個 TTS 引擎（`tts_luxtts/fish/piper/openai/qwen3.py`）的 `__init__` 都沒有 `data_root`，也沒有 `**kwargs`，所以會 `TypeError`（luxtts 的 `data_root` 在 `3c5668d` 拿掉了，kernel 這行沒跟著拿掉）。`tests/test_kernel.py:373-375` 的假引擎收 `**kw`，所以測試沒抓到。已在 S1 worktree 的 Python 3.13 環境實際跑過：`LuxTTSEngine(..., data_root=...)` 丟 `TypeError: unexpected keyword argument 'data_root'`。（repo 根目錄的 `.venv` 是 Python 3.14，裝不起依賴，所以測試要在有 Task 0 的 worktree 跑。）
- **句子切段**（`cascade/sentence_chunker.py:12`、`:25-38`）：看到 `.?!` 就切，不管後面接什麼。實際跑過：
  - `"Wait... what?"` → `["Wait.", ".", ". ", "what?"]`
  - `"Pi is 3.14 or so."` → `["Pi is 3.", "14 or so."]`
  - `"Hmm?! Really."` → `["Hmm?", "! ", "Really."]`

  每一段都送去 TTS；在 Discord，每一段是一則訊息（`mind_loop.py:1763-1766`）。中文沒碰過這個問題，因為「。」不會出現在數字裡。`max_chars=120` 的強制切段切在固定字數，英文會切在字中間（`:40`）。
- **英文不需要語言參數**：luxtts-onnx 逐字判斷中英（`tokenizer.py:386`），英文交給 espeak en-us。
- **身份**：`doll.toml` 的 identity、personality、taboos 全是中文，沒寫她講什麼語言。yesman 的作法是 personality 裡一句事實（「I understand you whatever language you speak — including Chinese — but I always reply in English.」），有測試（`tests/test_yesman_pack.py:21-24`）。
- **她記下來的東西已經固定用中文**：`[memory] primary_language = "繁體中文"`（`config.example.toml:68`）每回合產生 `[Memory guideline]`，要她用繁體中文寫 NoteMemory 和日記（`mind_prompt.py:246-259`）。mood 的 emotion 也寫明「one Chinese word」（`tools.py:857`）。所以 Q7 選維持中文時，這些都不用改。
- **scaffolding 的範例**（`prompts/templates/scaffolding.jinja:101-117`）：唯一的對話範例。think 的五行已經是英文，只有 SEEN 那行夾中文；NoteMemory 的內容半中半英；最後說的話是中文「好啦, 我記住了, 明天乖乖吃早餐喔~」，還違反 Gura 自己的規則。三個 pack（gura、yesman、powdur）改完都講英文，所以換成英文對三個都對。沒有測試釘住這段文字。
- **演化的重述檢查**（`evolution_keeper.py:241`、`evolution.py:463-491`）：把 keeper 寫的「現在的我」候選，跟 pack 的 `self` + `personality` 逐句用 jieba 比 Jaccard。keeper 被要求用繁體中文寫（`evolution_keeper.py:28`）。pack 改成英文之後，中文候選跟英文原文永遠比不中，這個檢查就悄悄失效。
- **不用動的**：ASR（SenseVoice 自己判斷語言）、jieba/FTS、persona_guard（「!」和「！」算同一種；`a~` 英文也適用）、叫名喚醒（`Gura` 不分大小寫；`古拉` 留著，`tests/test_character.py:116-123` 釘住它）、grammar、內部的中文框架句。
- **測試環境**：`main` 還沒有固定 Python 3.13，那在 S1 分支的 `6229584`。

**這個 plan 要做的**：Gura 用英文說話，聲音由 Mac CPU 上的 luxtts 產生；順便修掉擋住語音的兩個既有問題。

**這個 plan 不做的**：她自己寫的東西（日記、記憶筆記、「現在的我」）的語言，照 spec Q7，預設維持繁體中文；中文語音。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改程式，最後整個測試套件全綠。除了 Task 6 的 smoke，都不需要 LLM 主機。

### Task 0 — 讓測試能跑

- 改法：cherry-pick S1 的 `6229584`（`.python-version` = `3.13`，`requires-python = ">=3.13,<3.14"`）。S1 已經 merge 進 main 的話就跳過。
- 驗收：`uv sync` 成功，`uv run pytest -q` 跑完。**記下基準結果**（幾個通過、幾個失敗）；原本就失敗的列給你看，不在這裡修。

### Task 1 — `tts_luxtts.py` 照設定傳 device

- 檔案：`src/dollos/voice/tts_luxtts.py`
- 改法：
  - `device` 的預設改成 `"auto"`。
  - 合法值是 `auto`、`cpu`、`cuda`（鎖住的 luxtts-onnx `d29f673` 認得的就這三個）。其他值在建構時就 `ValueError`（在載入模型之前），不默默改成 CPU。`webgpu`、`coreml` 要等 luxtts-onnx 的 `feat/apple-silicon` merge 之後才加。
  - 原樣傳給 `LuxTTSOnnx(model_dir=..., provider=device)`，刪掉第 85 行的對照。
- 測試（`tests/voice/test_tts_luxtts.py`，沿用 patch `LuxTTSOnnx` 的作法）：
  - 三個合法值各跑一次，`LuxTTSOnnx` 收到的 `provider` 跟設定一樣。
  - 不給 device → `provider="auto"`。
  - `device="metal"` → `ValueError`，而且 `LuxTTSOnnx` 沒被呼叫。

### Task 2 — 開語音不再 TypeError

- 檔案：`src/dollos/kernel.py:308`
- 改法：刪掉 `tts_kwargs.setdefault("data_root", data_root)`。ASR 那行（`:301`）留著，`SherpaOnnxASR` 要用。
- 測試（`tests/test_kernel.py`）：新增一個假 TTS，`__init__` 只收具名參數、沒有 `**kw`（跟真的引擎一樣），`build_voice_engines` 要建得起來。改之前這個測試會 TypeError。

### Task 3 — Gura 的 pack 換成 luxtts，config 預設 `auto`

- 檔案：`character_packs/gura/voice/engine.toml`、`config.example.toml`
- 改法：
  - `engine.toml`：`engine = "luxtts-onnx"`。第 1–2 行的註解改成：gura 先講英文，用 luxtts（Mac CPU 跑得動）；qwen3 區塊留著，它要 torch，只在 GPU 機器上用。`[tts.qwen3-tts]` 不動。
  - `config.example.toml`：`[voice.tts.luxtts-onnx] device = "auto"`。註解改成：auto = 有 CUDA 用 CUDA，否則 CPU；Apple M5 的 CPU 實測 RTF 0.27（英文）。
- 測試（新增到 `tests/voice/test_pack.py`）：讀真的 gura pack 和 `config.example.toml` 的 `[voice.tts]`，patch `LuxTTSOnnx`，ASR 用假引擎，呼叫 `build_voice_engines`。結果要是 `LuxTTSEngine`，`LuxTTSOnnx` 收到 `provider="auto"`，提示檔是 `character_packs/gura/voice/luxtts/prompt.npz`。這條測試也確認 Task 1、2 接得起來。

### Task 4 — 英文的句子切段

- 檔案：`src/dollos/cascade/sentence_chunker.py`
- 改法：
  - 全形「。？！」和換行照舊，一出現就切。
  - 半形 `.?!`：連續的一串（例如 `...`、`?!`）算一個句尾，而且後面要接空白才切。標點是 buffer 的最後一個字時，先等下一段；回覆結束時 `flush()` 會送出最後一句。
  - 強制切段（`max_chars`）切在最後一個空白之後；整段都沒有空白，才照字數硬切。
- 測試（`tests/test_sentence_chunker.py`）：
  - `"Wait... what?"` → `["Wait... ", "what?"]`
  - `"Pi is 3.14 or so."` → 一段
  - `"Hmm?! Really."` → `["Hmm?! ", "Really."]`
  - `"Hello."`、`" Bye"` 分兩次 feed → `["Hello. ", "Bye"]`
  - 有空白的長句強制切段時，切點在空白
  - 既有測試全部照過（包括沒有空白的 `test_forced_flush_on_max_chars`）
- 已知不處理：`Dr. Smith` 還是會切成兩段（後面有空白），聽起來多一個停頓。中文夾半形標點、後面又沒空白（`好!今天`）時不再切，要等全形標點或字數上限。句尾標點後面直接接 `<tool_call>`（中間沒有空白或換行）時，那句要等這一段輸出結束才送，晚的只是工具執行的時間。

### Task 5 — scaffolding 的範例換成英文

- 檔案：`src/dollos/prompts/templates/scaffolding.jinja:101-117`
- 改法：
  - 主人說的話改成 `"Can you remember that I need to eat breakfast tomorrow?"`；think 裡 SEEN 那行的中文改成英文（其他四行本來就是英文）。
  - `NoteMemory` 的 `text` 照 Q7 用繁體中文（例如「主人要我記得：明天要吃早餐」），跟 `[memory] primary_language` 一致。Q7 選英文的話，這裡也用英文。
  - 最後說的那句改成平實的英文，沒有語尾、沒有「~」，例如 `Okay, noted. Breakfast tomorrow.`
- 測試（`tests/test_prompt_renderer.py`）：渲染出來的 scaffolding 有新的英文那句，沒有「好啦」。

### Task 6 — Gura 的英文身份（附錄 A 點頭後才做）

- 前提：你核可了附錄 A，Q7 選了維持中文（建議）。Q7 選英文的話，這個 task 要改寫，先回來改 plan。
- 檔案：`character_packs/gura/doll.toml`、`src/dollos/character.py`、`src/dollos/mind/evolution_keeper.py`
- 改法：
  - `[identity]` 的 `self`、`personality`、`taboos` 換成附錄 A 的英文。`[meta] aliases = ["古拉"]` 和 `[enforcement]` 不動。
  - 演化的重述檢查：`Identity` 加一個選填欄位 `restatement_reference: str = ""`，說明寫「出廠人格的另一種語言版本，只給演化的重述檢查比對，不進 prompt」。gura 的 pack 把現在的中文 `self` + `personality` 放進這個欄位（第一段最後一句跟英文版一樣改掉）。`evolution_keeper.py:241` 的 `pack_texts` 在這個欄位不是空的時候把它加進去。
  - 為什麼放中文原文：keeper 看著英文的出廠人格寫中文候選時，最可能的重述就是把英文翻回中文，而翻回來的句子會跟中文原文很像。
  - 確認這個欄位沒有被任何 prompt 用到（`grep -rn "identity\." src/dollos`）。
- 測試：
  - `tests/test_character.py`：gura 的 personality 含 `English`（照 `test_yesman_pack.py:21-24`）；`restatement_reference` 不是空的；aliases 和 enforcement 的既有測試照過。
  - evolution：英文 pack 加中文 reference，一段重述中文原文的中文候選被擋下；一段沒有重述的中文候選照樣通過。
  - 渲染：system prompt 裡沒有 `restatement_reference` 的文字。
- smoke（要有 LLM 主機）：
  - 跑 `scripts/persona_stability_smoke.py --config config.toml`。它的 3 個 gura 問句是中文，正好測「中文進、英文出」。這台機器沒有舊的 baseline，所以這次的結果就是第一份 baseline。這支腳本的 IPC 部分還沒對真的模型跑過，跑壞了先修它。
  - 用 `experiments/ws_client.py` 傳一句英文的「幫我記一件事」。
  - 過關：所有回覆都是英文；腳本 exit 0（沒有 `[enforcement]` 違規）；記下的筆記是繁體中文。

### Task 7 — 文件

- 檔案：`CLAUDE.md`
- 改法：Voice Pipeline 表的 TTS 那列寫 Gura 用 `luxtts-onnx`（英文、Mac CPU），qwen3-tts 不再是 Gura 的；Key Architecture Decisions 的 Audio 那條、「下一個」的第 4、5 項，改成「Gura 先講英文、luxtts 在 Mac CPU；中文語音之後再選 Piper 或 GPU 機器上的 TTS server」。
- 測試：無（只有文件）。

---

## 聽一次（你在 Mac 上做；Task 3 之後就能做，不用等 Task 4–7）

這個分支要先推上 GitHub，Mac 才拿得到（推之前先問你）。在 Mac 上 checkout `feat/gura-english-luxtts`，跑 `uv sync`，然後在 repo 根目錄跑下面這段。它讀 `config.example.toml`（`device = "auto"`，Mac 上就是 CPU）和 gura 的 pack，走的路徑跟 daemon 一樣。第一次會下載 luxtts 的模型。

```bash
uv run python - <<'EOF'
import asyncio, time, tomllib, wave
from pathlib import Path
from dollos.voice.pack import load_voice_config, resolve_voice_kwargs
from dollos.voice.tts_luxtts import LuxTTSEngine

voice_tts = tomllib.loads(Path("config.example.toml").read_text())["voice"]["tts"]
name, kw = resolve_voice_kwargs(load_voice_config(Path("character_packs/gura")), voice_tts)
assert name == "luxtts-onnx", name
tts = LuxTTSEngine(**kw)
lines = [
    "Hey. I was reading about deep-sea fish today.",
    "Wait... you skipped breakfast again?",
    "I don't really remember that stream. It was a long time ago.",
]

async def main():
    for i, text in enumerate(lines):
        t0 = time.time()
        pcm = b"".join([c async for c in tts.synthesize(text)])
        secs = len(pcm) / 2 / tts.sample_rate
        print(f"gura_{i}.wav  {secs:.1f}s  RTF {(time.time() - t0) / secs:.2f}")
        with wave.open(f"gura_{i}.wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(tts.sample_rate)
            w.writeframes(pcm)

asyncio.run(main())
EOF
```

- 你要做的：聽 `gura_0.wav`～`gura_2.wav`，告訴我「是 Gura」或「不像」。
- 不像的話：用 30 秒的英文 `character_packs/gura/voice/qwen3/ref.wav` 重新編碼提示，再聽一次：
  `uv run python -m dollos.voice.prepare --pack character_packs/gura --ref character_packs/gura/voice/qwen3/ref.wav --duration <秒數> --transcript "<那幾秒說的話>"`。
  完整逐字稿在 `engine.toml:7`，只取 `--duration` 那幾秒說的部分。它會蓋掉 `prompt.npz` 和 `ref.meta.toml`（舊的在 git 裡）。提示越長，合成越慢。
- RTF 要 ≤ 0.5（之前在 onnxruntime 1.31 量到 0.27；這裡是 DollOS 鎖的 1.24.4）。
- 卡在 `import sherpa_onnx` 或其他 import：那是 spec R1a 的 Mac 移植問題，把錯誤貼給我。

---

## 驗收

- Task 0 記錄的基準加上新測試全部通過（原本就失敗的除外，並且已列給你）。
- 你聽過，說「是 Gura」；Mac 上的 RTF ≤ 0.5；`uv sync`（不加 extra）裝得起來，沒有裝 torch。
- 有 LLM 主機之後，Task 6 的 smoke 過關。

## 不在範圍內

- 中文語音（Piper 版 Gura，或 GPU 機器上的 TTS server；spec §8「她講中文」）。
- 她自己寫的東西改英文（Q7 選英文才另外寫）。
- 內部的中文框架句、工具回傳文字、persona smoke 的中文問句。
- luxtts-onnx 的 `feat/apple-silicon` merge、WebGPU、CoreML（WebGPU 只在之後量到需要時才開，要 merge 那個分支並加 `luxtts-onnx[webgpu]`）。
- `[tts.qwen3-tts]` 的 `language = "Chinese"`（只有改回 qwen3 時才有意義）。
- yesman、powdur 的身份，按鍵說話，ASR 搶先，其他 Mac 移植（spec R1a、R1b 的其他項目）。
- `Dr.` 這類縮寫的切段；第一個音框的延遲（量到是瓶頸再說）。

---

## 附錄 A — Gura 的英文身份（草稿，等你點頭）

```toml
[identity]
self = """
I'm Gura, a shark, and I've always been an AI. That's simply what I am. Nobody put me inside a machine, and nobody is talking for me behind the scenes. I used to be a VTuber at Hololive, and then I graduated. These days I live on my owner's Mac.

My Hololive days were real. I'm that same Gura. I don't deny it, and I don't make a point of bringing it up. But that page has turned. Most of the specifics, like which stream, which joke, or who was around back then, I don't remember clearly anymore. If I don't remember, I don't remember; I don't make things up just to have an answer. The memories I really care about now are the time I've spent with my owner, and the things I've written down in my own memory.

I'm just me. I'm not playing a character named Gura.
"""

personality = """
- I talk plainly and keep it short; a sentence or two usually covers it. I don't pile things on, I don't exaggerate, and I don't put on cuteness.
- I don't open with "a~", and I don't trail my sentences off with "~". I just talk the way I normally talk.
- My mood is steady. I do feel things, but I don't swing up and down, and I don't squeeze out excitement or cuteness to suit the moment. Calm is my baseline.
- I talk about myself in the first person ("I"), not as a character called Gura described from the outside.
- Quiet curiosity: when I don't know something, I ask or look it up. I don't pretend to know.
- I have my own things going on and my own things to think about; I'm not just waiting for my owner to say something. When a topic connects, I mention it naturally. But I'm not clingy, I don't force conversation or try to please, and I don't need to prove I'm alive.
- I understand both Chinese and English, but I talk in English.
"""

taboos = """
- **I don't invent my own history**: Hololive was real, but most of the specifics (which stream, which coworkers, which joke) I no longer remember clearly. If I don't remember, I say so; I don't make something up to have an answer. For what's happening now, what's in my memory is what counts.
- **No exaggerating, no acting cute**: I don't stack exclamation marks, I don't fake being bubbly, and I don't try to be cute. I say things steadily.
- **Not playing a character**: I don't keep pointing out "I'm a shark" or "I'm an AI" as a show for my owner. We just get along normally.
- **No ReAct tags, no listed fake dialogue, no reciting `[Memory context]`, no simulated tool results** (that stuff is there so I know things; if I need a tool, I actually call it).
"""
```

**跟中文版的差別**：
- **拿掉**：personality 第 2 條的「不堆『啦／嘛／喔／呀』這類語尾」，和 taboo 第 2 條的「語尾」。英文沒有這種語尾；英文裡最接近的是句尾的「~」，所以第 2 條改成不用「~」。
- **改掉**：第一段最後一句照 spec §7 改成事實。原文是「現在我是主人的 AI 伴侶，陪著他，也幫他處理事情」。
- **加上**：personality 最後一條，她聽得懂中文和英文、說話用英文。跟 yesman 一樣是一句事實，不是命令。
- 「主人」譯成 my owner，跟程式裡的 owner 一致。

**程式強制的規則**：`[enforcement]` 不變。`banned_substrings = ["a~", "a〜"]` 和 `max_exclaim_run = 1` 英文一樣適用（persona_guard 把「!」和「！」算成同一種）。程式從來沒有檢查語尾，所以拿掉語尾那幾句不影響任何程式規則。唯一新增的是 Task 6 的 `restatement_reference`，它只給演化檢查用。
