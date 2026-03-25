# Key Management

This document describes the signing key procedures for DollOS. No actual key material appears here.

## Critical Security Rule

The key directory must NEVER be committed to any repository, shared over a network, or stored in plain text on an internet-connected machine. Treat all key files as secrets equivalent to root credentials.

## Key Directory

```
~/dollos-keys/bluejay/
```

All keys for the `bluejay` (Pixel 6a) target live under this path. Create it with restricted permissions:

```bash
mkdir -p ~/dollos-keys/bluejay
chmod 700 ~/dollos-keys/bluejay
```

## APK Signing Keys

DollOS requires the following 9 APK signing keys. Each key is generated with the AOSP helper script at `development/tools/make_key`.

| Key name | Purpose |
|----------|---------|
| releasekey | Default app signing |
| platform | Platform-signed apps |
| shared | Shared user ID apps |
| media | Media provider |
| networkstack | Network stack module |
| bluetooth | Bluetooth module |
| sdk_sandbox | SDK sandbox |
| nfc | NFC module |

Generate each key (run once, from the AOSP source root):

```bash
for key in releasekey platform shared media networkstack bluetooth sdk_sandbox nfc; do
    development/tools/make_key ~/dollos-keys/bluejay/$key \
        '/C=US/ST=California/L=Mountain View/O=DollOS/OU=Android/CN=DollOS/emailAddress=keys@example.com'
done
```

You will be prompted for a password for each key. All key passwords MUST be identical (AOSP build script requirement).

Note: Verify the required key list against the AOSP build scripts at your tag. The set of required keys may change between AOSP releases.

## AVB (Android Verified Boot) Key

The AVB key protects the boot chain. Generate a 4096-bit RSA private key and extract the public key in the format `avbtool` expects:

```bash
openssl genrsa 4096 > ~/dollos-keys/bluejay/avb.pem
avbtool extract_public_key \
    --key ~/dollos-keys/bluejay/avb.pem \
    --output ~/dollos-keys/bluejay/avb_pkmd.bin
```

`avb_pkmd.bin` is embedded in the device image. `avb.pem` must stay secret.

## OTA Signing Key

The OTA key authenticates over-the-air update packages. Use an elliptic curve key on the `prime256v1` curve. Do NOT use `ssh-keygen` for this purpose.

```bash
openssl ecparam -name prime256v1 -genkey -noout \
    -out ~/dollos-keys/bluejay/ota.pem
openssl req -new -x509 -key ~/dollos-keys/bluejay/ota.pem \
    -out ~/dollos-keys/bluejay/ota.crt \
    -days 10000 \
    -subj '/CN=DollOS OTA/'
```

## Encrypting the Key Directory

After generating all keys, encrypt the entire key directory using the provided script:

```bash
script/encrypt-keys ~/dollos-keys/bluejay
```

Store the encryption passphrase separately from the encrypted archive (e.g., in a hardware password manager or printed and stored offline).

## Offline Backup

After encryption:

1. Copy the encrypted archive to at least two offline storage media (USB drives or optical discs).
2. Label the media with the date and target device.
3. Store backups in physically separate, secure locations.
4. Test that decryption works before discarding the plaintext originals.

Loss of signing keys means loss of the ability to sign future builds or OTA updates for devices already enrolled with those keys.
