use std::process::Command;
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    webview::NewWindowResponse,
    Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};

const DASHBOARD: &str = "http://127.0.0.1:17865/";

fn service() -> std::io::Result<()> {
    let status = Command::new("/usr/bin/blynx-clips")
        .arg("--start-service")
        .status()?;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other("Could not start local Blynx Clips service"))
    }
}

fn main() {
    if let Err(error) = service() {
        eprintln!("{error}");
        let _ = Command::new("notify-send").args(["Blynx Clips", &error.to_string()]).spawn();
        std::process::exit(1);
    }
    tauri::Builder::default()
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "Show Blynx Clips", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit window and tray", true, None::<&str>)?;
            let stop = MenuItem::with_id(app, "stop", "Stop service and quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit, &stop])?;
            let mut tray = TrayIconBuilder::new().menu(&menu).tooltip("Blynx Clips");
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            let tray_ready = tray.on_menu_event(|app, event| match event.id().as_ref() {
                "show" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.show();
                        let _ = window.unminimize();
                        let _ = window.set_focus();
                    }
                }
                "quit" => app.exit(0),
                "stop" => {
                    let handle = app.clone();
                    std::thread::spawn(move || {
                        let _ = Command::new("systemctl").args(["--user", "stop", "blynx-clips.service"]).status();
                        handle.exit(0);
                    });
                }
                _ => {}
            }).build(app).is_ok();

            let web = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(DASHBOARD.parse()?))
                .title("Blynx Clips")
                .inner_size(1260., 840.)
                .min_inner_size(540., 490.)
                .on_navigation(|url| {
                    url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(17865)
                })
                .on_new_window(|url, _| {
                    let provider_link = matches!((url.host_str(), url.path()),
                        (Some("www.twitch.tv"), "/activate") |
                        (Some("dev.twitch.tv"), "/console/apps") |
                        (Some("console.cloud.google.com"), "/apis/credentials"));
                    if url.scheme() == "https" && provider_link {
                        let _ = Command::new("xdg-open").arg(url.as_str()).spawn();
                    }
                    NewWindowResponse::Deny
                })
                .build()?;
            if tray_ready {
                let hidden = web.clone();
                web.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = hidden.hide();
                    }
                });
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("could not run Blynx Clips");
}

