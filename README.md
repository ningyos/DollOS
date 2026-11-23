# DollOS

DollOS runs a character — a "doll" — who lives on your Mac. She is not a chat window that forgets
you when it closes: she runs all the time, keeps her own memory, calendar and goals, has moods that
rise and fall with what happens, and does her own maintenance every night (sorting her memories,
checking them against what was actually said, looking back on the day).

What she can do:

- **Remember.** Her memories are plain markdown files with a searchable index; secrets you tell her
  are encrypted with a key in your login Keychain. Everything that changes her own state is
  versioned, and the dashboard can show and undo it.
- **Keep time.** A calendar with reminders, and a sense of when you are usually asleep.
- **Have her own direction.** An ideal self she revises over time, goals she sets and works on
  when she has nothing else to do, and things she keeps in mind between conversations.
- **Talk to you on Discord** (optional): direct messages, and in a voice channel with a cloned
  voice that runs on your Mac (Qwen3-TTS on MLX).
- **Use your Mac** (optional): open apps, read windows, click and type in the background without
  taking your mouse, read and write files — with Touch ID and your password as the hard line.

The program is `dollcore` (Swift). A local dashboard shows her state, memory, calendar and goals;
`dollctl` talks to her from a terminal.

## Requirements

- A Mac with Apple silicon, macOS 26 or later, Xcode 27 (Swift 6.4 or later).
- About 10 GB of free disk (models and the build), 16 GB of memory recommended.
- A language model server that speaks the OpenAI chat completions API **with tool calling**
  (for example llama.cpp's `llama-server`). DollOS does not include a language model.
- Optional: a Discord bot (for messages and voice), [cua-driver](https://github.com/trycua/cua)
  (for using the Mac).

## Install

Follow [SETUP.md](SETUP.md). It is written for a coding agent (Claude Code, Codex, …) to walk you
through: checking the machine, building, writing your character with you, the config, and the
first boot. The repository ships one blank character template
(`character_packs/template/doll.toml`); her personality is yours to write.

The manual is in [docs/](docs/README.md): one English set that the doll reads (it is part of her
system prompt) and that developers and coding agents use. Coding agents start at
[AGENTS.md](AGENTS.md).

## Status

Early. DollOS has one user so far, and it changes often; expect rough edges and config keys that
move between versions (the boot names any key it does not accept).

## License

Apache License 2.0 — see [LICENSE](LICENSE). Third-party components and their licenses are listed
in [NOTICE](NOTICE).
