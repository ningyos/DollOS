# Power Key Binding Discovery: Long-Press to Launch Assist

**Date**: 2026-04-26  
**Task**: Understand how AOSP / GrapheneOS binds long-press power behavior before patching DollOS to use `launchAssist`.

---

## Enum Mapping: Integer → Behavior

Source: `/home/progcat/Projects/DollOS-build/frameworks/base/core/res/res/values/config.xml` (lines 1151–1158)  
and `/home/progcat/Projects/DollOS-build/frameworks/base/services/core/java/com/android/server/policy/PhoneWindowManager.java` (lines 320–325)

```
0 = LONG_PRESS_POWER_NOTHING
1 = LONG_PRESS_POWER_GLOBAL_ACTIONS (default)
2 = LONG_PRESS_POWER_SHUT_OFF (with confirmation)
3 = LONG_PRESS_POWER_SHUT_OFF_NO_CONFIRM (no confirmation)
4 = LONG_PRESS_POWER_GO_TO_VOICE_ASSIST
5 = LONG_PRESS_POWER_ASSISTANT (uses Settings.Secure.ASSISTANT, typically Google Assistant)
```

---

## Default Value

**Mainline AOSP (framework):**
- `config_longPressOnPowerBehavior` integer defaults to **1** (LONG_PRESS_POWER_GLOBAL_ACTIONS)
- Location: `frameworks/base/core/res/res/values/config.xml:1159`

**GrapheneOS override:**
- No override found in vendor/dollos or local_manifest configurations.
- GrapheneOS inherits mainline default (1).

---

## Code Paths for Long-Press Dispatch

### Entry Point: `interceptPowerKeyDown()`
- File: `frameworks/base/services/core/java/com/android/server/policy/PhoneWindowManager.java`
- Line: 1076
- Invoked when: Power key is pressed down (any duration)

### Long-Press Detection: `SingleKeyGestureDetector`
- Triggers callback `onLongPress(long eventTime)` (line 2791)
- No keyguard check at this stage; behaves the same whether locked or unlocked

### Dispatch to Behavior: `powerLongPress()`
- File: PhoneWindowManager.java
- Line: 1501–1560
- **Critical:** This method does NOT have keyguard gating. Long-press executes regardless of lock state.
- Calls `getResolvedLongPressOnPowerBehavior()` to resolve the final behavior (line 1502)

### Resolution: `getResolvedLongPressOnPowerBehavior()`
- File: PhoneWindowManager.java
- Line: 1617–1635
- **Does check:** Factory test mode, device provisioning, and settings overrides
- **Does NOT check:** Keyguard state
- Returns the behavior integer to execute

### Settings.Global Override Point
- Key: `Settings.Global.POWER_BUTTON_LONG_PRESS`
- Defined: `frameworks/base/core/java/android/provider/Settings.java:18663`
- Runtime read: `PhoneWindowManager.java:3137–3140`
- Fallback: `config.xml` integer if not set in Settings.Global

### Action Dispatch: `launchAssistAction()`
- File: PhoneWindowManager.java
- Line: 4830
- Calls `SearchManager.launchAssist(args)` (line 4852)
- This is the method that DollOS needs to intercept / override

---

## Keyguard Branch Behavior

**Finding:** Long-press DOES NOT have keyguard gating in mainline AOSP.

### Why?
1. `powerLongPress()` is called unconditionally from `SingleKeyGestureDetector.onLongPress()` (line 2791–2798)
2. No `isKeyguardShowingAndNotOccluded()` check exists in the call chain
3. `getResolvedLongPressOnPowerBehavior()` has NO keyguard checks

### Keyguard checks exist elsewhere:
- `powerPress()` (line 1234): Checks `isKeyguardShowing()` for some short-press behaviors
- `shortPressPowerGoHome()` (line 1399): Uses `isKeyguardShowingAndNotOccluded()` to notify keyguard delegate
- `showGlobalActions()` (line 1920): Reads `isKeyguardShowingAndNotOccluded()` to style dialog

But `powerLongPress()` itself has NO such gates.

---

## Recommended Approach: Option B (Settings.Global Runtime Write)

**Option A (RRO Override):**  
✓ Pros: Static, survives reboots, clean vendor separation  
✗ Cons: RRO layer complexity, requires rebuilding system image  

**Option B (Settings.Global Runtime Write):** ← RECOMMENDED  
✓ Pros: Dynamic at runtime, DollOS AI Service can toggle it, no system image rebuild  
✓ Pros: Easy to test and debug on device  
✓ Pros: Can be user-configurable  
✗ Cons: Survives reboot (lives in Settings), but can be reset by user  

**Option C (Source Patch):**  
✗ Cons: Requires framework source edit, merge conflict risk, maintenance burden  
✗ Cons: Not as dynamic as B  

### **Recommendation: Option B**

Set `Settings.Global.POWER_BUTTON_LONG_PRESS = 5` (LONG_PRESS_POWER_ASSISTANT) at first boot or during setup via DollOSAIService or DollOSSetupWizard. This leverages the built-in Settings.Global observer that PhoneWindowManager already registers (line 926), so mLongPressOnPowerBehavior will be updated live without requiring a reboot or patch.

**Justification:** Simplest, most maintainable, no framework patch required, leverage built-in infrastructure, user-friendly (can be toggled in Settings if needed later).

---

## Implementation Next Steps (For Task 2)

1. **Verify on device:**
   - Confirm that `Settings.Global.POWER_BUTTON_LONG_PRESS = 5` is actually read and applied
   - Test long-press while locked to ensure `launchAssist` is invoked (even on keyguard)
   - Verify haptic feedback fires (ASSISTANT_BUTTON)

2. **DollOSAIService integration:**
   - At startup or first-boot, write: `Settings.Global.putInt(contentResolver, Settings.Global.POWER_BUTTON_LONG_PRESS, 5)`
   - Ensure DollOS AI Service has WRITE_SETTINGS permission

3. **Fallback to voice assist if needed:**
   - If LONG_PRESS_POWER_ASSISTANT doesn't work as expected, fall back to option 4 (LONG_PRESS_POWER_GO_TO_VOICE_ASSIST)
   - But prefer 5 for full integration with system assistant framework

4. **Test permutations:**
   - Long-press while locked (should trigger assist)
   - Long-press while unlocked (should trigger assist)
   - Long-press during setup (getResolvedLongPressOnPowerBehavior has device provisioning logic)

---

## Open Questions for Verification

1. When `LONG_PRESS_POWER_ASSISTANT` is set, does it call launchAssistAction with INVOCATION_TYPE_POWER_BUTTON_LONG_PRESS?
   - Expected: Yes (line 1556–1557)
   - Impact: This determines if DollOS AI Service receives the right invocation context

2. Does launchAssistAction route to DollOSAIService or system Google Assistant by default?
   - Depends on: Settings.Secure.ASSISTANT value
   - Action: May need to override ASSISTANT setting to point to DollOSAIService

3. Can DollOS intercept launchAssistAction before SearchManager.launchAssist()?
   - Options: StatusBar service, DollOSService via AIDL, launchAssist override
   - TBD: Next task

4. Does keyguard dismiss automatically when assist is launched while locked?
   - Behavior: Possibly handled by StatusBar / DollOSLauncher UI
   - Test: Long-press on locked device, observe if launcher appears

---

## Files Modified / Referenced

- `/home/progcat/Projects/DollOS-build/frameworks/base/core/res/res/values/config.xml` (enum values, default)
- `/home/progcat/Projects/DollOS-build/frameworks/base/services/core/java/com/android/server/policy/PhoneWindowManager.java` (dispatch logic, Settings.Global observer)
- `/home/progcat/Projects/DollOS-build/frameworks/base/core/java/android/provider/Settings.java` (Settings.Global.POWER_BUTTON_LONG_PRESS definition)

No GrapheneOS-specific overrides found.

---

## Summary

Long-press power binding in AOSP is fully configurable via `Settings.Global.POWER_BUTTON_LONG_PRESS`. The behavior enum supports direct assist launch (value 5 = LONG_PRESS_POWER_ASSISTANT), which will call `launchAssistAction()` unconditionally, even on the keyguard. DollOS should set this value at startup and intercept the assist intent downstream (via StatusBar, DollOSService, or settings), making long-press power the primary PTT trigger.
