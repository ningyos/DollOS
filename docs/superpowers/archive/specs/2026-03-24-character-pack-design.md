# Character Pack System Design

## Overview

Character Pack is the core packaging format for DollOS AI characters. A single `.doll` file (zip archive) bundles everything that defines an AI character: 3D avatar model, personality prompts, voice settings, scene configuration, animations, wake word, and metadata. Users can import, export, and switch between multiple character packs.

## File Format

### Extension and Structure

File extension: `.doll` (zip archive internally)

```
character.doll
├── manifest.json          — metadata: name, version, author, format version
├── thumbnail.png          — character thumbnail (512x512 recommended)
├── personality.json       — backstory, response directive, dynamism, address, language
├── voice.json             — TTS settings (speed, pitch, model preference)
├── scene.json             — background, lighting, camera position
├── model.glb              — 3D Avatar (glTF 2.0 with skeleton)
├── animations/            — additional animation clips
│   ├── idle.glb
│   ├── talking.glb
│   ├── thinking.glb
│   └── ...
└── wake_word.bin          — wake word model (optional)
```

### manifest.json

```json
{
  "formatVersion": 1,
  "name": "Luna",
  "version": "1.0.0",
  "author": "creator name",
  "description": "A cheerful AI companion",
  "wakeWord": "hey luna",
  "avatarType": "3d",
  "created": "2026-03-24T00:00:00Z"
}
```

- `formatVersion`: integer, for future format evolution
- `avatarType`: `"3d"` or `"live2d"` (live2d support added later)
- `wakeWord`: optional, null if not provided
- All other fields required

### personality.json

```json
{
  "backstory": "Luna is a cheerful companion who...",
  "responseDirective": "Speak warmly and concisely",
  "dynamism": 0.6,
  "address": "主人",
  "languagePreference": "zh-TW"
}
```

Maps directly to existing PersonalityManager fields. On import, these values are written to DollOSAIService's personality settings.

### voice.json

```json
{
  "speed": 1.0,
  "pitch": 1.0,
  "ttsModel": "default",
  "language": "zh-TW"
}
```

Reserved for future Voice Pipeline integration. Stored but not actively used until TTS is implemented.

### scene.json

```json
{
  "background": {
    "type": "color",
    "value": "#0a0a1a"
  },
  "lighting": {
    "ambient": 0.3,
    "directional": {
      "intensity": 1.0,
      "direction": [0.0, -1.0, -0.5],
      "color": "#ffffff"
    }
  },
  "camera": {
    "position": [0.0, 1.2, 3.0],
    "target": [0.0, 1.0, 0.0],
    "fov": 45.0
  }
}
```

Background type can be `"color"`, `"gradient"`, or `"image"` (image file path relative to pack root).

## Architecture

### CharacterManager

Lives in DollOSAIService as an independent module.

Responsibilities:
- Import `.doll` files (unzip, validate, store)
- List installed characters
- Switch active character (apply personality, notify Launcher to reload avatar)
- Export character (zip current config + model into `.doll`)
- Delete character
- Provide active character info to other components via AIDL

Storage: each character extracted to `{app_internal}/characters/{character_id}/`

### AIDL Interface

Add to `IDollOSAIService.aidl`:

```
// Character Pack management
String importCharacter(in ParcelFileDescriptor fd);  // returns character ID, null on failure
void exportCharacter(String characterId, in ParcelFileDescriptor fd);
void setActiveCharacter(String characterId);
String getActiveCharacter();  // returns character ID
String listCharacters();  // returns JSON array
void deleteCharacter(String characterId);
String getCharacterInfo(String characterId);  // returns JSON
ParcelFileDescriptor getCharacterAsset(String characterId, String path);  // asset access for Launcher
```

Add to `IDollOSAICallback.aidl`:

```
void onCharacterChanged(String characterId, String characterName);
void onCharacterImportFailed(String errorCode, String message);
```

### Memory Model

- **Shared memory**: Core memory (MEMORY.md, daily/, deep/) is shared across all characters. User facts persist regardless of which character is active.
- **Character private notes**: Each character has a private notes file at `characters/{id}/notes.md`. Characters can write observations about the user here. Not searchable by other characters.
- On character switch: shared memory stays, private notes switch to new character's file.

### Integration Points

| Component | How it uses Character Pack |
|-----------|--------------------------|
| **Launcher** | Loads `model.glb`, `animations/`, `scene.json` for 3D rendering. Listens for `onCharacterChanged` to reload. |
| **PersonalityManager** | `setActiveCharacter()` writes `personality.json` values to personality settings |
| **Voice Pipeline** (future) | Reads `voice.json` for TTS config |
| **Wake Word** (future) | Loads `wake_word.bin` for wake word detection |
| **Settings UI** | Character picker in Memory Settings or dedicated Character Settings sub-page |

### Import Flow

1. User selects `.doll` file (via SAF file picker or file manager tap)
2. DollOSAIService receives `ParcelFileDescriptor`
3. CharacterManager validates zip structure (manifest.json must exist)
4. Extracts to `characters/{generated_id}/`
5. Returns character ID
6. User can then call `setActiveCharacter(id)` to activate

### Character Switch Flow

1. `setActiveCharacter(characterId)` called
2. CharacterManager:
   - Loads `personality.json` → writes to PersonalityManager
   - Saves active character ID to SharedPreferences
   - Switches private notes file
   - Broadcasts `onCharacterChanged` callback
3. Launcher receives callback:
   - Unloads current 3D model
   - Loads new `model.glb` + `animations/` + `scene.json`
   - Applies new scene settings (background, lighting, camera)
4. Wake Word (future): reloads `wake_word.bin`

### Character Switch During Active Conversation

`setActiveCharacter()` must:
1. Call `stopGeneration()` to cancel any active LLM streaming
2. Clear all pending confirmations (action confirms, memory confirms)
3. Cancel all background workers (they may have personality-dependent context)
4. Then proceed with the switch flow

If the caller wants to avoid disruption, check `getActiveTasks()` first and warn the user.

### Asset Access for Other Apps

Character assets (model.glb, animations, scene.json) are stored in DollOSAIService's internal storage. Other apps (e.g., Launcher) cannot access this directly. CharacterManager provides asset access via AIDL:

```
ParcelFileDescriptor getCharacterAsset(String characterId, String path);
```

Where `path` is relative to the character directory (e.g., `"model.glb"`, `"animations/idle.glb"`, `"scene.json"`). The Launcher calls this to load 3D assets.

### File Association

Register `.doll` file type via content URI scheme (file URIs blocked on Android 7+):

```xml
<intent-filter>
    <action android:name="android.intent.action.VIEW" />
    <category android:name="android.intent.category.DEFAULT" />
    <data android:scheme="content" />
    <data android:mimeType="*/*" />
</intent-filter>
```

The activity checks if the URI's display name ends with `.doll` before processing.

## Security

### Zip Extraction Safety

- **Path traversal (zip-slip):** Every zip entry's path is validated against canonical path. Entries containing `..` are rejected. Follow the same pattern as `MemoryExporter.importMemory()`.
- **File size limits:** Max `.doll` file size: 200MB. Max individual entry: 100MB. Max total extracted: 300MB. Max entry count: 100.
- **Zip bomb protection:** Track total bytes extracted and abort if exceeding limit.

### Validation

`CharacterValidator` checks:
- `manifest.json` exists and is valid JSON with required fields
- `formatVersion` is a known version (reject unknown versions)
- `model.glb` exists (for `avatarType: "3d"`)
- All referenced files exist in the archive
- File sizes within limits
- Personality field lengths within PersonalityManager limits (backstory ≤ 2500, directive ≤ 150)

## Error Handling

- `importCharacter()` returns null on failure. Errors reported via `onCharacterImportFailed(String errorCode, String message)` callback.
- `getCharacterInfo()` returns JSON with all manifest fields + character ID + installed timestamp + file sizes.
- `listCharacters()` returns JSON array with: id, name, author, avatarType, thumbnailAvailable, isActive.

## Edge Cases

- **No character installed:** System operates without avatar. Personality uses defaults. Launcher shows empty scene with import prompt.
- **Corrupt character files:** CharacterManager detects on load, marks character as invalid, falls back to previous active character or no-character state.
- **Duplicate import:** Each import generates a new UUID. Importing the same `.doll` twice creates two separate characters.
- **Format version mismatch:** Unknown `formatVersion` → reject with clear error message.

## Files

```
DollOSAIService:
  app/src/main/java/org/dollos/ai/character/
    CharacterManager.kt        — import, export, switch, list, delete
    CharacterPack.kt           — data classes for manifest, personality, voice, scene
    CharacterValidator.kt      — validate .doll zip structure
  DollOSAIServiceImpl.kt       — AIDL method implementations
  aidl/IDollOSAIService.aidl   — character pack AIDL methods
  aidl/IDollOSAICallback.aidl  — onCharacterChanged callback
  AndroidManifest.xml          — .doll file association
```

## Out of Scope

- Live2D avatar rendering (deferred, `avatarType: "live2d"` reserved in manifest)
- Character marketplace / sharing platform
- Character editor UI
- Wake word model training
- TTS model bundling
- Encrypted character packs
