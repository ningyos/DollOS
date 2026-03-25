# Build Environment Setup

This guide covers setting up a build environment for compiling DollOS, which is based on GrapheneOS Android 16 for Pixel devices.

## System Requirements

| Component | Minimum |
|-----------|---------|
| OS | Ubuntu 22.04 LTS or 24.04 LTS (64-bit) |
| RAM | 32 GB |
| Free disk space | 500 GB SSD (NVMe recommended) |
| Shell | bash or zsh |
| CPU | Modern multi-core (more cores = faster builds) |

Note: A full build takes approximately 1 to 3 hours on modern hardware. Incremental builds are significantly faster.

## Install Dependencies

Update the package list and install all required build tools in one step:

```bash
sudo apt update && sudo apt install -y \
    git \
    gnupg \
    curl \
    python3 \
    python3-pip \
    openjdk-21-jdk \
    zip \
    unzip \
    rsync \
    bison \
    flex \
    gcc \
    g++ \
    make \
    libssl-dev \
    libncurses-dev \
    gperf \
    xmlstarlet \
    fonttools \
    ninja-build \
    libxml2-utils \
    xsltproc \
    bc \
    imagemagick \
    build-essential \
    zlib1g-dev \
    fontconfig
```

Install the `repo` tool from Google:

```bash
mkdir -p ~/.local/bin
curl https://storage.googleapis.com/git-repo-downloads/repo -o ~/.local/bin/repo
chmod a+x ~/.local/bin/repo
```

Ensure `~/.local/bin` is on your PATH. Add this to `~/.bashrc` or `~/.zshrc` if it is not already present:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Reload your shell:

```bash
source ~/.bashrc   # or source ~/.zshrc
```

Verify the installation:

```bash
repo --version
java -version      # must report OpenJDK 21
python3 --version
```

## Configure Git Identity

AOSP build scripts require a Git identity. Set your name and email globally:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

## Directory Structure

Use the following layout to keep build artifacts and signing keys organized:

```
~/Projects/DollOS/          - Main repo (docs, design specs)
~/Projects/DollOS-build/    - GrapheneOS source tree and all build output
~/dollos-keys/              - Signing keys (see docs/key-management.md)
```

Create the directories:

```bash
mkdir -p ~/Projects/DollOS-build
mkdir -p ~/dollos-keys
```

## Initialize the Source Tree

DollOS uses the official GrapheneOS manifest with a `local_manifests` overlay to add DollOS-specific repositories.

```bash
cd ~/Projects/DollOS-build

# Use the official GrapheneOS manifest (Android 16)
repo init -u https://github.com/GrapheneOS/platform_manifest.git -b 16 --depth=1
```

Create the DollOS local manifest to pull in custom repos:

```bash
mkdir -p .repo/local_manifests
cat > .repo/local_manifests/dollos.xml << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="dollos" fetch="https://github.com/ningyos/" revision="refs/heads/main"/>
  <project path="packages/apps/DollOSService" name="DollOSService" remote="dollos"/>
  <project path="packages/apps/DollOSSetupWizard" name="DollOSSetupWizard" remote="dollos"/>
  <project path="vendor/dollos" name="vendor_dollos" remote="dollos"/>
  <project path="device/dollos/bluejay" name="device_dollos_bluejay" remote="dollos"/>
</manifest>
EOF
```

Sync the source tree:

```bash
repo sync -j$(nproc) --no-tags --no-clone-bundle
```

This syncs the full GrapheneOS source tree (~90GB+). Depending on network speed, this may take several hours.

## Extract Vendor Binaries

GrapheneOS handles vendor binary extraction via `adevtool`. No manual factory image download is needed.

```bash
cd ~/Projects/DollOS-build
vendor/adevtool/bin/adevtool generate-all -d bluejay
```

This automatically fetches and extracts the required vendor binaries for the target device.

## Build

```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-bp2a-userdebug
m -j$(nproc)
```

A full clean build takes 1 to 3 hours on modern hardware depending on core count and disk speed. Ensure the machine does not suspend during the build. Output images will be in `out/target/product/bluejay/`.

## Flash to Pixel 6a

Prerequisite: unlock the bootloader (one-time, Developer Options > OEM Unlocking, then reboot to fastboot and run `fastboot flashing unlock`).

```bash
cd ~/Projects/DollOS-build/out/target/product/bluejay/
fastboot flashall -w
```
