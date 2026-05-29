// DollOS Tauri spike — Rust shell.
//
// Notes:
// - Tauri 2.x: tray icon API lives in `tauri::tray` (built-in, no separate
//   `tauri-plugin-tray` crate on 2.x — that crate exists on Tauri 1.x).
// - Left click toggles main-window visibility.
// - Menu: "Show / Hide" + "Quit".
// - On Linux some DEs require libayatana-appindicator3-dev to render tray
//   icons; GNOME stock Wayland needs the AppIndicator extension. We don't
//   try to work around DE-level limitations in the spike.

use tauri::{
    menu::{Menu, MenuEvent, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

fn toggle_main_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        match w.is_visible() {
            Ok(true) => { let _ = w.hide(); }
            _ => {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let show_item = MenuItem::with_id(app, "toggle", "Show / Hide", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_item, &quit_item])?;

            let _tray = TrayIconBuilder::with_id("main-tray")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("DollOS spike")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event: MenuEvent| match event.id().as_ref() {
                    "toggle" => toggle_main_window(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        toggle_main_window(tray.app_handle());
                    }
                })
                .build(app)?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
