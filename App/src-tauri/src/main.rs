// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use walkdir::WalkDir;

#[derive(Serialize)]
struct FileEntry {
    path: String,
    name: String,
    is_dir: bool,
}

#[tauri::command]
fn list_dir(path: String) -> Result<Vec<FileEntry>, String> {
    let mut files = vec![];

    // Recursive walk with filtering for common ignored directories
    for entry in WalkDir::new(&path)
        .into_iter()
        .filter_entry(|e| {
            let name = e.file_name().to_str().unwrap_or("");
            name != "node_modules" && name != ".git" && name != "target" && name != "dist"
        })
        .filter_map(|e| e.ok()) {
            
        let file_path = entry.path();
        let name = file_path.file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_string();
        
        // Skip the root path itself
        if file_path.to_str() == Some(&path) {
            continue;
        }

        files.push(FileEntry {
            path: file_path.display().to_string(),
            name,
            is_dir: file_path.is_dir(),
        });
    }

    Ok(files)
}

#[tauri::command]
fn read_file(path: String) -> Result<String, String> {
    std::fs::read_to_string(path)
        .map_err(|e| e.to_string())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![list_dir, read_file])
        .run(tauri::generate_context!())
        .expect("error while running tauri app");
}