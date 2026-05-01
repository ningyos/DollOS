# DollOS Base Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bootable DollOS ROM based on GrapheneOS for Pixel 6a (bluejay), with DollOS branding, SELinux-protected data directories, a system service skeleton, and a customized OOBE setup wizard.

**Architecture:** Fork GrapheneOS's AOSP 16 manifest and key repositories. Create a DollOS manifest repo that points to GrapheneOS upstream and overrides forked repos. DollOS-specific code (system service, OOBE wizard) lives in dedicated repos wired into the manifest. SELinux policy additions go in a device-specific sepolicy directory to avoid conflicts with upstream `system/sepolicy`.

**Tech Stack:** AOSP build system (Soong/Make), Kotlin (system app/service), AIDL (IPC), SELinux (TE policy), repo tool (source management), fastboot (flashing)

---

## File Structure

### New DollOS Repositories (hosted on GitHub/GitLab)

| Repository | Purpose |
|-----------|---------|
| `dollos/platform_manifest` | Fork of GrapheneOS manifest, adds DollOS repo `<project>` entries |
| `dollos/branding` | Fork of GrapheneOS `vendor/grapheneos` with DollOS identity |
| `dollos/script` | Fork of GrapheneOS `script/` with DollOS key paths and product names |
| `dollos/platform_packages_apps_DollOSService` | DollOS persistent system service (AI Core daemon skeleton) |
| `dollos/platform_packages_apps_DollOSSetupWizard` | Custom OOBE setup wizard |
| `dollos/device_dollos_bluejay` | Device config + SELinux policy for Pixel 6a |

### Modified Upstream Repositories (Fork Required)

| Repository | Changes |
|-----------|---------|
| `dollos/platform_packages_apps_Settings` | Add DollOS settings section (API key, model management) -- future task |

### Key Files Within AOSP Tree

```
# DollOS manifest
.repo/manifests/default.xml        -- forked, adds DollOS project entries

# Branding
vendor/dollos/branding/             -- logos, boot animation, wallpapers

# Device configuration + SELinux
device/dollos/bluejay/
  AndroidProducts.mk
  dollos_bluejay.mk
  BoardConfig.mk
  sepolicy/
    dollos_data_file.te             -- data file type declaration
    dollos_service.te               -- service domain rules
    file_contexts                   -- /data/dollos paths
    seapp_contexts                  -- map DollOS app to domain

# DollOS System Service
packages/apps/DollOSService/
  Android.bp
  AndroidManifest.xml
  privapp-permissions-dollos-service.xml
  aidl/org/dollos/service/
    IDollOSService.aidl
  src/org/dollos/service/
    DollOSService.kt
    DollOSServiceImpl.kt

# DollOS Setup Wizard (OOBE)
packages/apps/DollOSSetupWizard/
  Android.bp
  AndroidManifest.xml
  privapp-permissions-dollos-setup.xml
  res/layout/
    activity_setup_wizard.xml
    page_welcome.xml (+ other page layouts)
  src/org/dollos/setup/
    SetupWizardActivity.kt
    pages/
      WelcomePage.kt
      WifiPage.kt
      GmsPage.kt
      ModelDownloadPage.kt
      ApiKeyPage.kt
      PersonalityPage.kt
      VoicePage.kt
      CompletePage.kt
```

### Task Dependency Graph

```
Task 1 (Build Env)
  |
  v
Task 2 (Manifest Fork + Source Sync)
  |
  v
Task 3 (First Unmodified Build + Flash) -- REQUIRED flash verification
  |
  +--> Task 4 (Device Config) --+--> Task 7 (SELinux)
  |                              |
  +--> Task 5 (Signing Keys)    +--> Task 8 (System Service)
  |                              |
  +--> Task 6 (Branding)        +--> Task 9 (OOBE Wizard)
                                 |
                                 v
                           Task 10 (Release Script Fork)
                                 |
                                 v
                           Task 11 (Full Build + Flash + Verify)
```

Tasks 4, 5, 6 can run in parallel after Task 3.
Tasks 7, 8, 9 can run in parallel after Task 4.
Task 10 depends on Task 5 (keys) and Task 6 (branding).

---

## Task 1: Build Environment Setup

**Goal:** Prepare a Linux build machine capable of compiling GrapheneOS.

**Files:**
- Create: `docs/build-env-setup.md`

- [ ] **Step 1: Verify system requirements**

```bash
free -h           # Need 32GB+ RAM
df -h .           # Need 500GB+ free disk
lsb_release -a    # Need Ubuntu 22.04+ or 24.04
```

Expected: Ubuntu 22.04+, 32GB+ RAM, 500GB+ free.

- [ ] **Step 2: Install build dependencies**

```bash
sudo apt update
sudo apt install -y repo yarnpkg git curl python3 python3-pip \
  openjdk-21-jdk zip unzip rsync bison flex gcc g++ make \
  libssl-dev libncurses-dev gperf xmlstarlet \
  fonttools ninja-build
```

- [ ] **Step 3: Configure git identity**

```bash
git config --global user.name "DollOS Builder"
git config --global user.email "builder@dollos.org"
```

- [ ] **Step 4: Document and commit**

Create `docs/build-env-setup.md` with exact system requirements and install commands.

```bash
cd ~/Desktop/DollOS
git add docs/build-env-setup.md
git commit -m "docs: add build environment setup guide"
```

---

## Task 2: Create DollOS Manifest and Sync Source

**Goal:** Fork GrapheneOS manifest, add DollOS project entries, sync full source tree.

**Files:**
- Create: `dollos/platform_manifest` repository (on GitHub/GitLab)
- Create: `docs/fork-point.md`

- [ ] **Step 1: Fork GrapheneOS manifest on GitHub**

On GitHub/GitLab, fork `https://github.com/GrapheneOS/platform_manifest.git` to `dollos/platform_manifest`.

- [ ] **Step 2: Clone and identify fork point**

```bash
git clone https://github.com/dollos/platform_manifest.git ~/dollos-manifest
cd ~/dollos-manifest
git checkout 16
git log --oneline -1
# Record this tag/commit hash
```

Save the tag to `~/Desktop/DollOS/docs/fork-point.md`.

- [ ] **Step 3: Add DollOS project entries to manifest**

Edit `~/dollos-manifest/default.xml` to add DollOS-specific repos. Add before `</manifest>`:

```xml
<!-- DollOS custom repositories -->
<project path="device/dollos/bluejay" name="dollos/device_dollos_bluejay" remote="dollos" revision="main" />
<project path="packages/apps/DollOSService" name="dollos/platform_packages_apps_DollOSService" remote="dollos" revision="main" />
<project path="packages/apps/DollOSSetupWizard" name="dollos/platform_packages_apps_DollOSSetupWizard" remote="dollos" revision="main" />
<project path="vendor/dollos" name="dollos/branding" remote="dollos" revision="main" />
```

Also add a `<remote>` entry for DollOS:

```xml
<remote name="dollos" fetch="https://github.com/dollos" />
```

- [ ] **Step 4: Commit and push manifest changes**

```bash
cd ~/dollos-manifest
git add default.xml
git commit -m "feat: add DollOS custom repository entries to manifest"
git push origin 16
```

- [ ] **Step 5: Initialize and sync source tree**

```bash
mkdir -p ~/dollos-build
cd ~/dollos-build
repo init -u https://github.com/dollos/platform_manifest.git -b 16 --depth=1
repo sync -j$(nproc) --no-tags --no-clone-bundle
```

Expected: Full AOSP/GrapheneOS source tree synced (~90GB+). DollOS repos will fail to sync until they are created (Tasks 4-9), which is expected at this stage.

- [ ] **Step 6: Verify source tree structure**

```bash
ls ~/dollos-build/build/
ls ~/dollos-build/frameworks/base/
ls ~/dollos-build/system/sepolicy/
ls ~/dollos-build/packages/apps/
```

Expected: Standard AOSP directory structure present.

- [ ] **Step 7: Commit fork point doc**

```bash
cd ~/Desktop/DollOS
git add docs/fork-point.md
git commit -m "docs: record GrapheneOS fork point for DollOS Base"
```

---

## Task 3: First Unmodified Build and Flash Verification

**Goal:** Build an unmodified GrapheneOS image for Pixel 6a and flash it to verify the entire build pipeline.

**Files:**
- None (verification only)

- [ ] **Step 1: Extract vendor binaries**

```bash
cd ~/dollos-build
source build/envsetup.sh
yarn --cwd vendor/adevtool/ install

# adevtool requires a factory OTA image or a connected device
# Download the latest bluejay factory image from GrapheneOS releases
# then extract vendor binaries from it:
node vendor/adevtool/bin/adevtool generate-all -d bluejay -b ~/dollos-build
```

If the above fails, consult GrapheneOS build docs for the current `adevtool` invocation. The tool's CLI changes between versions. Alternative: connect a Pixel 6a running stock GrapheneOS via USB and use:

```bash
node vendor/adevtool/bin/adevtool generate-all -d bluejay -s
```

Expected: Vendor binaries extracted to `vendor/google_devices/bluejay/`.

- [ ] **Step 2: Configure and build**

```bash
source build/envsetup.sh
lunch bluejay-cur-userdebug
m -j$(nproc)
```

Expected: Build completes (1-3 hours first time). Output in `out/target/product/bluejay/`.

- [ ] **Step 3: Verify build outputs**

```bash
ls -lh ~/dollos-build/out/target/product/bluejay/system.img
ls -lh ~/dollos-build/out/target/product/bluejay/boot.img
ls -lh ~/dollos-build/out/target/product/bluejay/vendor.img
```

Expected: All partition images present.

- [ ] **Step 4: Flash to Pixel 6a (REQUIRED)**

```bash
# Prerequisite: unlock bootloader (one-time, Developer Options > OEM Unlocking,
# then reboot to fastboot and: fastboot flashing unlock)
cd ~/dollos-build/out/target/product/bluejay/
fastboot flashall -w
```

Expected: Device boots into unmodified GrapheneOS. This confirms the build pipeline is fully functional before any DollOS modifications.

- [ ] **Step 5: Verify device boots and functions**

Check: home screen loads, Wi-Fi connects, Settings > About shows GrapheneOS version.

---

## Task 4: DollOS Device Configuration for Pixel 6a

**Goal:** Create device config that inherits GrapheneOS's bluejay config and provides the DollOS lunch target.

**Files:**
- Create: `device/dollos/bluejay/AndroidProducts.mk`
- Create: `device/dollos/bluejay/dollos_bluejay.mk`
- Create: `device/dollos/bluejay/BoardConfig.mk`
- Create: `device/dollos/bluejay/sepolicy/` (empty, populated in Task 7)

- [ ] **Step 1: Create directory structure**

```bash
cd ~/dollos-build
mkdir -p device/dollos/bluejay/sepolicy
```

- [ ] **Step 2: Create AndroidProducts.mk**

Create `device/dollos/bluejay/AndroidProducts.mk`:

```makefile
PRODUCT_MAKEFILES := \
    $(LOCAL_DIR)/dollos_bluejay.mk

COMMON_LUNCH_CHOICES := \
    dollos_bluejay-cur-user \
    dollos_bluejay-cur-userdebug
```

- [ ] **Step 3: Create product makefile**

Create `device/dollos/bluejay/dollos_bluejay.mk`:

```makefile
# Inherit from GrapheneOS bluejay configuration
$(call inherit-product, device/google/bluejay/aosp_bluejay.mk)

# DollOS identity
PRODUCT_NAME := dollos_bluejay
PRODUCT_DEVICE := bluejay
PRODUCT_BRAND := DollOS
PRODUCT_MODEL := DollOS on Pixel 6a
PRODUCT_MANUFACTURER := DollOS

# DollOS version
DOLLOS_VERSION := 0.1.0
PRODUCT_PROPERTY_OVERRIDES += \
    ro.dollos.version=$(DOLLOS_VERSION) \
    ro.build.display.id=DollOS-$(DOLLOS_VERSION)

# DollOS packages (added in Tasks 8, 9)
PRODUCT_PACKAGES += \
    DollOSService \
    DollOSSetupWizard

# Privapp permissions
PRODUCT_COPY_FILES += \
    packages/apps/DollOSService/privapp-permissions-dollos-service.xml:$(TARGET_COPY_OUT_SYSTEM)/etc/permissions/privapp-permissions-dollos-service.xml \
    packages/apps/DollOSSetupWizard/privapp-permissions-dollos-setup.xml:$(TARGET_COPY_OUT_SYSTEM)/etc/permissions/privapp-permissions-dollos-setup.xml

# DollOS branding overlay
PRODUCT_PACKAGE_OVERLAYS += vendor/dollos/branding/overlay
```

- [ ] **Step 4: Create BoardConfig.mk**

Create `device/dollos/bluejay/BoardConfig.mk`:

```makefile
# Inherit from GrapheneOS bluejay board config
include device/google/bluejay/BoardConfig.mk

# DollOS SELinux policy (device-specific additions, NOT system/sepolicy/private)
BOARD_SEPOLICY_DIRS += device/dollos/bluejay/sepolicy
```

- [ ] **Step 5: Verify lunch target**

```bash
cd ~/dollos-build
source build/envsetup.sh
lunch dollos_bluejay-cur-userdebug
```

Expected: Build target configured for DollOS Pixel 6a.

- [ ] **Step 6: Init git repo and commit**

```bash
cd ~/dollos-build/device/dollos
git init
git add .
git commit -m "feat: add DollOS device configuration for Pixel 6a (bluejay)"
```

Push to `dollos/device_dollos_bluejay` on GitHub/GitLab.

---

## Task 5: Generate DollOS Signing Keys

**Goal:** Generate a complete set of signing keys for DollOS builds.

**Files:**
- Create: `~/dollos-keys/bluejay/` (secure location, NOT in any repo)
- Create: `docs/key-management.md`

- [ ] **Step 1: Create key directory**

```bash
mkdir -p ~/dollos-keys/bluejay
cd ~/dollos-keys/bluejay
```

- [ ] **Step 2: Generate APK signing keys**

```bash
CN=DollOS
for key in releasekey platform shared media networkstack bluetooth sdk_sandbox gmscompat_lib nfc; do
    ~/dollos-build/development/tools/make_key "$key" "/CN=$CN/"
done
```

Expected: 9 pairs of `.pk8` + `.x509.pem` files. All keys use the SAME password (GrapheneOS script requirement).

Verify against actual `finalize.sh` requirements:

```bash
grep -oP '\w+\.pk8' ~/dollos-build/script/finalize.sh | sort -u
# Compare with generated keys, add any missing ones
```

- [ ] **Step 3: Generate AVB key**

```bash
openssl genrsa 4096 | openssl pkcs8 -topk8 -scrypt -out avb.pem
~/dollos-build/external/avb/avbtool.py extract_public_key --key avb.pem --output avb_pkmd.bin
```

- [ ] **Step 4: Generate OTA update signing key**

```bash
# GrapheneOS uses openssl for OTA signing, NOT ssh-keygen
openssl ecparam -name prime256v1 -genkey -noout -out ota.pem
openssl pkcs8 -topk8 -scrypt -in ota.pem -out ota_key.pem
openssl ec -in ota.pem -pubout -out ota_pub.pem
```

Expected: `ota_key.pem` (private) and `ota_pub.pem` (public) for OTA signing.

- [ ] **Step 5: Encrypt and backup**

```bash
cd ~/dollos-keys
~/dollos-build/script/encrypt-keys bluejay
```

Store securely. Create offline backup. Never commit private keys.

- [ ] **Step 6: Document and commit**

Create `docs/key-management.md` (procedures only, no key content).

```bash
cd ~/Desktop/DollOS
git add docs/key-management.md
git commit -m "docs: add signing key management guide"
```

---

## Task 6: DollOS Branding

**Goal:** Fork GrapheneOS branding and replace with DollOS identity.

**Files:**
- Fork: `vendor/grapheneos/` -> `vendor/dollos/`

- [ ] **Step 1: Fork branding repo on GitHub**

On GitHub/GitLab, fork `GrapheneOS/branding` (or the equivalent vendor repo) to `dollos/branding`.

```bash
cd ~/dollos-build
git clone https://github.com/dollos/branding.git vendor/dollos
```

- [ ] **Step 2: Update product name and fingerprint**

Edit `vendor/dollos/brand.mk` (or equivalent):

```makefile
PRODUCT_BRAND := DollOS
PRODUCT_MODEL := DollOS
PRODUCT_MANUFACTURER := DollOS
```

- [ ] **Step 3: Replace boot animation**

Replace the boot animation PNGs in `vendor/dollos/bootanimation/` with DollOS branding. For now, a simple placeholder with "DollOS" text is fine. The animation format is a zip of numbered PNG frames + a `desc.txt` descriptor.

- [ ] **Step 4: Update About page strings**

Search and replace "GrapheneOS" references in `vendor/dollos/` with "DollOS".

- [ ] **Step 5: Commit and push**

```bash
cd ~/dollos-build/vendor/dollos
git add -A
git commit -m "feat: replace GrapheneOS branding with DollOS identity"
git push origin main
```

---

## Task 7: SELinux Policy for DollOS

**Goal:** Define SELinux policy for DollOS system services and data directories.

**Files:**
- Create: `device/dollos/bluejay/sepolicy/dollos_data_file.te`
- Create: `device/dollos/bluejay/sepolicy/dollos_service.te`
- Create: `device/dollos/bluejay/sepolicy/file_contexts`
- Create: `device/dollos/bluejay/sepolicy/seapp_contexts`

NOTE: Policy goes in `device/dollos/bluejay/sepolicy/` (added to `BOARD_SEPOLICY_DIRS` in Task 4), NOT directly in `system/sepolicy/private/`. This avoids conflicts with upstream.

- [ ] **Step 1: Define dollos_data_file type**

Create `device/dollos/bluejay/sepolicy/dollos_data_file.te`:

```te
type dollos_data_file, file_type, data_file_type;
```

- [ ] **Step 2: Define dollos_service domain**

Create `device/dollos/bluejay/sepolicy/dollos_service.te`:

```te
# DollOS Service domain -- persistent platform-signed priv-app
# Uses app_domain() for Zygote-launched process. No coredomain (reserved for init/vold/system_server).
type dollos_service, domain;

app_domain(dollos_service)
net_domain(dollos_service)

# File access: read/write /data/dollos/
allow dollos_service dollos_data_file:dir create_dir_perms;
allow dollos_service dollos_data_file:file create_file_perms;

# Binder IPC
binder_use(dollos_service)
binder_service(dollos_service)

# Allow callers
allow system_app dollos_service:binder { call transfer };
allow untrusted_app dollos_service:binder { call transfer };
allow dollos_service dollos_service:binder { call transfer };

# Network access (for cloud LLM API calls)
allow dollos_service self:tcp_socket { create connect read write getattr setopt };
allow dollos_service port_type:tcp_socket name_connect;
```

- [ ] **Step 3: Define file contexts**

Create `device/dollos/bluejay/sepolicy/file_contexts`:

```
# DollOS data directories
/data/dollos(/.*)?    u:object_r:dollos_data_file:s0
```

- [ ] **Step 4: Define seapp_contexts**

Create `device/dollos/bluejay/sepolicy/seapp_contexts`:

```
# DollOS persistent system service (platform-signed, running as system user)
user=system seinfo=platform name=org.dollos.service domain=dollos_service type=dollos_data_file
```

Note: no `levelFrom` field -- the default (`none`) is correct for a simple platform service.

- [ ] **Step 5: Build and verify SELinux policy compiles**

```bash
cd ~/dollos-build
source build/envsetup.sh
lunch dollos_bluejay-cur-userdebug
m selinux_policy -j$(nproc)
```

Expected: SELinux policy compiles without errors. If `neverallow` violations occur, review the domain attributes and consult AOSP's `system/sepolicy/private/` for the correct macro usage.

- [ ] **Step 6: Commit and push**

```bash
cd ~/dollos-build/device/dollos
git add bluejay/sepolicy/
git commit -m "feat: add SELinux policy for DollOS service domain and data directories"
git push origin main
```

---

## Task 8: DollOS System Service Skeleton

**Goal:** Create a persistent system service that starts on boot, creates `/data/dollos/` directories, and exposes a Binder IPC interface including API key management.

**Files:**
- Create: `packages/apps/DollOSService/Android.bp`
- Create: `packages/apps/DollOSService/AndroidManifest.xml`
- Create: `packages/apps/DollOSService/privapp-permissions-dollos-service.xml`
- Create: `packages/apps/DollOSService/aidl/org/dollos/service/IDollOSService.aidl`
- Create: `packages/apps/DollOSService/src/org/dollos/service/DollOSService.kt`
- Create: `packages/apps/DollOSService/src/org/dollos/service/DollOSServiceImpl.kt`

- [ ] **Step 1: Create AIDL interface**

Create `packages/apps/DollOSService/aidl/org/dollos/service/IDollOSService.aidl`:

```aidl
package org.dollos.service;

interface IDollOSService {
    /** Returns the DollOS version string */
    String getVersion();

    /** Returns true if the AI core is configured (API key set) */
    boolean isAiConfigured();

    /** Returns the path to the DollOS data directory */
    String getDataDirectory();

    /** Store API key configuration (called by OOBE wizard via Binder) */
    void setApiKey(String provider, String apiKey);

    /** Store GMS opt-in preference (called by OOBE wizard via Binder) */
    void setGmsOptIn(boolean optIn);

    /** Check if user opted into GMS */
    boolean isGmsOptedIn();
}
```

- [ ] **Step 2: Create Android.bp**

Create `packages/apps/DollOSService/Android.bp`:

```json
android_app {
    name: "DollOSService",
    srcs: [
        "src/**/*.kt",
    ],
    aidl: {
        local_include_dirs: ["aidl"],
        srcs: ["aidl/**/*.aidl"],
    },
    platform_apis: true,
    privileged: true,
    certificate: "platform",
    static_libs: [
        "androidx.core_core-ktx",
    ],
    kotlincflags: ["-Xjvm-default=all"],
}
```

- [ ] **Step 3: Create privapp-permissions XML**

Create `packages/apps/DollOSService/privapp-permissions-dollos-service.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<permissions>
    <privapp-permissions package="org.dollos.service">
        <permission name="android.permission.INTERNET" />
        <permission name="android.permission.RECEIVE_BOOT_COMPLETED" />
    </privapp-permissions>
</permissions>
```

- [ ] **Step 4: Create AndroidManifest.xml**

Create `packages/apps/DollOSService/AndroidManifest.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="org.dollos.service">

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />

    <application
        android:label="DollOS Service"
        android:persistent="true"
        android:directBootAware="true">

        <service
            android:name=".DollOSService"
            android:exported="true"
            android:enabled="true">
            <intent-filter>
                <action android:name="org.dollos.service.IDollOSService" />
            </intent-filter>
        </service>

    </application>
</manifest>
```

- [ ] **Step 5: Implement the service**

Create `packages/apps/DollOSService/src/org/dollos/service/DollOSService.kt`:

```kotlin
package org.dollos.service

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log
import java.io.File

class DollOSService : Service() {

    companion object {
        private const val TAG = "DollOSService"
        const val VERSION = "0.1.0"
        const val DATA_DIR = "/data/dollos"
    }

    private val binder = DollOSServiceImpl()

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "DollOS Service starting, version $VERSION")
        initDataDirectories()
    }

    override fun onBind(intent: Intent?): IBinder {
        return binder
    }

    private fun initDataDirectories() {
        val dirs = listOf(
            "$DATA_DIR/ai",
            "$DATA_DIR/avatar",
            "$DATA_DIR/voice",
            "$DATA_DIR/config"
        )
        for (path in dirs) {
            val dir = File(path)
            if (!dir.exists()) {
                val created = dir.mkdirs()
                Log.i(TAG, "Created directory $path: $created")
            }
        }
    }
}
```

- [ ] **Step 6: Implement the Binder interface**

Create `packages/apps/DollOSService/src/org/dollos/service/DollOSServiceImpl.kt`:

```kotlin
package org.dollos.service

import android.util.Log
import java.io.File
import org.json.JSONObject

class DollOSServiceImpl : IDollOSService.Stub() {

    companion object {
        private const val TAG = "DollOSServiceImpl"
    }

    override fun getVersion(): String {
        return DollOSService.VERSION
    }

    override fun isAiConfigured(): Boolean {
        val configFile = File("${DollOSService.DATA_DIR}/config/api_key.json")
        return configFile.exists() && configFile.length() > 0
    }

    override fun getDataDirectory(): String {
        return DollOSService.DATA_DIR
    }

    override fun setApiKey(provider: String, apiKey: String) {
        // Encrypt API key using Android KeyStore before writing to disk
        val encryptedKey = encryptWithKeyStore(apiKey)
        val config = JSONObject().apply {
            put("provider", provider)
            put("api_key_encrypted", encryptedKey)
        }
        val configFile = File("${DollOSService.DATA_DIR}/config/api_key.json")
        configFile.writeText(config.toString())
        Log.i(TAG, "API key saved for provider: $provider")
    }

    private fun encryptWithKeyStore(plaintext: String): String {
        val keyStore = java.security.KeyStore.getInstance("AndroidKeyStore")
        keyStore.load(null)

        val keyAlias = "dollos_api_key"
        if (!keyStore.containsAlias(keyAlias)) {
            val keyGenerator = javax.crypto.KeyGenerator.getInstance(
                javax.crypto.KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore"
            )
            keyGenerator.init(
                android.security.keystore.KeyGenParameterSpec.Builder(
                    keyAlias,
                    android.security.keystore.KeyProperties.PURPOSE_ENCRYPT or
                        android.security.keystore.KeyProperties.PURPOSE_DECRYPT
                )
                    .setBlockModes(android.security.keystore.KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(android.security.keystore.KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build()
            )
            keyGenerator.generateKey()
        }

        val key = keyStore.getKey(keyAlias, null) as javax.crypto.SecretKey
        val cipher = javax.crypto.Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(javax.crypto.Cipher.ENCRYPT_MODE, key)
        val iv = cipher.iv
        val encrypted = cipher.doFinal(plaintext.toByteArray())

        // Store IV + ciphertext as base64
        val combined = iv + encrypted
        return android.util.Base64.encodeToString(combined, android.util.Base64.NO_WRAP)
    }

    override fun setGmsOptIn(optIn: Boolean) {
        val configFile = File("${DollOSService.DATA_DIR}/config/gms_optin.json")
        val config = JSONObject().apply {
            put("opted_in", optIn)
        }
        configFile.writeText(config.toString())
        Log.i(TAG, "GMS opt-in set to: $optIn")
    }

    override fun isGmsOptedIn(): Boolean {
        val configFile = File("${DollOSService.DATA_DIR}/config/gms_optin.json")
        if (!configFile.exists()) return false
        val config = JSONObject(configFile.readText())
        return config.optBoolean("opted_in", false)
    }
}
```

NOTE: The OOBE wizard and other apps write config through this Binder interface, NOT by writing files directly to `/data/dollos/`. Only the DollOS service process has SELinux permission to write to `dollos_data_file`.

- [ ] **Step 7: Build and verify**

```bash
cd ~/dollos-build
source build/envsetup.sh
lunch dollos_bluejay-cur-userdebug
m DollOSService -j$(nproc)
```

Expected: `DollOSService.apk` built in `out/target/product/bluejay/system/priv-app/DollOSService/`.

- [ ] **Step 8: Init git repo, commit, push**

```bash
cd ~/dollos-build/packages/apps/DollOSService
git init
git add .
git commit -m "feat: add DollOS persistent system service with AIDL IPC interface"
```

Push to `dollos/platform_packages_apps_DollOSService` on GitHub/GitLab.

---

## Task 9: Custom OOBE Setup Wizard (Skeleton)

**Goal:** Create an 8-page OOBE wizard that replaces GrapheneOS's SetupWizard2. Skeleton pages with working navigation; full functionality (Wi-Fi picker, model download) deferred to later tasks. GMS opt-in and API key are stored via DollOS Service Binder IPC.

**Files:**
- Create: `packages/apps/DollOSSetupWizard/Android.bp`
- Create: `packages/apps/DollOSSetupWizard/AndroidManifest.xml`
- Create: `packages/apps/DollOSSetupWizard/privapp-permissions-dollos-setup.xml`
- Create: `packages/apps/DollOSSetupWizard/res/layout/*.xml`
- Create: `packages/apps/DollOSSetupWizard/src/org/dollos/setup/*.kt`

- [ ] **Step 1: Create Android.bp**

Create `packages/apps/DollOSSetupWizard/Android.bp`:

```json
android_app {
    name: "DollOSSetupWizard",
    srcs: [
        "src/**/*.kt",
    ],
    resource_dirs: ["res"],
    platform_apis: true,
    privileged: true,
    certificate: "platform",
    overrides: ["SetupWizard2", "Provision"],
    static_libs: [
        "androidx.core_core-ktx",
        "androidx.appcompat_appcompat",
        "com.google.android.material_material",
    ],
    kotlincflags: ["-Xjvm-default=all"],
}
```

- [ ] **Step 2: Create privapp-permissions XML**

Create `packages/apps/DollOSSetupWizard/privapp-permissions-dollos-setup.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<permissions>
    <privapp-permissions package="org.dollos.setup">
        <permission name="android.permission.INTERNET" />
        <permission name="android.permission.ACCESS_WIFI_STATE" />
        <permission name="android.permission.CHANGE_WIFI_STATE" />
        <permission name="android.permission.WRITE_SETTINGS" />
        <permission name="android.permission.WRITE_SECURE_SETTINGS" />
    </privapp-permissions>
</permissions>
```

- [ ] **Step 3: Create AndroidManifest.xml**

Create `packages/apps/DollOSSetupWizard/AndroidManifest.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="org.dollos.setup">

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_WIFI_STATE" />
    <uses-permission android:name="android.permission.CHANGE_WIFI_STATE" />
    <uses-permission android:name="android.permission.WRITE_SETTINGS" />
    <uses-permission android:name="android.permission.WRITE_SECURE_SETTINGS" />

    <application
        android:label="DollOS Setup"
        android:theme="@style/Theme.MaterialComponents.DayNight.NoActionBar">

        <activity
            android:name=".SetupWizardActivity"
            android:exported="true"
            android:immersive="true"
            android:windowSoftInputMode="stateAlwaysHidden">
            <intent-filter android:priority="5">
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.SETUP_WIZARD" />
            </intent-filter>
        </activity>

    </application>
</manifest>
```

- [ ] **Step 4: Create layout files**

Create `packages/apps/DollOSSetupWizard/res/layout/activity_setup_wizard.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:id="@+id/page_container"
    android:layout_width="match_parent"
    android:layout_height="match_parent" />
```

Create a reusable page layout template `res/layout/page_generic.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:padding="32dp"
    android:gravity="center">

    <TextView
        android:id="@+id/title"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:textSize="28sp"
        android:textStyle="bold" />

    <TextView
        android:id="@+id/description"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:layout_marginTop="16dp"
        android:textSize="16sp" />

    <Space
        android:layout_width="match_parent"
        android:layout_height="0dp"
        android:layout_weight="1" />

    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:orientation="horizontal"
        android:gravity="end">

        <Button
            android:id="@+id/btn_back"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="Back"
            android:visibility="gone"
            style="?attr/materialButtonOutlinedStyle" />

        <Button
            android:id="@+id/btn_skip"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="Skip"
            android:visibility="gone"
            android:layout_marginStart="8dp"
            style="?attr/materialButtonOutlinedStyle" />

        <Button
            android:id="@+id/btn_next"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="Next"
            android:layout_marginStart="8dp" />

    </LinearLayout>
</LinearLayout>
```

Create additional page-specific layouts (e.g., `page_api_key.xml` with EditText fields, `page_gms.xml` with a Switch) as variations of the generic template.

- [ ] **Step 5: Create SetupWizardActivity.kt**

Create `packages/apps/DollOSSetupWizard/src/org/dollos/setup/SetupWizardActivity.kt`:

```kotlin
package org.dollos.setup

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.IBinder
import android.provider.Settings
import androidx.appcompat.app.AppCompatActivity
import org.dollos.service.IDollOSService

class SetupWizardActivity : AppCompatActivity() {

    private val pageKeys = listOf(
        "welcome", "wifi", "gms", "model_download",
        "api_key", "personality", "voice", "complete"
    )

    private var currentPageIndex = 0
    var dollOSService: IDollOSService? = null
        private set
    private var isBound = false

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            dollOSService = IDollOSService.Stub.asInterface(service)
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            dollOSService = null
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_setup_wizard)

        // Bind to DollOS Service for config storage
        val intent = Intent("org.dollos.service.IDollOSService")
        intent.setPackage("org.dollos.service")
        isBound = bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)

        showPage(0)
    }

    override fun onDestroy() {
        super.onDestroy()
        if (isBound) {
            unbindService(serviceConnection)
            isBound = false
        }
    }

    fun nextPage() {
        if (currentPageIndex < pageKeys.size - 1) {
            currentPageIndex++
            showPage(currentPageIndex)
        } else {
            finishSetup()
        }
    }

    fun previousPage() {
        if (currentPageIndex > 0) {
            currentPageIndex--
            showPage(currentPageIndex)
        }
    }

    fun skipToPage(pageIndex: Int) {
        currentPageIndex = pageIndex
        showPage(currentPageIndex)
    }

    fun getPageIndex(key: String): Int = pageKeys.indexOf(key)

    private fun showPage(index: Int) {
        val fragment = when (pageKeys[index]) {
            "welcome" -> WelcomePage()
            "wifi" -> WifiPage()
            "gms" -> GmsPage()
            "model_download" -> ModelDownloadPage()
            "api_key" -> ApiKeyPage()
            "personality" -> PersonalityPage()
            "voice" -> VoicePage()
            "complete" -> CompletePage()
            else -> WelcomePage()
        }
        supportFragmentManager.beginTransaction()
            .replace(R.id.page_container, fragment)
            .commit()
    }

    private fun finishSetup() {
        Settings.Global.putInt(contentResolver, Settings.Global.DEVICE_PROVISIONED, 1)
        Settings.Secure.putInt(contentResolver, Settings.Secure.USER_SETUP_COMPLETE, 1)

        val intent = Intent(Intent.ACTION_MAIN)
        intent.addCategory(Intent.CATEGORY_HOME)
        intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK
        startActivity(intent)
        finish()
    }
}
```

- [ ] **Step 6: Create page fragments using AndroidX Fragment**

All pages extend `androidx.fragment.app.Fragment` (NOT `android.app.Fragment`).

Create `packages/apps/DollOSSetupWizard/src/org/dollos/setup/WelcomePage.kt`:

```kotlin
package org.dollos.setup

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.TextView
import androidx.fragment.app.Fragment

class WelcomePage : Fragment() {
    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?
    ): View {
        val view = inflater.inflate(R.layout.page_generic, container, false)
        view.findViewById<TextView>(R.id.title).text = "Welcome to DollOS"
        view.findViewById<TextView>(R.id.description).text = "Your AI companion lives here."
        view.findViewById<Button>(R.id.btn_next).setOnClickListener {
            (activity as SetupWizardActivity).nextPage()
        }
        // No back button on first page
        return view
    }
}
```

Create `GmsPage.kt` -- writes GMS opt-in via Binder IPC:

```kotlin
package org.dollos.setup

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.Switch
import android.widget.TextView
import androidx.fragment.app.Fragment

class GmsPage : Fragment() {
    private var gmsOptIn = false

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?
    ): View {
        val view = inflater.inflate(R.layout.page_generic, container, false)
        view.findViewById<TextView>(R.id.title).text = "Google Play Services"
        view.findViewById<TextView>(R.id.description).text =
            "Install sandboxed Google Play? Google will have no special system privileges."

        // TODO: replace with proper Switch layout
        view.findViewById<Button>(R.id.btn_next).setOnClickListener {
            val wizard = activity as SetupWizardActivity
            wizard.dollOSService?.setGmsOptIn(gmsOptIn)
            wizard.nextPage()
        }
        view.findViewById<Button>(R.id.btn_back).apply {
            visibility = View.VISIBLE
            setOnClickListener { (activity as SetupWizardActivity).previousPage() }
        }
        return view
    }
}
```

Create `ApiKeyPage.kt` -- writes API key via Binder IPC, with skip support:

```kotlin
package org.dollos.setup

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.fragment.app.Fragment

class ApiKeyPage : Fragment() {
    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?
    ): View {
        val view = inflater.inflate(R.layout.page_generic, container, false)
        view.findViewById<TextView>(R.id.title).text = "AI Configuration"
        view.findViewById<TextView>(R.id.description).text =
            "Enter your LLM API key. You can skip this and configure later in Settings."

        // TODO: add EditText fields for provider selection and API key input

        val wizard = activity as SetupWizardActivity

        view.findViewById<Button>(R.id.btn_next).setOnClickListener {
            // TODO: read provider + key from EditText, call wizard.dollOSService?.setApiKey(provider, key)
            wizard.nextPage()
        }
        view.findViewById<Button>(R.id.btn_skip).apply {
            visibility = View.VISIBLE
            setOnClickListener {
                // Skip personality page too (it depends on AI being configured)
                wizard.skipToPage(wizard.getPageIndex("voice"))
            }
        }
        view.findViewById<Button>(R.id.btn_back).apply {
            visibility = View.VISIBLE
            setOnClickListener { wizard.previousPage() }
        }
        return view
    }
}
```

Create remaining pages (`WifiPage.kt`, `ModelDownloadPage.kt`, `PersonalityPage.kt`, `VoicePage.kt`, `CompletePage.kt`) following the same `androidx.fragment.app.Fragment` pattern. Each shows title + description + next/back buttons. `CompletePage` calls `(activity as SetupWizardActivity).nextPage()` which triggers `finishSetup()`.

- [ ] **Step 7: Build and verify**

```bash
cd ~/dollos-build
source build/envsetup.sh
lunch dollos_bluejay-cur-userdebug
m DollOSSetupWizard -j$(nproc)
```

Expected: `DollOSSetupWizard.apk` built successfully.

- [ ] **Step 8: Init git repo, commit, push**

```bash
cd ~/dollos-build/packages/apps/DollOSSetupWizard
git init
git add .
git commit -m "feat: add DollOS OOBE setup wizard skeleton with 8-page flow"
```

Push to `dollos/platform_packages_apps_DollOSSetupWizard` on GitHub/GitLab.

---

## Task 10: Fork and Configure Release Scripts

**Goal:** Fork GrapheneOS's `script/` repo and configure it for DollOS signing keys and product names.

**Files:**
- Fork: `GrapheneOS/script` -> `dollos/script`

- [ ] **Step 1: Fork script repo on GitHub**

Fork `https://github.com/GrapheneOS/script.git` to `dollos/script`.

```bash
cd ~/dollos-build
git clone https://github.com/dollos/script.git script-dollos
# Or update the existing script/ directory if it's already synced
```

- [ ] **Step 2: Update key paths**

Edit `script-dollos/finalize.sh` and `script-dollos/generate-release.sh` to reference DollOS key locations:

- Change key directory references from GrapheneOS paths to `~/dollos-keys/`
- Change product/brand name references from "GrapheneOS" to "DollOS"

- [ ] **Step 3: Verify finalize.sh runs**

```bash
cd ~/dollos-build
source build/envsetup.sh
lunch dollos_bluejay-cur-userdebug
m target-files-package otatools-package -j$(nproc)

# Test signing with DollOS keys
./script-dollos/finalize.sh
```

Expected: Target files signed with DollOS keys.

- [ ] **Step 4: Commit and push**

```bash
cd ~/dollos-build/script-dollos
git add -A
git commit -m "feat: configure release scripts for DollOS signing keys and product name"
git push origin main
```

---

## Task 11: Full Build, Flash, and Verification

**Goal:** Build the complete DollOS image with all customizations, sign it, and verify on Pixel 6a.

**Files:**
- Create: `docs/verification-v0.1.0.md`

- [ ] **Step 1: Ensure all DollOS repos are in the source tree**

```bash
cd ~/dollos-build
# Verify all DollOS directories exist
ls device/dollos/bluejay/
ls packages/apps/DollOSService/
ls packages/apps/DollOSSetupWizard/
ls vendor/dollos/
```

If any are missing, clone them manually or run `repo sync` after the manifest is updated.

- [ ] **Step 2: Clean build**

```bash
source build/envsetup.sh
lunch dollos_bluejay-cur-userdebug
rm -rf out
m -j$(nproc)
```

Expected: Full build completes.

- [ ] **Step 3: Sign and generate factory image**

```bash
m target-files-package otatools-package -j$(nproc)
./script-dollos/finalize.sh
./script-dollos/generate-release.sh bluejay $(date +%Y%m%d%H)
```

Expected: Signed factory image zip created.

- [ ] **Step 4: Flash to Pixel 6a**

```bash
# Boot to fastboot: power + volume down
fastboot flashall -w
```

Expected: Device boots into DollOS.

- [ ] **Step 5: Verify checklist**

| Check | Command | Expected |
|-------|---------|----------|
| Boot animation | Visual check | DollOS branding |
| OOBE wizard | First boot | DollOS setup wizard appears |
| OOBE navigation | Tap through all pages | All 8 pages work, skip/back functional |
| GMS opt-in persisted | `adb shell cat /data/dollos/config/gms_optin.json` | `{"opted_in":true}` or `false` |
| Settings > About | Navigate in Settings | Shows "DollOS 0.1.0" |
| DollOSService running | `adb shell dumpsys activity services org.dollos.service` | Service listed as running |
| /data/dollos/ exists | `adb shell ls /data/dollos/` | ai/, avatar/, voice/, config/ |
| SELinux enforcing | `adb shell getenforce` | "Enforcing" |
| SELinux context | `adb shell ls -Z /data/dollos/` | `u:object_r:dollos_data_file:s0` |
| Binder IPC works | `adb shell service call org.dollos.service 1` | Returns version string |

- [ ] **Step 6: Document results and tag**

Create `docs/verification-v0.1.0.md` with test results.

```bash
cd ~/Desktop/DollOS
git add docs/verification-v0.1.0.md
git commit -m "docs: add v0.1.0 verification results"
git tag v0.1.0
```

---

## Task 12: CI Upstream Tracking Setup (Placeholder)

**Goal:** Document the process for tracking GrapheneOS upstream updates and set up basic CI notification.

**Files:**
- Create: `docs/upstream-tracking.md`

- [ ] **Step 1: Document upstream tracking process**

Create `docs/upstream-tracking.md`:

```markdown
# GrapheneOS Upstream Tracking

## Manual Process (Current)

1. Check GrapheneOS releases page for new tags
2. In the DollOS manifest repo, update the base branch to the new GrapheneOS tag
3. `repo sync` and resolve merge conflicts in forked repos
4. Build and test

## Future CI Automation

- GitHub Action that checks GrapheneOS releases daily
- On new release: attempt `repo sync`, run build, notify developer
- Merge conflicts require human intervention
```

- [ ] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS
git add docs/upstream-tracking.md
git commit -m "docs: add upstream tracking process documentation"
```

---

## Notes

### Build Directory Layout

- `~/dollos-build/` -- AOSP/GrapheneOS source tree (managed by `repo`)
- `~/dollos-keys/` -- signing keys (secure, never in any repo)
- `~/Desktop/DollOS/` -- project documentation, specs, plans

### Upstream Repos Tracked by DollOS Manifest

The DollOS manifest inherits all GrapheneOS repo entries. Only repos that DollOS modifies need to be forked. Unmodified repos continue to point to GrapheneOS upstream.

### Next Sub-Project

After DollOS Base is stable, proceed to **AI Core** sub-project design (brainstorming -> spec -> plan -> implementation).
