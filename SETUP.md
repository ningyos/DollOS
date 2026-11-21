# SETUP.md — installing DollOS (written for a coding agent)

You are an AI coding agent helping a person install DollOS on their Mac. Follow this file top to bottom.
It tells you what to check, what to ask, what to write, and how to verify. It is written for you, not
for the person; talk to them in their language.

DollOS runs one "doll": a character with her own memory, calendar, goals and moods, who talks to her
owner (the person you are helping) and, if allowed, operates this Mac. The program is `dollcore`.

## Rules for you

- **Ask one question at a time.** Wait for the answer. Offer a default when there is a sensible one.
- **Never print, log or commit secrets** (the Discord bot token). Write them only into the file named below.
- **Do not touch anything outside** the repository clone and the data folder you agree on with the person,
  except what a step below names (Homebrew packages, the Keychain item DollOS creates itself).
- **Do not run anything with `sudo`.** Nothing here needs it.
- **Do not guess.** If a check fails and this file does not say what to do, stop and tell the person
  exactly what failed (the command and its output).
- Every path you write into the config must be **absolute**.

## 1. Check the machine

Run and check each; stop with a clear message if one fails.

| Requirement | Check | Why |
|---|---|---|
| Apple silicon | `uname -m` prints `arm64` | MLX (memory search, voice) runs on Apple silicon only |
| macOS 26 or later | `sw_vers -productVersion` ≥ 26 | the package's minimum platform |
| Swift 6.2 or later (Xcode 26+) | `swift --version` | the package's tools version |
| Disk | ≥ 10 GB free in the home volume | models: memory embedding ~1.2 GB; voice ~2–3 GB if enabled; the build ~3 GB |
| Memory | ≥ 16 GB recommended | voice alone peaks around 4 GB |

## 2. The language model

DollOS does not include a language model. It needs a server that speaks the **OpenAI chat completions
API with tool calling** (`POST <base_url>/v1/chat/completions`, `tools`, `tool_calls`). It also sends
`reasoning_effort`; a server that ignores unknown fields is fine. llama.cpp's `llama-server` works.

Ask the person:
1. The server's base URL (without `/v1`), e.g. `http://192.168.1.20:8080`.
2. The model name the server expects.

Verify before going on:
```sh
curl -s <base_url>/v1/models
```
It must answer. If it does not, stop: nothing else will work.

Notes to pass on:
- One request at a time is enough; DollOS sends requests one after another.
- If the server goes away, DollOS waits and retries (it does not exit); it only refuses to **boot** when
  the server cannot be reached.

## 3. Build

From the repository root:
```sh
swift build -c release
```
The first build downloads Swift packages and takes several minutes. When it ends with
`Build complete!`, the program is `.build/release/dollcore` and next to it three resource bundles
(`*.bundle`) it needs at run time.

## 4. The data folder

Ask where the doll's data should live. Default: `~/DollOS-data/<doll id>/` (for example
`~/DollOS-data/mydoll/`). Everything she has — memories, timeline, calendar, logs, history — goes there.

Create it and copy the program next to it so a rebuild of the repository never changes a running doll:
```sh
mkdir -p ~/DollOS-data/bin ~/DollOS-data/<doll id>
cp .build/release/dollcore ~/DollOS-data/bin/
cp -R .build/release/*.bundle ~/DollOS-data/bin/
chmod 700 ~/DollOS-data
```

## 5. The character

The repository has one template: `character_packs/template/doll.toml`. It shows every field with a
comment. Copy it into the data folder and fill it in **with the person**:
```sh
cp character_packs/template/doll.toml ~/DollOS-data/<doll id>/doll.toml
```

Fields (all strings unless noted):
- `[meta] id` — short, lowercase, no spaces. `name` — what she is called. `aliases` (list) — other names.
  `language` — e.g. `"zh-TW"`, `"en"`.
- `[identity] self` — who she is, in her own voice ("I am …"). Required.
- `[identity] personality` — how she behaves. Required.
- `[identity] taboos` — what she must not do or say. Required.
- `[identity] examples` — optional short example exchanges; they help more than adjectives.
- `[identity] ideal` — who she wants to become; only the starting point, she revises it herself.
  Required while `[drives]` is enabled (it is by default).

Ask about each field in plain words ("How should she talk?", "What should she never do?") and write
what the person says. Do not invent a personality for them. If they want a real person's or someone
else's character, remind them that it is theirs to judge, and keep it out of anything they publish.

## 6. The config file

Copy the example and change only the keys below:
```sh
cp dollcore.example.toml ~/DollOS-data/<doll id>/dollcore.toml
```

| Key | Set to |
|---|---|
| `[llm] base_url`, `model` | from step 2 |
| `[core] data_dir` | `<data folder>/data` |
| `[core] pack` | `<data folder>/doll.toml` |
| `[core] docs` | `<repository>/dolldocs` (absolute) |
| `[memory] dir` | `<data folder>/memory` |
| `[memory] keychain_service` | leave the default unless two DollOS installs share this Mac user |
| `[dashboard] port`, `[api] port` | defaults 8790 / 8791; change only if taken (`lsof -iTCP:<port> -sTCP:LISTEN`) |
| `[owner_sleep] start`, `end` | ask when the person usually sleeps (24 h, `HH:MM`) |
| `[computer] enabled` | step 7 |
| `[bridge] enabled`, `command` | step 8 |
| `[voice] enabled`, `reference_audio`, `reference_text` | step 9 |

Every other key has a comment explaining it and a default chosen for a reason; leave them.
The config is strict: an unknown key or a wrong type stops the boot with a message naming it.

## 7. Operating this Mac (optional)

Ask: **"May she operate this Mac — open and use apps, read windows, read and write files in your home
folder?"** Explain plainly: she does it in the background without taking the mouse; the hard safety
line is Touch ID / the system password (she cannot type it); otherwise she relies on her judgment and
asks before sensitive actions, and she can make mistakes.

- **No**: `[computer] enabled = false`. She then has no computer or file tools.
- **Yes**: install cua-driver (open source, https://github.com/trycua/cua; DollOS was tested with
  0.52.x) following its own instructions, so that `/Applications/CuaDriver.app` exists. Then give it
  **Accessibility** and **Screen Recording** in System Settings → Privacy & Security (the person must
  click these themselves). Set `[computer] driver` to its CLI
  (`/Applications/CuaDriver.app/Contents/MacOS/cua-driver`) and `enabled = true`.
  A missing driver or permission stops the boot with a message saying which.

## 8. Discord (optional)

This is how the person talks to her from their phone. Ask whether they want it.

1. They create a bot: https://discord.com/developers/applications → New Application → **Bot** →
   **Reset Token** (copy it; shown once). On the same page turn on **Message Content Intent**.
2. **OAuth2 → URL Generator**: scope `bot`; for voice also permissions **Connect** and **Speak**.
   They open the URL and invite the bot to a server they are in.
3. Their user ID: Discord → Settings → Advanced → Developer Mode on → right-click themselves →
   Copy User ID.
4. Install `uv` if missing (`brew install uv`), then:
   ```sh
   cp bridge/discord.example.toml bridge/discord.toml   # gitignored
   (cd bridge && uv sync --frozen)
   ```
   Write the token and `owner_id` into `bridge/discord.toml`. Do not echo the token.
5. In the config: `[bridge] enabled = true` and `command` with **absolute** paths:
   `["<path to uv>", "run", "--quiet", "--frozen", "--project", "<repository>/bridge", "python", "-m", "dollbridge", "--config", "<repository>/bridge/discord.toml"]`
   (`which uv` gives the path).

Without Discord, the person talks to her in a terminal with `dollctl` (step 11).

## 9. Voice (optional, needs Discord)

She can join the person's Discord voice channel and speak in a cloned voice (Qwen3-TTS, runs on this
Mac). Ask whether they want it.

- Needs `brew install opus` (the bridge loads `/opt/homebrew/lib/libopus.dylib`).
- Needs a **reference recording** the person has the right to use: a clean WAV of one speaker, about
  10–20 seconds, and a text file with exactly what is said in it. DollOS ships none. Put both in the
  data folder and set `[voice] reference_audio` and `reference_text` to their absolute paths.
- `[voice] enabled = true`. The first boot downloads the voice model (a few GB) into
  `~/Library/Caches`; later boots load it in about 10 s.

## 10. First boot

Run it in the foreground once so you can read it:
```sh
cd ~/DollOS-data/<doll id> && ~/DollOS-data/bin/dollcore --config ~/DollOS-data/<doll id>/dollcore.toml
```
Expect, in order: a `log:` line, `computer:` (enabled or disabled), the memory index line, `voice:`,
`api:` and `dashboard:` with their URLs, `bridge: up` (if enabled), and finally
`<name> is up. Ctrl-C stops her.` The first boot also downloads the memory embedding model
(~1.2 GB) and creates a Keychain item (service `[memory] keychain_service`) holding the key that
encrypts her secrets.

If it stops instead, the last line says why (a config key, a missing file, a permission, the model
server). Fix that one thing and run it again. Do not edit other keys to make it pass.

Then check:
- Open the dashboard URL in a browser; it shows her state, memory, calendar and goals.
- Talk to her once (Discord DM, or step 11) and see her answer.

Stop her with Ctrl-C (a clean stop).

## 11. Running her

- In the background, kept running after the terminal closes:
  ```sh
  cd ~/DollOS-data/<doll id> && nohup ~/DollOS-data/bin/dollcore --config dollcore.toml >> dollcore.out 2>&1 &
  ```
  Stop: `kill <pid>` (SIGTERM is a clean stop). She does **not** start by herself after the Mac
  restarts; run the command again.
- Talk to her from a terminal as the developer:
  `<repository>/.build/release/dollctl --config <data folder>/dollcore.toml chat`
  (`… status` shows her state). Without `--config` it assumes the default API port 8791.
- Her own log: `<data folder>/data/logs/dollcore.log`. After a crash or an unclean stop, her next boot
  reads it and tells her what happened.

## 12. Updating

```sh
git pull && swift build -c release
```
Stop her, copy the new `dollcore` and `*.bundle` into `~/DollOS-data/bin/` (step 4), start her again.
If the new version's config has new keys, the boot names any that are wrong; compare with the new
`dollcore.example.toml`.

## Things to tell the person before you finish

- Her secrets (what she stores as secret) are encrypted with a key in their **login Keychain**, local
  to this Mac. If that Keychain item is lost, those secrets are lost; there is no other copy.
- A message of theirs is checked for secrets before it is stored (one extra request to the model,
  about a second). Starting a message with 🔒 marks it secret without the check.
- Everything she does that changes her own state (memories, goals, ideal self) is versioned; the
  dashboard can show and undo changes.
