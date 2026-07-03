# MVP spec R2 findings（原始清單，供 writing-plans 逐 P1x plan 消化）

R2 全 5-lens 覆核 spec v2（task wyqehobc1 / workflow wf_2f6f5321-c66）。3 Critical 已修入 v3（§2/§3.3/§3.4）。以下 13 Important + 8 Minor 留待各 P1x plan 在 writing-plans 時消化。完整原文見 workflow journal.jsonl（`.../subagents/workflows/wf_2f6f5321-c66/journal.jsonl`）。

（此檔為 R2 findings 的暫存索引；下個 session 應從 journal 抽全文補完本檔，再逐條指派到 P1a-P1g。）

## R1_fixes verdict 摘要（各 lens 對自己領域 R1 修正的裁決）
- **architecture**: PARTIAL — 五個 [R1-arch] 修正都正確診斷、三個乾淨成立（I3 cache-split、I2 sink locus、C1 per-origin 皆對照 code 確認）；殘留待補（見下）。
- **security**: PARTIAL — S1 external_ctx（ChannelMessage 進 _EXTERNAL_KINDS，PinSelf 讀 ctx.external_ctx tools.py:811）VERIFIED-SOUND;其餘 S2/S3 scope 過濾在 memsearch 層的實作具體度待驗。
- **trace**: CONFIRMED（含殘留 gap）— per-pass atom 對照 mind_loop.py:627/633 確認;input_messages_delta 精確定義、per-pass grammar/GBNF state、voice 未來待補。
- **scope**: CONFIRMED-sound-but-self-undermining — R1 修正都對，正因如此 P1 變 ~10 概念 → 已拆 EPIC。
- **attention**: partial — 預設沉默+code admission 半有效（修失敗 1）;reply-chain 定義（已於 v3 補對話式 session）+ 串內 disengage（v3 補）+ 差異化 debounce（v3 補）。

## 待抽全文的 13 Important（下個 session 從 journal 補）
- attention: window 自我增強迴圈（v3 已補 disengage 閘，確認是否足）;L2 admit 後仍弱模型判斷（v3 補 code session-turn 閘為主防線）;固定 debounce 熱串堆疊（v3 補差異化);
- architecture: [R1-arch] 殘留 —（待抽）
- security: S2/S3 memsearch 層 scope 實作、owner-DM-DoS 可否再濫用、DiscordLookup RPC 作為注入/DoS 向量、keyed-grammar-cache 正確性（待抽）
- trace: input_messages_delta 精確定義、per-pass grammar state（待抽）
- scope: Discord 429 rate limit、reconnect gap dedup 是否 specified（待抽）

## 8 Minor（下個 session 從 journal 補）
- name_aliases 子字串命中第三人稱、stranger-DM rate（attention）;其餘待抽。
