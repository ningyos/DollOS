"""Discord bridge for dollcore (text 2026-11-12, voice M6).

dollcore starts this process with DOLL_API (its local API) and DOLL_BRIDGE_TOKEN (this launch's
owner key) in the environment. Only the owner's DMs go in, as owner lines; what she says to the
owner comes back out to the owner's DM (core.py). In voice, only the owner's voice goes in and her
synthesized voice comes out, over `/api/voice` (voice.py).
"""
