# AI Launcher Design

## Overview

AI Launcher replaces the default AOSP Launcher. The entire home screen is a 3D scene with the AI character's avatar at the center. Users interact through a floating text input at the bottom. AI responses appear as floating bubbles near the avatar, dismissed by tapping anywhere. App drawer slides in from the right edge.

## Layout

```
┌──────────────────────────┐
│  status bar              │
│                          │
│                          │
│    ┌──────────────┐      │
│    │              │      │
│    │  3D Avatar   │  ◄── Filament SurfaceView (fullscreen)
│    │  (center)    │      │
│    │              │      │
│    └──────────────┘      │
│         ┌──────────┐     │
│         │ AI says  │ ◄── Floating bubble (tap to dismiss)
│         │ hello!   │     │
│         └──────────┘     │
│  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  │
│  ░ Message...      🎤 ░ ◄── Input bar (translucent, gradient fade)
└──────────────────────────┘
```

- Entire background is Filament 3D scene
- UI elements (input bar, bubbles, status) are standard Android Views overlaid on SurfaceView
- No traditional home screen elements (no widgets, no icon grid)

## 3D Rendering

**Engine:** Google Filament (PBR renderer)
**Model format:** glTF 2.0 (.glb) with skeleton animations
**Render target:** SurfaceView (fullscreen, behind all UI)

Scene setup loaded from active Character Pack's `scene.json`:
- Background color/gradient
- Directional + ambient lighting
- Camera position and FOV

Avatar animations from Character Pack:
- `idle.glb` — default loop
- `talking.glb` — plays during AI response streaming
- `thinking.glb` — plays while waiting for LLM response
- Blend between animations for smooth transitions

## Conversation

### Input
- Bottom input bar with translucent background + gradient fade into scene
- Text input field + microphone button (microphone reserved for future Voice Pipeline)
- Sends message via `IDollOSAIService.sendMessage()`

### Response Display
- AI response streams token-by-token into a floating bubble near the avatar
- Bubble position: right side of avatar, vertically centered
- Bubble style: semi-transparent background with blur, rounded corners
- When streaming completes: bubble stays until user taps anywhere on screen
- Tap anywhere → bubble dismissed, screen returns to avatar-only view
- Only one response bubble at a time (new response replaces old)

### Action Results
- When AI executes an action (open app, set alarm, etc.), result shown as a brief toast-like indicator below the bubble
- Confirmation dialogs for confirmRequired actions appear as a card overlay

## App Drawer

- Triggered by swiping from right edge to left
- Slides in as a panel covering ~80% of screen width
- Semi-transparent dark background with blur
- Contents:
  - Search bar at top
  - Alphabetical app list with icons
  - Recent apps section at top (last 5 used)
- Swipe right or tap outside to dismiss
- Launching an app closes the drawer and starts the activity

## Character Switching

- Long-press on avatar → character picker overlay
- Shows thumbnails of all installed characters (from CharacterManager)
- Tap to switch → `setActiveCharacter()` → Launcher reloads model + scene
- Current character name shown briefly after switch

## Architecture

### Activity

`DollOSLauncherActivity` — registered as HOME launcher in AndroidManifest:

```xml
<activity android:name=".DollOSLauncherActivity"
    android:launchMode="singleTask"
    android:clearTaskOnLaunch="true"
    android:stateNotNeeded="true"
    android:resumeWhilePausing="true"
    android:excludeFromRecents="true"
    android:screenOrientation="nosensor"
    android:theme="@style/Theme.DollOSLauncher">
    <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.HOME" />
        <category android:name="android.intent.category.DEFAULT" />
    </intent-filter>
</activity>
```

### View Hierarchy

```
FrameLayout (root)
├── TextureView (Filament 3D scene, fullscreen — TextureView for proper compositing with translucent overlays)
├── FrameLayout (UI overlay)
│   ├── ResponseBubbleView (floating bubble, GONE by default)
│   ├── ActionConfirmCard (confirmation overlay, GONE by default)
│   └── InputBarView (bottom, translucent)
│       ├── EditText
│       └── ImageButton (mic)
├── AppDrawerView (right-edge panel, hidden by default)
│   ├── SearchBar
│   ├── RecentAppsRow
│   └── RecyclerView (app list)
└── CharacterPickerOverlay (GONE by default)
```

### Filament Integration

- `FilamentSceneManager` class manages the Filament engine lifecycle
- Initialized in `onCreate()`, destroyed in `onDestroy()`
- Loads glTF model via Filament's `gltfio` library
- Animation playback via Filament's `Animator`
- Scene config (lighting, camera, background) from Character Pack's `scene.json`
- Renders on `TextureView` (not SurfaceView — TextureView enables proper alpha compositing with translucent UI overlays like blurred bubbles and drawer)

### Service Connection

- Binds to `IDollOSAIService` on startup
- Registers `IDollOSAICallback` for:
  - `onToken()` → append to response bubble
  - `onResponseComplete()` → finalize bubble, switch avatar to idle animation
  - `onResponseError()` → show error in bubble
  - `onActionConfirmRequired()` → show confirmation card
  - `onActionExecuted()` → show result toast
  - `onCharacterChanged()` → reload avatar model + scene

### App Drawer Data

- Uses `PackageManager` to query installed apps with `getLaunchIntentForPackage()`
- Caches app list, refreshes on `ACTION_PACKAGE_ADDED` / `ACTION_PACKAGE_REMOVED`
- Recent apps tracked in SharedPreferences (last 5 package names)
- Search filters by app label (case-insensitive substring match)

## Project Structure

This is a new Android app (not part of DollOSAIService or DollOSService).

```
packages/apps/DollOSLauncher/
  AndroidManifest.xml
  Android.bp
  src/org/dollos/launcher/
    DollOSLauncherActivity.kt      — main launcher activity
    scene/
      FilamentSceneManager.kt      — Filament engine lifecycle + rendering
      SceneConfig.kt               — parse scene.json
      AvatarAnimator.kt            — animation state machine (idle/talking/thinking)
    conversation/
      ResponseBubbleView.kt        — floating response bubble
      InputBarView.kt              — bottom input bar
      ActionConfirmCard.kt         — action confirmation overlay
    drawer/
      AppDrawerView.kt             — right-swipe app drawer panel
      AppListAdapter.kt            — RecyclerView adapter for app list
      AppInfo.kt                   — data class for app entry
    character/
      CharacterPickerOverlay.kt    — long-press avatar character switcher
  res/
    layout/
      activity_launcher.xml        — root FrameLayout with SurfaceView + overlays
      view_response_bubble.xml
      view_input_bar.xml
      view_app_drawer.xml
      view_character_picker.xml
      item_app.xml
    drawable/
      bg_bubble.xml                — semi-transparent rounded rect
      bg_input_bar.xml             — translucent gradient
      bg_drawer.xml                — dark translucent panel
    values/
      styles.xml                   — Theme.DollOSLauncher (fullscreen, no action bar)
      colors.xml
```

## Character Asset Access

The Launcher is a separate app from DollOSAIService and cannot access its internal storage directly. Character assets (model.glb, animations, scene.json) are loaded via AIDL:

```
ParcelFileDescriptor getCharacterAsset(String characterId, String path);
```

On startup and on `onCharacterChanged`, the Launcher:
1. Calls `getActiveCharacter()` to get the character ID
2. Calls `getCharacterAsset(id, "scene.json")` → parse scene config
3. Calls `getCharacterAsset(id, "model.glb")` → load into Filament
4. Calls `getCharacterAsset(id, "animations/idle.glb")` etc. → load animation clips

Assets are read via ParcelFileDescriptor (file descriptor IPC), not copied.

## No Character Installed State

When no character pack is installed or active character is invalid:
- 3D scene shows empty/default background (dark gradient)
- No avatar rendered
- Center text: "No AI character installed. Import a .doll file to get started."
- Input bar still functional (conversation works, just no avatar)
- Character picker (long-press) shows empty state with import button

## App Drawer Gesture Handling

Right-edge swipe conflicts with Android's system back gesture. Resolution:
- Launcher calls `View.setSystemGestureExclusionRects()` to exclude a strip on the right edge (40dp wide)
- This allows the app drawer swipe to take priority over system back
- System back gesture still works on the left edge

## Navigation Behavior

- **Back button:** If app drawer is open → close drawer. If character picker is open → close picker. Otherwise → no-op (standard launcher behavior).
- **Home button:** If app drawer/picker is open → close and return to home. Handled via `onNewIntent()` in singleTask mode.
- **Recents:** Launcher excluded from recents (`excludeFromRecents="true"`).

## Animation State Machine

States: `IDLE`, `THINKING`, `TALKING`

Transitions:
- User sends message → `IDLE` → `THINKING`
- First token received → `THINKING` → `TALKING`
- Response complete → `TALKING` → `IDLE`
- Error → any state → `IDLE`

Blend duration: 300ms crossfade between animations.

Missing animations: if `talking.glb` or `thinking.glb` is not in the character pack, fall back to `idle.glb` for all states. Only `model.glb` is required; animation files are optional.

## Dependencies

- `com.google.android.filament:filament-android` — 3D rendering
- `com.google.android.filament:gltfio-android` — glTF model loading
- `com.google.android.filament:filament-utils-android` — utilities
- `dollos-service-aidl` — DollOSAIService AIDL
- `androidx.recyclerview` — app drawer list

## Out of Scope

- Live2D rendering (deferred, reserved in Character Pack format)
- Voice input (mic button is placeholder until Voice Pipeline)
- Notification display on launcher
- Widget support
- Wallpaper support (scene.json background replaces wallpaper)
- Avatar expression system tied to sentiment analysis
- Custom gesture recognition beyond app drawer swipe
- Conversation history browsing (deferred to separate Conversation UI)
