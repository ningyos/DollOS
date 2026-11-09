// Runs inside her VM (spec 3.1, 2026-11-09: the program reads an app's menu shortcuts itself,
// through the accessibility API; System Events hangs in the VM). Build on the host and copy in:
//
//     swiftc -O scripts/axmenus.swift -o /tmp/axmenus
//     vmpal --cli cp /tmp/axmenus <vm-id>:/Users/doll/agent-desktop/axmenus
//
// It needs the accessibility access VMPal Tools already has (`vmpal exec` runs under it).
//
//     axmenus --front        one line: bundle id, name, version (tabs); nothing when no front app
//     axmenus <bundle id>    one line per menu item with a shortcut: menu path, then the keys (a tab
//                            between), e.g. "File › Save As…\t⇧⌥⌘S"
//
// Keys: modifiers fn⇧⌥⌃⌘ (⌘ unless the item says it has none), then the key. A key that is not a
// printable character (Esc, arrows, Return…) is named from its virtual key code; an item whose key
// is neither (Emoji & Symbols' globe glyph) is skipped rather than written wrong.
import AppKit
import ApplicationServices

func attr(_ e: AXUIElement, _ n: String) -> CFTypeRef? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, n as CFString, &v) == .success ? v : nil
}
func kids(_ e: AXUIElement) -> [AXUIElement] { (attr(e, kAXChildrenAttribute) as? [AXUIElement]) ?? [] }
/// AXMenuItemCmdModifiers: 1 shift, 2 option, 4 control, 8 no command, 16 fn (the globe key;
/// TextEdit's Enter Full Screen is 24 with F, Fill is 28 with F, read 2026-11-09).
func mods(_ m: Int) -> String {
    var t = ""
    if m & 16 != 0 { t += "fn" }
    if m & 1 != 0 { t += "⇧" }; if m & 2 != 0 { t += "⌥" }; if m & 4 != 0 { t += "⌃" }; if m & 8 == 0 { t += "⌘" }
    return t
}
/// Virtual key codes of the keys that have no printable character.
let named: [Int: String] = [
    36: "Return", 48: "Tab", 49: "Space", 51: "Delete", 53: "Esc", 117: "ForwardDelete",
    115: "Home", 119: "End", 116: "PageUp", 121: "PageDown",
    123: "←", 124: "→", 125: "↓", 126: "↑",
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6", 98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
]
func printable(_ s: String) -> Bool {
    s.unicodeScalars.allSatisfy { u in
        !CharacterSet.controlCharacters.contains(u) && !(0xE000...0xF8FF).contains(u.value) && u != " "
    }
}
func key(_ item: AXUIElement) -> String? {
    let ch = attr(item, "AXMenuItemCmdChar") as? String ?? ""
    let vk = attr(item, "AXMenuItemCmdVirtualKey") as? Int
    let glyph = attr(item, "AXMenuItemCmdGlyph") as? Int ?? 0
    let k: String
    if !ch.isEmpty, printable(ch), glyph == 0 { k = ch.uppercased() }
    else if let vk, let n = named[vk] { k = n }
    else { return nil }
    return mods((attr(item, "AXMenuItemCmdModifiers") as? Int) ?? 0) + k
}

let arg = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "--front"
if arg == "--front" {
    guard let app = NSWorkspace.shared.frontmostApplication else { exit(0) }
    let info = app.bundleURL.flatMap { Bundle(url: $0)?.infoDictionary } ?? [:]
    let version = info["CFBundleShortVersionString"] as? String ?? info["CFBundleVersion"] as? String ?? ""
    print([app.bundleIdentifier ?? "", app.localizedName ?? "", version].joined(separator: "\t"))
    exit(0)
}
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: arg).first else {
    FileHandle.standardError.write(Data("\(arg) is not running\n".utf8)); exit(1)
}
guard AXIsProcessTrusted() else { FileHandle.standardError.write(Data("not trusted for accessibility\n".utf8)); exit(2) }
let el = AXUIElementCreateApplication(app.processIdentifier)
guard let barRef = attr(el, kAXMenuBarAttribute) else { FileHandle.standardError.write(Data("no menu bar\n".utf8)); exit(3) }
let bar = barRef as! AXUIElement
func walk(_ menuItem: AXUIElement, path: [String]) {
    for menu in kids(menuItem) {
        for item in kids(menu) {
            guard let title = attr(item, kAXTitleAttribute) as? String, !title.isEmpty else { continue }
            let p = path + [title]
            if let k = key(item) { print(p.joined(separator: " › ") + "\t" + k) }
            walk(item, path: p)   // submenus
        }
    }
}
for top in kids(bar) {
    guard let t = attr(top, kAXTitleAttribute) as? String, t != "Apple" else { continue }
    walk(top, path: [t])
}
