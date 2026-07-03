"""AddressedText server message + external-origin emit (spec §3.1)."""
import json

from dollos.ipc.messages import AddressedText, encode_server_message


def test_addressed_text_encodes():
    raw = encode_server_message(AddressedText(channel_id="disc:1", text="hi"))
    d = json.loads(raw)
    assert d["type"] == "addressed_text" and d["channel_id"] == "disc:1" and d["text"] == "hi"
