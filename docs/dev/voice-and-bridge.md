# Discord bridge and voice

How the owner reaches the doll over Discord (direct messages and a voice channel), and how she
speaks back. Two processes are involved:

- `dollcore` (Swift): `Sources/DollCore/Bridge.swift` launches and watches the bridge;
  `LocalAPI.swift` serves it (see [local-api-and-dashboard.md](local-api-and-dashboard.md));
  `Voice.swift`, `WebSocket.swift` and `VoiceEngines.swift` implement the voice side.
- The bridge (Python, `bridge/`): `bridge/src/dollbridge/__main__.py` (entry, config, Discord
  client), `core.py` (DM logic), `api.py` (HTTP/WebSocket client of the local API), `voice.py`
  (voice channel), `audio.py` (resampling).

The bridge is the only path by which anything can speak *as the owner*. It forwards only the
owner's DMs and only the owner's voice.

## The bridge process (`Bridge.swift`)

`Bridge.start` runs `[bridge] command` (an argv array; the first element must be an absolute
path, checked at config load) with the current directory as working directory. It adds two
environment variables:

- `DOLL_API`: `LocalAPI.baseURL`, `http://127.0.0.1:<[api] port>`.
- `DOLL_BRIDGE_TOKEN`: `LocalAPI.ownerKey`, 32 random bytes in hex, new on every dollcore launch.

stdin is `/dev/null`. Each stdout/stderr line is copied to dollcore's stderr prefixed `bridge: `
and to the log file at the level its Python logging prefix names (`ERROR`/`CRITICAL` → ERROR,
`WARNING` → WARN, else INFO).

**Readiness** is "an owner stream is open on the local API" (`LocalAPI.listening(.owner) > 0`).
The bridge opens it only after Discord's `on_ready` and after it has resolved the owner's DM
channel. Boot fails if the bridge exits first, or is not ready within `[bridge] ready_timeout_s`
(it is then stopped). `LocalAPI.setBridgeState` tracks `starting` → `up (pid N)` → `down: …`;
`dollctl status` and her failed `discord.dm` results show that string.

**Not restarted.** After it was up, an exit is logged as ERROR, the state becomes
`down: exit status N at … ; not restarted (fix the cause, restart dollcore)`, and every later
`discord.dm` fails with that reason in her tool result. Reason: py-cord already reconnects to the
Discord gateway by itself, so a real exit means something a restart would not fix (bad token,
missing intent, a crash), and a silent restart would hide it. dollcore itself keeps running
(dollctl and the dashboard stay usable). `Bridge.stop` (SIGTERM at shutdown) is not an error.
If dollcore dies, the owner stream ends and the bridge exits on its own.

Config: `[bridge] enabled` (example default `false`), `command`, `ready_timeout_s`.

### What makes the bridge exit (non-zero)

`__main__.py` exits on anything it cannot carry on from: `DOLL_API`/`DOLL_BRIDGE_TOKEN` missing
(exit 2), a bad `bridge/discord.toml` (unknown keys are errors, like dollcore's config; exit 2),
libopus not loading (exit 2), login refused (`discord.LoginFailure`), the Message Content intent
off (`discord.PrivilegedIntentsRequired`), the owner's DM channel not resolvable, the owner
stream ending (`pump` raises `ConnectionError`), the *first* `/api/voice` connect failing, DAVE not
ready after joining voice, or the UDP keep-alive failing (`VoiceFatal`).

## Direct messages (`core.py`)

`Bridge.on_message` (Python):

- Her own messages are ignored without a log line.
- Only a DM (`message.guild is None`) whose author id equals `owner_id` is forwarded. Every other
  message (another user's DM, any guild message, the owner's guild messages too) is dropped with
  one INFO line naming the author id and where — never the content.
- Attachments are not downloaded: each becomes a line `[附件：<filename>]` ("[attachment: …]")
  after the text (`owner_text`). An empty result is dropped.
- Forwarding is `POST /api/owner`. dollcore answers 200 only after the line is written (sealed)
  to its inbox (`Doll.ownerLine`); on any failure the bridge replies in the DM
  `[系統] 這則沒送到她那裡：…` ("[system] this did not reach her: …") and logs ERROR.

`Bridge.on_core` handles the owner stream (`GET /api/listen?to=owner`, NDJSON):

- `say`: sent to the owner's DM via `split_message` — pieces of at most `DISCORD_LIMIT` (2000)
  characters, cut at line ends where possible, a line longer than the limit cut inside, blank
  pieces dropped (Discord refuses empty messages). A `say` with `image` (an absolute screenshot
  path on this Mac) attaches the file to the last piece; if attaching fails, the words still go
  and the owner gets `[系統] 截圖沒附上：…`.
- `typing` on/off: Discord's typing indicator, re-triggered every `TYPING_EVERY_S` (8 s; Discord
  shows it ~10 s per trigger). A `say` also stops it. Typing failures are WARN only.
- A failed DM send is logged ERROR in the bridge only; she is not told (her `discord.dm` already
  returned once the line was queued on the stream).

Intents (`intents()`): guilds, DM messages, guild messages (only so other messages are seen,
dropped and logged), message content (privileged), voice states.

## Dependency pins (`bridge/pyproject.toml`)

- Python `>=3.12,<3.13`; managed with `uv` (`bridge/uv.lock`; always `--frozen`).
- `py-cord[voice]` is pinned by `[tool.uv.sources]` to a specific git commit of an upstream pull
  request that adds DAVE (Discord voice end-to-end encryption) receive support. Voice receive
  depends on that commit. Move the pin only together with `davey` and a live voice test.
- `davey==0.1.7`: with 0.2.0, `dave.ready` stays false and every received packet decodes to noise.
- libopus is loaded explicitly from `[voice] opus_library` in `bridge/discord.toml` (default
  Homebrew path; `brew install opus`) because py-cord does not find Homebrew's on its own.
- `install_keepalive_fix` replaces py-cord's receive-side `UDPKeepAlive`: the upstream one calls
  `sendto()` on an already connected socket, macOS answers EISCONN and the thread spins. Ours uses
  `send()` and waits `udp_keepalive_s` between packets.

## Setting up a real Discord bot

1. Discord Developer Portal → New Application → **Bot** → **Reset Token**; copy the token (shown
   once).
2. Same page: enable **Message Content Intent** under Privileged Gateway Intents (Presence and
   Server Members are not needed). Without it the bridge exits with
   "Message Content Intent is off".
3. **OAuth2 → URL Generator**: scope `bot`. For DMs only, no permissions. For voice, add
   **Connect** and **Speak**. Invite the bot to a server the owner is also in: a DM needs a shared
   server, and the owner's privacy settings for that server must allow DMs from members.
4. Owner's user id: Discord → Settings → Advanced → Developer Mode on → right-click yourself →
   Copy User ID.
5. `cp bridge/discord.example.toml bridge/discord.toml` (gitignored) and fill `token` and
   `owner_id`. dollcore never reads this file; the bridge never logs the token.
6. First time: `(cd bridge && uv sync --frozen)` (creates `bridge/.venv`).
7. In the dollcore config: `[bridge] enabled = true`, `command` with absolute paths (the `uv`
   path from `which uv`). For voice also `[voice] enabled = true` (see the pitfall below).
8. Start dollcore; after `bridge: up (pid …)`, DM the bot from the owner's account.

`SETUP.md` steps 8–9 give the same procedure for end users.

## Bridge tests

`cd bridge && uv run --frozen pytest` — fake Discord, fake dollcore (`bridge/tests/test_bridge.py`,
`bridge/tests/test_voice.py`). `test_example_config_documents_the_defaults` keeps
`bridge/discord.example.toml` in sync with `VoiceConfig` defaults; `test_real_opus_round_trip`
needs libopus installed.

## Voice WebSocket protocol (`/api/voice`)

The authoritative description is the header comment of `Voice.swift` (dollcore side) and the
module docstring of `bridge/src/dollbridge/voice.py` (bridge side); both must change together.

The bridge opens one WebSocket `ws://127.0.0.1:<[api] port>/api/voice` with
`Authorization: Bearer <owner key>`, once Discord is ready. Binary frames are PCM, text frames
are JSON objects with a `type`.

bridge → dollcore:

| message | when |
|---|---|
| binary | the owner's audio, 16 kHz mono s16le, one frame per received Discord packet (~20 ms), only while his packets arrive |
| `hello {version: 1}` | first message of every connection |
| `owner_joined {channel}` / `owner_left` | the owner's voice channel changes; `owner_joined` right after `hello` if he is already in one |
| `owner_speaking` | first packet of a talk burst (after ≥ `owner_burst_gap_ms` without his packets) |
| `joined {channel}` / `left` | the bot's own voice connection |
| `played {utterance, samples}` | 24 kHz samples of that utterance handed to Discord; ≤ 100 ms apart while it changes, plus a final one when it finishes or is stopped |
| `error {message}` | anything that failed on the bridge side (join refused, playback failed, a dollcore violation) |

dollcore → bridge:

| message | meaning |
|---|---|
| `join` / `leave` | enter the owner's current voice channel / leave |
| `utterance_start {utterance}` | audio for this id follows |
| binary | her audio for the open utterance, 24 kHz mono s16le |
| `utterance_end {utterance}` | no more audio for it |
| `stop {utterance}` | drop its buffered audio now; it is closed (no more audio or `utterance_end`) |
| `error {message}` | a protocol violation by the bridge, sent right before dollcore closes the socket |

Rules:

- Every protocol violation is answered with an `error` message and an ERROR log line; a
  malformed frame or message closes the socket. Nothing is dropped silently.
  `VoiceLink.control` decodes into `VoiceLink.Control`; a field of the wrong type fails the
  whole decode. A late `played` for an already-finished utterance (id ≤ the last id) is a race,
  not a violation; `played` above the samples sent is a violation.
- Utterance ids count up per dollcore process (`VoiceLink.utteranceSerial`), across reconnects;
  the bridge starts every connection with an empty player (`Speaker`), so no id is reused.
- The bridge resamples (`audio.py`): Discord's 48 kHz stereo → 16 kHz mono for the owner
  (`OwnerDownsampler`), 24 kHz mono → 48 kHz stereo for her (`HerUpsampler`). Resamplers are
  stateful so 20 ms chunking gives the same samples as one pass; `played` counts are in 24 kHz
  samples (48 kHz output halved).
- Only the owner's packets are Opus-decoded (`OwnerSink`); everyone else's are dropped first.

### One connection, probe, reconnect

`LocalAPI` upgrades `/api/voice` only with the owner key (401 otherwise) and only when
`[voice] enabled` (503 otherwise). `VoiceLink.claim` holds the single slot: if a socket is open,
it sends that socket a ping and waits `[voice] bridge_probe_ms`. Anything heard (a pong
included) means it is alive and the new connection gets 409; silence means a half-open dead
connection, which is closed as stale (ERROR) and replaced.

When the socket ends after it once worked, voice failure affects voice only:

- dollcore (`VoiceLink.detached`): forgets both channels, fails a pending join/leave and an
  in-flight `voice.say` with `語音連線斷了：<why>` ("voice link dropped"), and, if she was in a
  channel, gives her one notice `【語音】語音連線斷了（…），你已經不在語音頻道「…」裡了。`.
- bridge (`VoiceLink.run`): logs ERROR, leaves the Discord voice channel, drops queued audio,
  waits `reconnect_delay_s`, reconnects, and sends `hello` plus the owner's state again. Each
  failed try (or a link that ended again soon) doubles the wait up to `reconnect_max_delay_s`;
  a link that stayed up at least that long starts over at `reconnect_delay_s`. DMs keep working.
- Exception: the *first* connect at bridge startup failing (dollcore unreachable, key refused,
  dollcore `[voice] enabled = false`) is fatal: nothing ever worked, so it is a setup error.

`discord.toml [voice]` keys (all optional, positive, unknown keys rejected by
`VoiceConfig.from_toml`): `opus_library`, `owner_burst_gap_ms`, `played_interval_ms` (≤ 100,
the contract), `idle_hold_ms`, `connect_timeout_s`, `dave_ready_timeout_s`, `udp_keepalive_s`,
`reconnect_delay_s`, `reconnect_max_delay_s`.

## Listening: ASR and endpointing

`AppleSpeechRecognizer` (`VoiceEngines.swift`) wraps Apple's `SpeechTranscriber` with
volatile + fast results, locale `[voice] asr_locale`; missing assets are installed at boot when
the system offers them. Its own finals arrive only every ~12 s and ignore pauses, so utterance
ends are ours:

- `Endpointer` (in `Voice.swift`): each frame is voiced if its RMS ≥ `[voice] silence_rms`
  (Discord's silence frames are 0; speech is in the thousands). `VoiceLink.tick` runs every
  `[voice] endpoint_poll_ms`; once `[voice] silence_ms` of wall time pass after the last voiced
  frame (push-to-talk released = no frames at all), the recognizer is padded with zeros up to
  that point and `finalize(through:)` is called at the last voiced sample.
- `AppleSpeechRecognizer.finalize` waits at most `[voice] asr_final_timeout_ms`, whether or not
  the analyzer's finalize returned: measured, a finalize over silence alone never returns, and one
  stuck call must not hold every later utterance. No final in time = no words (logged).
- Finalizations run one after another in utterance order. Non-empty text becomes an owner line
  through `Doll.ownerLine(…, voice: true, channel: .discordVoice)` — the same path as a DM, so it
  goes through the secret check; a voice line cancels her in-flight model call at once. The log
  records only timing and character count, never the transcript.

## Speaking: TTS, sentences, interruption

`QwenSynthesizer` (`VoiceEngines.swift`): Qwen3-TTS 0.6B Base through the `voice-mlx` package, in
the dollcore process, sharing one MLX runtime with the embedding model (`MLXProcess.configure`,
`[core] mlx_cache_limit_mb`). Boot loads `[voice] tts_model` + `tts_codec` at `tts_precision`,
encodes `reference_audio` with its transcript `reference_text` once (in-context voice cloning),
and speaks `warmup_text` once (discarded) to compile the model and seed the streaming decoder.
Any failure is a boot failure. Output is 24 kHz float; one generation at a time (voice-mlx is not
reentrant). DollOS ships no reference recording.

`VoiceLink.speak` (one `voice.say`):

1. `Sentences.split(text, maxChars: [voice] sentence_max_chars)`: ends after `。！？!?；;`,
   newline, ellipsis, or a `.` followed by whitespace/end (so `3.14`, `v1.2`, `example.com`
   stay whole); runs of marks and closing quotes/brackets stay with their sentence; a piece with
   no letter or digit joins the previous one; an over-long sentence is cut after commas, else
   hard. Reason for the cap: the longer a sentence, the less precise "about where she was cut".
2. Language per sentence: `[voice] tts_language`, or `Sentences.language` when `"detect"`
   (kana → ja, Han → zh, Latin → en, else auto).
3. `utterance_start`, then each sentence streams through the TTS; each chunk is converted to s16
   and queued under the utterance's stop lock (`UtteranceShared`), so a chunk is either sent
   before a `stop` or not at all, and `played` can never exceed what was sent.
4. `utterance_end`, then wait until `played` reaches the samples sent. No progress for
   `[voice] bridge_timeout_s` is an error (a `stop` is sent first).

**Interruption.** `owner_speaking` while she speaks → `VoiceLink.ownerStarted`: sets the stop
flag (the TTS stops before its next chunk), sends `stop`, then collects `played` for
`[voice] stop_settle_ms` more. `SpokenReport.heard` converts played samples into "full sentences
heard + about how many characters of the cut one" (played/samples scaled to sentence length).
`SpokenReport.resultText` is empty when everything was heard (like a delivered text), otherwise
`說到「…」被主人打斷了（第 N 句「…」大約說到第 K 個字），後面「…」沒說出口。` ("cut off at …,
about character K of sentence N; the rest was not said"). In `Doll.execute`, `Doll.voiceCut`
then refuses further `voice.say` calls in the same reply: `沒有念：前面一句被主人打斷了…`.

**Wav manifest.** Every generated sentence (complete or cut) is kept for a later fine-tune by
`VoiceManifest.append`: `<dir>/<YYYYMMDD-HHMMSS>-u<utterance>-s<sentence>.wav` (24 kHz mono
float32) plus one append-only line in `<dir>/manifest.jsonl` (`VoiceManifest.Entry`, snake_case:
file, text, language, seed, session, utterance, sentence, start_ts, end_ts, sample_rate, samples,
generation_complete, played_samples, interrupted). `<dir>` is `Config.voiceDirectory`:
`[voice] data_dir` under `[core] data_dir` unless absolute. `session` is the process start, since
utterance ids restart per process. A write failure fails the call (loud).

## Tool refusals (`voice.join` / `voice.leave` / `voice.say`)

Wrong-state calls throw `VoiceFailure`, whose Traditional Chinese text becomes her tool result and
names the tool to use instead:

- `voice.join` when already in a channel (use `voice.say` / `voice.leave`); when the owner is not
  in a voice channel (wait to be asked, use `discord.dm` now); while a join is pending.
- `voice.leave` / `voice.say` when she is not in a channel (use `discord.dm`).
- `voice.say` while the previous utterance runs, or with nothing speakable.
- Any call while no bridge socket has said `hello`: `語音連線沒有接上…`.
- Bridge timeouts: no `joined`/`left` within `[voice] bridge_timeout_s`.
- Voice not wired (`[voice] enabled = false`): `Doll.voiceUnwired`.

The owner joining or leaving voice is *not* an event for her (otherwise she joined every time he
did); `voice.join` refuses when he is absent. Only his leaving while she is inside, or her being
removed, gives her a 【語音】 notice (`VoiceEvents.notice` → `Doll.voiceNotice`).

## Invariants

- Only the owner's DMs and only the owner's voice reach her; the owner key is never on disk.
- Every protocol violation is reported both ways and logged; no silent drops.
- Voice failure never takes down DMs once voice has connected once.
- A `played` count never exceeds samples sent; utterance ids are never reused within a process.
- Owner voice text enters through `Doll.ownerLine` (sealed inbox, secret check) like DM text.

## Known limitations

- Attachments are passed as file names only.
- A DM that Discord refuses (owner closed DMs) is only in the bridge log; she is not told.
- `[bridge] enabled = true` with dollcore `[voice] enabled = false` makes the bridge exit right
  after it comes up (first `/api/voice` connect gets 503, which is fatal at startup). Enable
  voice whenever the bridge is enabled.
- If the inbox write fails for a *voice* line, the line is dropped with an ERROR log
  (`voice: owner line not recorded (dropped)` in `Sources/DollCLI/main.swift`); there is no one to
  answer, unlike a DM's 500.
- The real TTS + SpeechTranscriber path is covered only by opt-in tests
  (`DOLL_VOICE_REAL=1 swift test --filter realVoiceThroughTheLoopback`, needs a reference
  recording); the default Swift tests use fakes.
