# Discord bridge and voice

The owner reaches the doll over Discord: direct messages, and a voice channel when voice is on. A separate Python process, the bridge (`bridge/`), talks to Discord and to dollcore's local API; it is the only path by which anything speaks *as the owner*, and it forwards only the owner's DMs and only his voice. In voice, dollcore transcribes him with Apple's speech recognizer and speaks her `voice.say` lines with Qwen3-TTS in her cloned voice.

## What the doll sees and does

### Direct messages

`discord.dm` sends her words to the owner's DMs. His DMs arrive as owner lines; attachments arrive only as file names, one line each: `[附件：<filename>]`. If the bridge is down, `discord.dm` fails with the reason and the line was not seen ([tool-failures.md](tool-failures.md)). A DM that Discord itself refuses is not reported to her.

### Voice tools (only when voice is enabled)

- `voice.join()`: enter the owner's current voice channel, only when he asks her to. His joining or leaving is not announced to her.
- `voice.say(text)`: speak in the channel. He hears only sound, no text.
- `voice.leave()`.

She can still use `discord.dm` while in voice. Wrong-state refusals (result starts 「失敗：」 and names the tool to use):

- owner not in voice: 「主人不在語音頻道裡，進不去。等他進了語音頻道、叫你進去再用 voice.join；現在跟他說話用 discord.dm。」
- already in: 「你已經在語音頻道「…」裡了，不用再進；在裡面說話用 voice.say，要出來用 voice.leave。」
- `voice.leave` when not in: 「你不在語音頻道裡，不用離開；要跟主人說話用 discord.dm。」
- `voice.say` when not in: 「你不在語音頻道裡，這句沒說出口。主人叫你進去才用 voice.join；現在要跟主人說話用 discord.dm。」
- no bridge link: 「語音連線沒有接上（bridge 沒有連到 /api/voice）」; voice not wired: 「語音還沒接上，語音頻道現在用不了；要跟主人說話用 discord.dm。」

**Listening.** She hears only the owner. His speech becomes text in the timeline labelled `[time 主人｜Discord 語音｜語音]`; a pause of about 0.7 s ends a sentence. Recognised text may contain wrong characters or homophones: read it for meaning. His voice cancels her model call in flight at once.

**Speaking.** Her text is split into sentences (at 。！？；… and newlines; long ones again at commas) and spoken one by one. The result comes when all is played; it is empty when he heard everything. Keep spoken lines short and colloquial. Do not put links, URLs, formulas, code, commands, file paths, long digit strings, emoji or kaomoji in `voice.say`: send them with `discord.dm` and just say so aloud ("我傳給你了").

**Interrupted.** When the owner starts talking she stops at once. The result says how far she got: 「說到「…」被主人打斷了（第 N 句「…」大約說到第 K 個字），後面「…」沒說出口。」, or 「還沒說出口就被主人打斷了：「…」主人都沒聽到。」 Later `voice.say` calls in the same reply return 「沒有念：前面一句被主人打斷了，這句沒說出口。」 What was unsaid was not heard: listen to him first, then decide whether and how to say it.

**【語音】 notices** (program events that give her a turn):

- 「【語音】主人離開語音頻道「…」了。你還在「…」裡；要出來用 voice.leave。」
- 「【語音】你被移出語音頻道「…」了。」
- 「【語音】語音連線斷了（…），你已經不在語音頻道「…」裡了。」 A `voice.say` in flight returns 「語音連線斷了：…」. The link reconnects by itself but she does not rejoin; when he asks again she uses `voice.join`. DMs are unaffected.

## How it works

### Bridge process (`Sources/DollCore/Bridge.swift`)

`Bridge.start` runs `[bridge] command` (argv; first element absolute, checked at config load) in dollcore's current directory, stdin `/dev/null`, with `DOLL_API` = `LocalAPI.baseURL` (`http://127.0.0.1:<[api] port>`) and `DOLL_BRIDGE_TOKEN` = `LocalAPI.ownerKey` (32 random bytes hex, new each launch, never on disk). Each output line goes to stderr as `bridge: …` and to the log at the level its Python prefix names.

Ready = an owner stream is open on the local API (`LocalAPI.listening(.owner) > 0`), opened after Discord's `on_ready` and the owner's DM channel resolve. Boot fails if the bridge exits first or is not ready within `ready_timeout_s` (then it is stopped). `LocalAPI.setBridgeState`: `starting` → `up (pid N)` → `down: <exit> at <time>; not restarted (fix the cause, restart dollcore)`, shown by `dollctl status` and in failed `discord.dm` results.

**Not restarted**: py-cord already reconnects to the gateway, so an exit means something a restart would not fix (bad token, missing intent, crash). dollcore keeps running; `Bridge.stop` (SIGTERM at shutdown) is not an error; if dollcore dies the owner stream ends and the bridge exits.

The bridge (`bridge/src/dollbridge/`: `__main__.py`, `core.py`, `api.py`, `voice.py`, `audio.py`) exits non-zero on: missing env vars or a bad `bridge/discord.toml` (unknown keys rejected) or libopus not loading (exit 2), login refused, Message Content intent off, owner DM channel unresolvable, owner stream ending, the *first* `/api/voice` connect failing, DAVE not ready after joining, UDP keep-alive failure (`VoiceFatal`).

### DMs (`core.py`)

- `on_message`: ignores her own messages; forwards only a DM whose author is `owner_id`; drops everything else with one INFO line (author id and where, never content). `owner_text` appends attachment lines; empty = dropped.
- `POST /api/owner`: dollcore answers 200 only after the line is written sealed to its inbox (`Doll.ownerLine`); otherwise the bridge DMs the owner `[系統] 這則沒送到她那裡：…` and logs ERROR.
- Owner stream (`GET /api/listen?to=owner`, NDJSON) in `on_core`: `say` → `split_message` (pieces ≤ `DISCORD_LIMIT` 2000, cut at line ends where possible, blank pieces dropped); `image` (absolute screenshot path) attaches to the last piece, a failed attach sends the words plus `[系統] 截圖沒附上：…`. `typing` on/off, re-triggered every `TYPING_EVERY_S` (8 s). A failed DM send is logged only.
- Intents: guilds, DM messages, guild messages (to drop and log them), message content, voice states.

### Voice WebSocket (`/api/voice`)

Authoritative: the header comment of `Voice.swift` and the docstring of `bridge/src/dollbridge/voice.py`; change both together. One socket `ws://127.0.0.1:<port>/api/voice`, `Authorization: Bearer <owner key>`; binary = PCM s16le mono, text = JSON with `type`.

Bridge → dollcore: owner audio (16 kHz, one frame per Discord packet, only while his packets arrive); `hello {version: 1}` first; `owner_joined {channel}` / `owner_left`; `owner_speaking` (first packet after ≥ `owner_burst_gap_ms` silence); `joined {channel}` / `left`; `played {utterance, samples}` (24 kHz samples handed to Discord, ≤ 100 ms apart, plus a final one); `error {message}`.

dollcore → bridge: `join` / `leave`; `utterance_start {utterance}`; her audio (24 kHz); `utterance_end {utterance}`; `stop {utterance}` (drop buffered audio; closed); `error {message}` before closing.

Rules: every violation gets `error` both ways plus an ERROR log, and a malformed frame closes the socket (`VoiceLink.control` → `VoiceLink.Control`, strict types). A late `played` for an old id is a race; `played` above samples sent is a violation. Utterance ids count up per process (`VoiceLink.utteranceSerial`) across reconnects. `audio.py` resamples statefully (`OwnerDownsampler` 48 kHz stereo → 16 kHz mono; `HerUpsampler` 24 kHz → 48 kHz stereo). Only the owner's packets are Opus-decoded (`OwnerSink`).

**One connection.** `LocalAPI` upgrades only with the owner key (401) and only when `[voice] enabled` (503). `VoiceLink.claim`: if a socket is open it is pinged; anything heard within `bridge_probe_ms` → new one gets 409; silence → the old one is closed as stale and replaced.

**Reconnect.** After a link that worked ends: dollcore (`VoiceLink.detached`) forgets both channels, fails pending join/leave and `voice.say`, and sends the 【語音】 notice if she was in. The bridge leaves the Discord channel, drops queued audio, waits `reconnect_delay_s` (doubling to `reconnect_max_delay_s`; a link that stayed up that long resets it), reconnects and re-sends `hello` and the owner's state. Only the first connect at startup is fatal.

### Listening (`VoiceEngines.swift`, `Voice.swift`)

`AppleSpeechRecognizer` wraps `SpeechTranscriber` (volatile + fast results, `asr_locale`; missing assets installed at boot). Its own finals come only every ~12 s and ignore pauses, so `Endpointer` decides: a frame is voiced if RMS ≥ `silence_rms`; `VoiceLink.tick` every `endpoint_poll_ms`; after `silence_ms` of wall time since the last voiced frame (push-to-talk release = no frames), the recognizer is zero-padded and `finalize(through:)` runs at the last voiced sample. It waits at most `asr_final_timeout_ms` (a finalize over silence never returns); no final = no words, logged. Finalizations run in order; text enters via `Doll.ownerLine(…, voice: true, channel: .discordVoice)`, like a DM (secret check, sealed inbox). Logs keep timing and length, never the transcript.

### Speaking

`QwenSynthesizer`: Qwen3-TTS 0.6B Base via `voice-mlx` in-process, sharing one MLX runtime with the embedder (`MLXProcess.configure`, `[core] mlx_cache_limit_mb`). Boot loads `tts_model` + `tts_codec` at `tts_precision`, encodes `reference_audio` with `reference_text` once (in-context cloning), and speaks `warmup_text` once; any failure fails the boot. 24 kHz float output, one generation at a time. DollOS ships no reference recording.

`VoiceLink.speak`:

1. `Sentences.split(text, maxChars: sentence_max_chars)`: ends after `。！？!?；;`, newline, ellipsis, or `.` before whitespace/end (`3.14`, `example.com` stay whole); trailing marks and closing quotes stay; a piece without letters or digits joins the previous; long sentences cut at commas, else hard. Shorter sentences make the "about where she was cut" more precise.
2. Language per sentence: `tts_language`, or `Sentences.language` for `"detect"` (kana → ja, Han → zh, Latin → en, else auto).
3. `utterance_start`; chunks converted to s16 and queued under the stop lock (`UtteranceShared`), so a chunk goes before a `stop` or not at all.
4. `utterance_end`, then wait for `played` to reach samples sent; no progress for `bridge_timeout_s` = error (after a `stop`).

Interruption: `owner_speaking` → `VoiceLink.ownerStarted` sets the stop flag, sends `stop`, collects `played` for `stop_settle_ms`. `SpokenReport.heard` scales played samples to sentence length; `SpokenReport.resultText` builds the result. `Doll.voiceCut` refuses later `voice.say` in the reply.

Manifest (`VoiceManifest.append`): every generated sentence, complete or cut, is kept for fine-tuning as `<dir>/<YYYYMMDD-HHMMSS>-u<utterance>-s<sentence>.wav` (24 kHz float32) plus a line in `<dir>/manifest.jsonl` (`VoiceManifest.Entry`: file, text, language, seed, session, utterance, sentence, start_ts, end_ts, sample_rate, samples, generation_complete, played_samples, interrupted). `<dir>` = `Config.voiceDirectory` (`[voice] data_dir` under `[core] data_dir` unless absolute). A write failure fails the call.

Refusals throw `VoiceFailure`; `Doll.voiceUnwired` when voice is off. Notices: `VoiceEvents.notice` → `Doll.voiceNotice`.

### Setup

`SETUP.md` steps 8–9 give the full procedure. Essentials: a bot with **Message Content Intent** enabled (else the bridge exits), invited with scope `bot` (plus **Connect** and **Speak** for voice) to a server the owner shares, since a DM needs one; `bridge/discord.toml` (gitignored, from `discord.example.toml`) holds `token` and `owner_id`, is never read by dollcore, and the token is never logged; `(cd bridge && uv sync --frozen)`; in dollcore's config `[bridge] enabled = true` with absolute paths in `command` and `[voice] enabled = true`.

Pins (`bridge/pyproject.toml`): Python `>=3.12,<3.13`, `uv` always `--frozen`. `py-cord[voice]` is pinned to a git commit of an upstream PR adding DAVE (voice E2EE) receive; move it only with `davey` and a live voice test. `davey==0.1.7` (0.2.0 never gets `dave.ready` and decodes noise). libopus is loaded from `opus_library` because py-cord does not find Homebrew's. `install_keepalive_fix` replaces py-cord's `UDPKeepAlive`, whose `sendto()` on a connected socket spins on macOS (EISCONN).

## Config

dollcore:

- `[bridge] enabled` = false; `command` = `["/opt/homebrew/bin/uv", "run", "--quiet", "--frozen", "--project", "bridge", "python", "-m", "dollbridge", "--config", "bridge/discord.toml"]`; `ready_timeout_s` = 60.0.
- `[voice] enabled` = false: off = no voice tools, no TTS load, `/api/voice` 503.
- `[voice] asr_locale` = "zh-TW"; `silence_ms` = 700; `silence_rms` = 300.0; `endpoint_poll_ms` = 20; `asr_final_timeout_ms` = 1500.
- `[voice] tts_model` = "aufklarer/Qwen3-TTS-12Hz-0.6B-Base-MLX-8bit"; `tts_codec` = "Qwen/Qwen3-TTS-Tokenizer-12Hz"; `tts_precision` = "int8"; `reference_audio` = "voice-reference.wav"; `reference_text` = "voice-reference.txt"; `tts_language` = "detect"; `warmup_text` = "嗯。".
- `[voice] sentence_max_chars` = 60; `data_dir` = "voice"; `stop_settle_ms` = 150; `bridge_timeout_s` = 10.0 (join/leave/playback progress); `bridge_probe_ms` = 1000.

`bridge/discord.toml` `[voice]` (optional, positive, unknown keys rejected by `VoiceConfig.from_toml`): `opus_library` = "/opt/homebrew/lib/libopus.dylib"; `owner_burst_gap_ms` = 200; `played_interval_ms` = 100 (≤ 100 by contract); `idle_hold_ms` = 500; `connect_timeout_s` = 15.0; `dave_ready_timeout_s` = 10.0; `udp_keepalive_s` = 5.0; `reconnect_delay_s` = 1.0; `reconnect_max_delay_s` = 30.0.

## Invariants

- Only the owner's DMs and voice reach her; the owner key is never on disk.
- Every protocol violation is reported both ways and logged; nothing is dropped silently.
- Once voice has connected, a voice failure never takes down DMs.
- `played` never exceeds samples sent; utterance ids are never reused within a process.
- Owner voice text enters through `Doll.ownerLine` like DM text.

## Known limitations

- Attachments pass as file names only.
- A DM Discord refuses is only in the bridge log.
- `[bridge] enabled = true` with `[voice] enabled = false` makes the bridge exit right after it comes up (first `/api/voice` gets 503, fatal). Enable both together.
- A voice line whose inbox write fails is dropped with ERROR (`voice: owner line not recorded (dropped)`, `Sources/DollCLI/main.swift`).
- The real TTS + recognizer path is covered only by opt-in tests (`DOLL_VOICE_REAL=1`, needs a reference recording).

## Tests

Swift: `theBridgeMustComeUpAndItsExitIsASystemError`, `apiAndBridgeConfigAreValidated`, `localAPIIsLoopbackOnlyAndOnlyTheBridgeSpeaksAsTheOwner`, `aSayLineCarriesTheScreenshotsPathForTheBridge`, `voiceToolsAndPromptOnlyWithVoiceEnabled`, `theSpeakingToolsAreRefusedWithAReasonWhenTheirChannelIsNotThere`, `voiceLinkRejectsASecondBridgeAndProtocolViolations`, `voiceStaleBridgeSocketIsReplacedByTheNewOne`, `voiceBridgeReconnectsMidUtteranceAndSheSpeaksAgain`, `voiceLoopbackHearsTheOwnerSpeaksAndStopsWhenHeTalks`, `endpointingClosesTheUtteranceAfterTheConfiguredSilence`, `anInterruptedSayReportsWhatWasHeard`, `everySpokenSentenceGoesToTheManifest`, `ownerVoiceCancelsTheCallAndTheRerunSeesTheVoiceWithTheSameEffort`, `hisVoiceCutsHerCallAtOnceAndTheRerunWaitsForItsCheck`; opt-in: `realVoiceThroughTheLoopback`, `realSpeechRecognizerFinalizesOnOurEndpoint`, `realBridgeRecoversFromAViolationMidUtterance`.

Bridge (`cd bridge && uv run --frozen pytest`; `bridge/tests/test_bridge.py`, `test_voice.py`): `test_owner_dm_is_forwarded`, `test_other_dms_and_guild_messages_are_dropped_and_logged`, `test_attachments_become_lines`, `test_undelivered_owner_dm_is_told_to_the_owner`, `test_say_goes_to_the_owner_dm_split_at_2000`, `test_a_say_with_an_image_carries_it_on_its_last_piece`, `test_example_config_documents_the_defaults`, `test_real_opus_round_trip` (needs libopus).
