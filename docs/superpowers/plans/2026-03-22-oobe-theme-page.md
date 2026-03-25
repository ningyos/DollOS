# OOBE Theme Selection Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a theme selection page to the DollOS OOBE wizard, allowing users to choose Light, Dark, or Auto (default) theme. The selection applies immediately to the OOBE UI and persists as the system default.

**Architecture:** Add a new `ThemePage` fragment between Welcome and Wi-Fi pages. Three radio-style options with live preview — tapping an option immediately applies the theme to the running OOBE. On `onNext()`, save the choice to system settings via `UiModeManager`.

**Tech Stack:** Kotlin, Android UiModeManager, AppCompatDelegate, ViewPager2

---

### File Structure

| Action | Path | Purpose |
|--------|------|---------|
| Create | `src/org/dollos/setup/ThemePage.kt` | Theme selection fragment |
| Create | `res/layout/page_theme.xml` | Theme page layout with 3 options |
| Modify | `src/org/dollos/setup/SetupWizardActivity.kt` | Add "theme" to page list |

---

### Task 1: Create ThemePage layout

**Files:**
- Create: `packages/apps/DollOSSetupWizard/res/layout/page_theme.xml`

- [ ] **Step 1: Create layout file**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:gravity="center_horizontal"
    android:paddingLeft="40dp"
    android:paddingRight="40dp"
    android:paddingTop="120dp">

    <TextView
        android:id="@+id/title"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        style="@style/SetupTitle" />

    <TextView
        android:id="@+id/description"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:layout_marginTop="12dp"
        android:gravity="center"
        style="@style/SetupSubtitle" />

    <RadioGroup
        android:id="@+id/theme_group"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="40dp"
        android:orientation="vertical">

        <RadioButton
            android:id="@+id/theme_auto"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:text="Auto (follow system)"
            android:textSize="17sp"
            android:paddingStart="12dp"
            android:paddingTop="16dp"
            android:paddingBottom="16dp"
            android:checked="true" />

        <RadioButton
            android:id="@+id/theme_light"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:text="Light"
            android:textSize="17sp"
            android:paddingStart="12dp"
            android:paddingTop="16dp"
            android:paddingBottom="16dp" />

        <RadioButton
            android:id="@+id/theme_dark"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:text="Dark"
            android:textSize="17sp"
            android:paddingStart="12dp"
            android:paddingTop="16dp"
            android:paddingBottom="16dp" />

    </RadioGroup>

</LinearLayout>
```

- [ ] **Step 2: Commit**

```bash
git add res/layout/page_theme.xml
git commit -m "feat: add theme selection page layout for OOBE"
```

---

### Task 2: Create ThemePage fragment

**Files:**
- Create: `packages/apps/DollOSSetupWizard/src/org/dollos/setup/ThemePage.kt`

- [ ] **Step 1: Create ThemePage.kt**

```kotlin
package org.dollos.setup

import android.app.UiModeManager
import android.content.Context
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.RadioGroup
import android.widget.TextView
import androidx.appcompat.app.AppCompatDelegate
import androidx.fragment.app.Fragment

class ThemePage : Fragment(), SetupPage {

    private lateinit var themeGroup: RadioGroup

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?
    ): View {
        val view = inflater.inflate(R.layout.page_theme, container, false)
        view.findViewById<TextView>(R.id.title).text = "Appearance"
        view.findViewById<TextView>(R.id.description).text =
            "Choose your preferred theme.\nYou can change this later in Settings."

        themeGroup = view.findViewById(R.id.theme_group)

        themeGroup.setOnCheckedChangeListener { _, checkedId ->
            val mode = when (checkedId) {
                R.id.theme_light -> AppCompatDelegate.MODE_NIGHT_NO
                R.id.theme_dark -> AppCompatDelegate.MODE_NIGHT_YES
                else -> AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM
            }
            AppCompatDelegate.setDefaultNightMode(mode)
        }

        return view
    }

    override fun onNext() {
        val uiModeManager = requireContext().getSystemService(Context.UI_MODE_SERVICE) as UiModeManager
        when (themeGroup.checkedRadioButtonId) {
            R.id.theme_light -> uiModeManager.setNightModeActivated(false)
            R.id.theme_dark -> uiModeManager.setNightModeActivated(true)
            R.id.theme_auto -> {
                // Reset to system default (auto)
                uiModeManager.setNightMode(UiModeManager.MODE_NIGHT_AUTO)
            }
        }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add src/org/dollos/setup/ThemePage.kt
git commit -m "feat: add ThemePage fragment with live preview and system persistence"
```

---

### Task 3: Wire ThemePage into SetupWizardActivity

**Files:**
- Modify: `packages/apps/DollOSSetupWizard/src/org/dollos/setup/SetupWizardActivity.kt`

- [ ] **Step 1: Add "theme" to pageKeys after "welcome"**

Change:
```kotlin
private val pageKeys = listOf(
    "welcome", "wifi", "gms", "model_download",
    "api_key", "personality", "voice", "complete"
)
```

To:
```kotlin
private val pageKeys = listOf(
    "welcome", "theme", "wifi", "gms", "model_download",
    "api_key", "personality", "voice", "complete"
)
```

- [ ] **Step 2: Add ThemePage to createFragment()**

Add a case in the `when` block inside `createFragment()`:

```kotlin
"theme" -> ThemePage()
```

- [ ] **Step 3: Commit**

```bash
git add src/org/dollos/setup/SetupWizardActivity.kt
git commit -m "feat: wire ThemePage into OOBE as page 2 (after Welcome)"
```

---

### Task 4: Build and verify

- [ ] **Step 1: Build**

```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-bp2a-userdebug
m DollOSSetupWizard -j$(nproc)
```

- [ ] **Step 2: Push to device and test**

```bash
adb root
adb remount
adb push out/target/product/bluejay/system_ext/priv-app/DollOSSetupWizard/DollOSSetupWizard.apk /system_ext/priv-app/DollOSSetupWizard/DollOSSetupWizard.apk
adb reboot
```

- [ ] **Step 3: Verify**

Expected behavior:
- Page 2 shows "Appearance" with Auto/Light/Dark radio buttons
- Auto is selected by default
- Tapping Light/Dark immediately changes the OOBE theme
- Dot indicators show 9 dots (was 8)
- After completing OOBE, system theme matches selection

- [ ] **Step 4: Commit final state**

```bash
git add -A
git commit -m "feat: OOBE theme selection page (Light/Dark/Auto)"
```
