#!/usr/bin/env python3
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from replicator_config import (
    CONFIG_PATH,
    DEFAULT_API_BASE,
    DEFAULT_MODEL,
    DEFAULT_OPENSCAD,
    DEFAULT_PREVIEW_SIZE,
    load_config,
    normalize_optional_text,
    save_config,
    project_dirs,
)
from replicator_generation import (
    build_generation_prompt,
    extract_requested_name,
    extract_scad_code,
    looks_like_name_plate_prompt,
    looks_like_token_prompt,
    maybe_postprocess_scad,
    request_scad_printability_fix,
    request_scad_syntax_fix,
    request_scad_from_openai,
    resolve_api_key,
    slugify,
    write_metadata,
)
from replicator_voice import transcribe_prompt_with_whisper


APP_TITLE = "Replicator"
OPENAI_MODEL_SUGGESTIONS = [
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
    "o4-mini",
]
def run_subprocess(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, cwd=str(cwd) if cwd else None)


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: "ReplicatorApp") -> None:
        super().__init__(parent)
        self.parent = parent
        self.cfg = parent.cfg
        self.title("Settings")
        self.geometry("860x620")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self.entries: dict[str, tk.Variable] = {}

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self._build_generation_tab(notebook)
        self._build_paths_tab(notebook)
        self._build_project_tab(notebook)
        self._build_print_tab(notebook)

        btn_row = ttk.Frame(self)
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(btn_row, text="Save", command=self._save).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 8))

    def _add_labeled_entry(self, parent: ttk.Frame, label: str, key: str, value: str, width: int = 70) -> None:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=4)
        ttk.Label(row, text=label, width=26).pack(side=tk.LEFT)
        var = tk.StringVar(value=value)
        self.entries[key] = var
        ttk.Entry(row, textvariable=var, width=width).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _add_labeled_combobox(self, parent: ttk.Frame, label: str, key: str, value: str, options: list[str], width: int = 67) -> None:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=4)
        ttk.Label(row, text=label, width=26).pack(side=tk.LEFT)
        var = tk.StringVar(value=value)
        self.entries[key] = var
        combo = ttk.Combobox(row, textvariable=var, values=options, width=width)
        combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _build_project_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Project")

        pr = self.cfg.get("projects", {})
        # Root directory with a browse button
        row_root = ttk.Frame(tab)
        row_root.pack(fill=tk.X, pady=4)
        ttk.Label(row_root, text="Project Root", width=26).pack(side=tk.LEFT)
        var_root = tk.StringVar(value=str(pr.get("root_dir", project_dirs(self.cfg)["root"])))
        self.entries["projects.root_dir"] = var_root
        entry_root = ttk.Entry(row_root, textvariable=var_root, width=60)
        entry_root.pack(side=tk.LEFT, fill=tk.X, expand=True)
        def _browse_root() -> None:
            from tkinter.filedialog import askdirectory
            cur = var_root.get().strip() or str(project_dirs(self.cfg)["root"])
            selected = askdirectory(initialdir=cur, title="Select Project Root")
            if selected:
                var_root.set(selected)
        ttk.Button(row_root, text="Browse", command=_browse_root).pack(side=tk.LEFT, padx=(6, 0))

        # Project name (required)
        self._add_labeled_entry(tab, "Project Name", "projects.name", str(pr.get("name", "default")))

        # Open project folder
        def _open_proj_folder() -> None:
            pd = project_dirs(self.cfg)
            base = Path(var_root.get().strip() or str(pd["root"])) / (self.entries["projects.name"].get().strip() or "default")
            base.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(base))
            else:
                messagebox.showinfo("Open Folder", str(base))
        ttk.Button(tab, text="Open Project Folder", command=_open_proj_folder).pack(anchor="w", padx=4, pady=(8, 0))

    def _add_labeled_bool(self, parent: ttk.Frame, label: str, key: str, value: bool) -> None:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=4)
        var = tk.BooleanVar(value=value)
        self.entries[key] = var
        ttk.Checkbutton(row, text=label, variable=var).pack(side=tk.LEFT)

    def _build_generation_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Generation")

        gen = self.cfg["generation"]
        self._add_labeled_entry(tab, "API Key", "generation.api_key", str(gen.get("api_key", "")))
        self._add_labeled_entry(tab, "API Key Env", "generation.api_key_env", str(gen.get("api_key_env", "OPENAI_API_KEY")))
        self._add_labeled_entry(tab, "API Base", "generation.api_base", str(gen.get("api_base", DEFAULT_API_BASE)))
        self._add_labeled_combobox(
            tab,
            "Model",
            "generation.model",
            str(gen.get("model", DEFAULT_MODEL)),
            OPENAI_MODEL_SUGGESTIONS,
        )
        self._add_labeled_entry(tab, "Temperature", "generation.temperature", str(gen.get("temperature", 0.2)))
        self._add_labeled_entry(tab, "Max Tokens", "generation.max_tokens", str(gen.get("max_tokens", 2500)))
        self._add_labeled_entry(tab, "Preview Size", "generation.preview_size", str(gen.get("preview_size", DEFAULT_PREVIEW_SIZE)))
        self._add_labeled_entry(tab, "Name Override", "generation.name", str(gen.get("name", "")))
        self._add_labeled_bool(tab, "Offline Nameplate", "generation.offline_nameplate", bool(gen.get("offline_nameplate", False)))
        self._add_labeled_bool(tab, "Dry Run", "generation.dry_run", bool(gen.get("dry_run", False)))
        self._add_labeled_entry(tab, "Whisper Model", "generation.whisper_model", str(gen.get("whisper_model", "base")))
        self._add_labeled_entry(tab, "Voice Seconds", "generation.voice_seconds", str(gen.get("voice_seconds", 8)))

    def _build_paths_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Paths")

        paths = self.cfg["paths"]
        self._add_labeled_entry(tab, "OpenSCAD EXE", "paths.openscad_exe", str(paths.get("openscad_exe", DEFAULT_OPENSCAD)))
        self._add_labeled_entry(tab, "Orca EXE", "paths.orca_exe", str(paths.get("orca_exe", "")))
        self._add_labeled_entry(tab, "Orca Config", "paths.orca_conf", str(paths.get("orca_conf", "")))
        self._add_labeled_entry(tab, "Orca User Dir", "paths.orca_user_dir", str(paths.get("orca_user_dir", "")))
        self._add_labeled_entry(tab, "Orca System Dir", "paths.orca_system_dir", str(paths.get("orca_system_dir", "")))
        self._add_labeled_entry(tab, "Build Dir", "paths.build_dir", str(paths.get("build_dir", "build")))

    def _build_print_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Print")

        printer = self.cfg["printer"]
        slicing = self.cfg["slicing"]
        self._add_labeled_entry(tab, "Host", "printer.host", str(printer.get("host", "192.168.1.156")))
        self._add_labeled_bool(tab, "Auto Level", "printer.auto_level", bool(printer.get("auto_level", False)))
        self._add_labeled_bool(tab, "Timelapse", "printer.timelapse", bool(printer.get("timelapse", False)))
        self._add_labeled_bool(tab, "Skip Confirmation (--yes)", "printer.skip_confirmation", bool(printer.get("skip_confirmation", True)))

        self._add_labeled_entry(tab, "Filament Preset", "slicing.filament_preset", normalize_optional_text(slicing.get("filament_preset", "")))
        self._add_labeled_bool(tab, "Allow Missing Thumbnail", "slicing.allow_missing_thumbnail", bool(slicing.get("allow_missing_thumbnail", False)))
        self._add_labeled_entry(tab, "Thumbnail Size", "slicing.thumbnail_size", str(slicing.get("thumbnail_size", 144)))
        self._add_labeled_bool(tab, "Ensure Heat Order", "slicing.ensure_heat_order", bool(slicing.get("ensure_heat_order", False)))
        self._add_labeled_bool(tab, "Ensure Prime Strip", "slicing.ensure_prime_strip", bool(slicing.get("ensure_prime_strip", False)))
        self._add_labeled_entry(tab, "WebSocket Port", "slicing.ws_port", str(slicing.get("ws_port", 3030)))
        self._add_labeled_entry(tab, "Upload URL Override", "slicing.upload_url", normalize_optional_text(slicing.get("upload_url", "")))
        self._add_labeled_bool(tab, "Simulation Mode", "slicing.sim", bool(slicing.get("sim", False)))
        self._add_labeled_bool(tab, "Sim Stub Slicer", "slicing.sim_stub_slicer", bool(slicing.get("sim_stub_slicer", False)))

    def _set_nested(self, key: str, value: object) -> None:
        a, b = key.split(".", 1)
        self.cfg[a][b] = value

    def _save(self) -> None:
        try:
            for key, var in self.entries.items():
                if isinstance(var, tk.BooleanVar):
                    self._set_nested(key, bool(var.get()))
                    continue

                text_value = str(var.get()).strip()
                if key in {
                    "generation.temperature",
                }:
                    self._set_nested(key, float(text_value))
                elif key in {
                    "generation.max_tokens",
                    "generation.preview_size",
                    "generation.voice_seconds",
                    "slicing.thumbnail_size",
                    "slicing.ws_port",
                }:
                    self._set_nested(key, int(text_value))
                else:
                    self._set_nested(key, text_value)

            # Validate required project name
            proj_name = str(self.cfg.get("projects", {}).get("name", "")).strip()
            if not proj_name:
                raise ValueError("Project Name cannot be empty")

            # Ensure project directories exist
            pd = project_dirs(self.cfg)
            for d in (pd["base"], pd["generated"], pd["stl"], pd["gcode"]):
                d.mkdir(parents=True, exist_ok=True)

            save_config(CONFIG_PATH, self.cfg)
            self.parent.on_settings_saved()
            self.destroy()
        except ValueError as exc:
            messagebox.showerror("Invalid value", f"Please fix numeric settings: {exc}", parent=self)


class ReplicatorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x700")

        self.cfg = load_config(CONFIG_PATH)
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self._log_lock = threading.Lock()
        try:
            self.log_file_path = Path("replicator.log").resolve()
        except Exception:
            self.log_file_path = Path("replicator.log")
        # Keep strong references to images inserted in the log to avoid GC
        self._log_images: list[object] = []
        # Track which image paths we've already previewed to avoid duplicates
        self._log_image_paths: set[str] = set()

        self.prompt_var = tk.StringVar(value=str(self.cfg["ui"].get("last_prompt", "")))
        self.print_var = tk.BooleanVar(value=bool(self.cfg["ui"].get("print_enabled", False)))
        self.show_preview_var = tk.BooleanVar(value=bool(self.cfg["ui"].get("show_preview", True)))
        self.visualize_var = tk.BooleanVar(value=bool(self.cfg["ui"].get("visualize_before_print", False)))
        self.show_log_details_var = tk.BooleanVar(value=bool(self.cfg["ui"].get("show_log_details", True)))

        self.status_var = tk.StringVar(value="Ready")

        self._build_menu()
        self._build_ui()
        self.after(100, self._drain_log_queue)

    def _build_menu(self) -> None:
        menu = tk.Menu(self)

        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Open OpenSCAD Folder", command=self.open_openscad_folder)
        file_menu.add_command(label="Open Model in OpenSCAD", command=self.open_model_in_openscad)
        file_menu.add_command(label="View Log", command=self.view_log_file)
        file_menu.add_command(label="Print", command=self.start_print_again)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_exit)
        menu.add_cascade(label="File", menu=file_menu)

        settings_menu = tk.Menu(menu, tearoff=False)
        settings_menu.add_command(label="Open Settings", command=self.open_settings)
        settings_menu.add_command(label="Save Settings", command=self.save_ui_settings)
        menu.add_cascade(label="Settings", menu=settings_menu)

        self.config(menu=menu)

    def _build_ui(self) -> None:
        root = ttk.Frame(self)
        root.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        top = ttk.LabelFrame(root, text="Replicator Prompt - Create objects with one click!")
        top.pack(fill=tk.X)

        ttk.Label(top, text="Prompt").pack(anchor="w", padx=8, pady=(8, 2))
        entry = ttk.Entry(top, textvariable=self.prompt_var)
        entry.pack(fill=tk.X, padx=8, pady=(0, 8))
        entry.focus_set()

        opts = ttk.Frame(top)
        opts.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Checkbutton(opts, text="3D Print", variable=self.print_var).pack(side=tk.LEFT)
        ttk.Checkbutton(opts, text="Show Preview", variable=self.show_preview_var).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Checkbutton(opts, text="Visualize G-code in 3D before print", variable=self.visualize_var).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Checkbutton(opts, text="Show Log Details", variable=self.show_log_details_var).pack(side=tk.LEFT, padx=(16, 0))

        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.generate_btn = ttk.Button(btns, text="Generate", command=self.start_generation)
        self.generate_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="Voice Input", command=self.capture_voice_prompt).pack(side=tk.LEFT, padx=(8, 0))

        log_frame = ttk.LabelFrame(root, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.log_widget = ScrolledText(log_frame, wrap=tk.WORD, height=24)
        self.log_widget.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.log_widget.configure(state=tk.DISABLED)

        status = ttk.Label(root, textvariable=self.status_var)
        status.pack(fill=tk.X, pady=(8, 0))
        # Refresh log view when toggling detail visibility
        self.show_log_details_var.trace_add("write", lambda *_: self._refresh_log_view())

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_queue.put(line)
        # also append to log file for offline diagnostics
        try:
            with self._log_lock:
                with self.log_file_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            # logging must never break UI
            pass
        # keep in-memory history for re-filtering on toggle
        if not hasattr(self, "_log_history"):
            self._log_history: list[str] = []
        self._log_history.append(line)

    def _is_noisy_log_line(self, line: str) -> bool:
        content = line
        if line.startswith("[") and "] " in line:
            try:
                content = line.split("] ", 1)[1]
            except Exception:
                content = line
        noisy_prefixes = (
            "OpenSCAD preview command:",
            "OpenSCAD launch command:",
            "OpenSCAD export command:",
            "OrcaSlicer command:",
            "print_scad (slice only) command:",
            "print_scad (slice retry) command:",
            "print_scad (full print) command:",
            "visualize_gcode command:",
            # Additional high-verbosity operational lines to hide
            "SCAD:",
            "Auto-detected OrcaSlicer settings",
            "Thumbnail embedded:",
            "Skipping upload.",
            "G-code:",
        )
        if content.startswith(noisy_prefixes):
            return True
        if content.startswith("[stderr]"):
            return True
        # command lines we print with two leading spaces
        if content.startswith("  "):
            return True
        # slicer trace stamp like: [2026-07-18 10:54:50....] [trace] ...
        if content.startswith("[") and len(content) > 5 and content[1:5].isdigit():
            return True
        return False

    def _run_command_streaming(self, cmd: list[str], *, label: str | None = None) -> tuple[int, str, str]:
        if label:
            self._log(f"{label} command:")
        self._log("  " + subprocess.list2cmdline(cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception as exc:
            raise RuntimeError(f"Failed to start command: {exc}")

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _pump(pipe, is_err: bool) -> None:
            try:
                assert pipe is not None
                for raw in iter(pipe.readline, ""):
                    txt = raw.rstrip("\r\n")
                    (stderr_chunks if is_err else stdout_chunks).append(txt + "\n")
                    prefix = "[stderr] " if is_err else ""
                    self._log(prefix + txt)
            finally:
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=_pump, args=(proc.stdout, False), daemon=True) if proc.stdout else None
        t_err = threading.Thread(target=_pump, args=(proc.stderr, True), daemon=True) if proc.stderr else None
        if t_out:
            t_out.start()
        if t_err:
            t_err.start()
        rc = proc.wait()
        if t_out:
            t_out.join()
        if t_err:
            t_err.join()
        return rc, "".join(stdout_chunks), "".join(stderr_chunks)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            show_detail = bool(self.show_log_details_var.get())
            if show_detail or not self._is_noisy_log_line(line):
                self.log_widget.configure(state=tk.NORMAL)
                self.log_widget.insert(tk.END, line + "\n")
                self.log_widget.see(tk.END)
                self.log_widget.configure(state=tk.DISABLED)
            # Try to inline-display any PNG paths mentioned in this line
            try:
                self._maybe_insert_inline_images(line)
            except Exception:
                # Never allow preview failures to impact logging/UI
                pass
        self.after(100, self._drain_log_queue)

    def _refresh_log_view(self) -> None:
        # Re-render the log widget based on current filter
        try:
            history = getattr(self, "_log_history", [])
            self.log_widget.configure(state=tk.NORMAL)
            self.log_widget.delete("1.0", tk.END)
            self.log_widget.configure(state=tk.DISABLED)
            # Reset image caches for fresh inserts
            self._log_images.clear()
            self._log_image_paths.clear()
            for line in history:
                show_detail = bool(self.show_log_details_var.get())
                if show_detail or not self._is_noisy_log_line(line):
                    self.log_widget.configure(state=tk.NORMAL)
                    self.log_widget.insert(tk.END, line + "\n")
                    self.log_widget.see(tk.END)
                    self.log_widget.configure(state=tk.DISABLED)
                try:
                    self._maybe_insert_inline_images(line)
                except Exception:
                    pass
        except Exception:
            pass

    def _maybe_insert_inline_images(self, line: str) -> None:
        # Detect quoted paths and bare tokens that end with .png
        import re
        candidates: list[str] = []
        # quoted "...png" or '...png'
        for m in re.finditer(r"[\"']([^\"']+?\.png)[\"']", line, flags=re.IGNORECASE):
            candidates.append(m.group(1))
        # bare tokens ending with .png (no spaces inside)
        for token in line.split():
            if token.lower().endswith(".png"):
                # strip trailing punctuation commonly present in logs
                t = token.strip().strip(",.;")
                candidates.append(t)

        if not candidates:
            return

        # Resolve and insert unique existing paths
        seen: set[str] = set()
        for raw in candidates:
            path = raw
            try:
                p = Path(path)
                if not p.is_absolute():
                    # Resolve relative to current working directory of app
                    p = (Path.cwd() / p).resolve()
                path = str(p)
            except Exception:
                continue
            if path in seen:
                continue
            seen.add(path)
            p_obj = Path(path)
            if not p_obj.exists() or not p_obj.is_file():
                continue
            # Skip if we already displayed this image earlier
            norm_key = str(p_obj.resolve())
            if norm_key in self._log_image_paths:
                continue
            # Insert on UI thread (we already are), using Pillow for scaling
            try:
                from PIL import Image, ImageTk  # type: ignore
            except Exception:
                # Pillow not installed; skip inline preview
                continue
            try:
                img = Image.open(p_obj)
                # Scale to max width 420 px, preserve aspect ratio
                max_w = 420
                if img.width > max_w:
                    ratio = max_w / float(img.width)
                    new_size = (max_w, max(1, int(img.height * ratio)))
                    img = img.resize(new_size, Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self.log_widget.configure(state=tk.NORMAL)
                self.log_widget.insert(tk.END, "\n")
                self.log_widget.image_create(tk.END, image=photo)
                self.log_widget.insert(tk.END, f"\n{p_obj}\n")
                self.log_widget.see(tk.END)
                self.log_widget.configure(state=tk.DISABLED)
                # Keep a reference so Tk doesn't GC the image
                self._log_images.append(photo)
                self._log_image_paths.add(norm_key)
            except Exception:
                # Ignore any image decoding/display errors
                pass

    def open_settings(self) -> None:
        SettingsDialog(self)

    def on_settings_saved(self) -> None:
        self.status_var.set("Settings saved")
        self._log("Settings saved to replicator.json")

    def save_ui_settings(self) -> None:
        self.cfg["ui"]["last_prompt"] = self.prompt_var.get().strip()
        self.cfg["ui"]["print_enabled"] = bool(self.print_var.get())
        self.cfg["ui"]["show_preview"] = bool(self.show_preview_var.get())
        self.cfg["ui"]["visualize_before_print"] = bool(self.visualize_var.get())
        self.cfg["ui"]["show_log_details"] = bool(self.show_log_details_var.get())
        save_config(CONFIG_PATH, self.cfg)
        self.status_var.set("Settings saved")

    def on_exit(self) -> None:
        self.save_ui_settings()
        self.destroy()

    def _current_scad_path(self) -> Path:
        pd = project_dirs(self.cfg)
        output_dir = pd["generated"].resolve()
        base_name_cfg = str(self.cfg["generation"].get("name", "")).strip()
        prompt = self.prompt_var.get().strip()
        base_name = slugify(base_name_cfg if base_name_cfg else prompt)
        return output_dir / f"{base_name}.scad"

    def open_openscad_folder(self) -> None:
        pd = project_dirs(self.cfg)
        output_dir = pd["generated"].resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(output_dir))
            self._log(f"Opened OpenSCAD folder: {output_dir}")
            return
        messagebox.showinfo("Open Folder", f"Open this folder manually:\n{output_dir}")

    def open_model_in_openscad(self) -> None:
        scad_path = self._current_scad_path()
        if not scad_path.exists():
            messagebox.showwarning(
                "SCAD Not Found",
                f"No generated SCAD found for current prompt or name override.\nExpected: {scad_path.name}\n\nGenerate first, or set Name Override to match an existing model.",
                parent=self,
            )
            return
        self._open_in_openscad(scad_path)

    def _open_in_openscad(self, scad_path: Path) -> None:
        openscad_exe = Path(str(self.cfg["paths"].get("openscad_exe", "")))
        if not openscad_exe.exists():
            messagebox.showerror("OpenSCAD Missing", f"OpenSCAD executable not found: {openscad_exe}", parent=self)
            return
        try:
            cmd = [str(openscad_exe), str(scad_path)]
            self._log("OpenSCAD launch command:")
            self._log("  " + subprocess.list2cmdline(cmd))
            subprocess.Popen(cmd)
            self.status_var.set("Opened in OpenSCAD")
        except Exception as exc:
            messagebox.showerror("OpenSCAD Error", str(exc), parent=self)

    def start_print_again(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A generation/print job is already running.")
            return

        scad_path = self._current_scad_path()
        if not scad_path.exists():
            messagebox.showwarning(
                "SCAD Not Found",
                f"No generated SCAD found for current prompt:\n{scad_path}\n\nGenerate first, or set Name Override to match an existing model.",
            )
            return

        # We are on the UI thread here; prompt directly to avoid deadlock.
        proceed = messagebox.askyesno("Start Print", f"Run print_scad on:\n{scad_path.name} ?", parent=self)
        if not proceed:
            self._log("Print canceled from File -> Print")
            return

        self.save_ui_settings()
        self.generate_btn.configure(state=tk.DISABLED)
        self.status_var.set("Working...")

        def _run_reprint_job() -> None:
            try:
                self._log(f"Reprint requested for SCAD: {scad_path}")
                self._run_print_scad_full(scad_path)
                self._log("Reprint done")
            except Exception as exc:
                self._log(f"ERROR: {exc}")
                self._show_error_ui_thread("Replicator Error", str(exc))
            finally:
                self.after(0, self._on_worker_done)

        self.worker = threading.Thread(target=_run_reprint_job, daemon=True)
        self.worker.start()

    def view_log_file(self) -> None:
        try:
            # Ensure log file exists to avoid OS error
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.log_file_path.exists():
                self.log_file_path.write_text("", encoding="utf-8")
            if os.name == "nt":
                os.startfile(str(self.log_file_path))
            else:
                subprocess.Popen(["xdg-open", str(self.log_file_path)])
            self._log(f"Opened log file: {self.log_file_path}")
        except Exception as exc:
            messagebox.showerror("View Log Error", str(exc), parent=self)

    def capture_voice_prompt(self) -> None:
        seconds = int(self.cfg["generation"].get("voice_seconds", 8))
        model_name = str(self.cfg["generation"].get("whisper_model", "base")).strip() or "base"

        # Update UI immediately so the user sees the listening state.
        self.status_var.set(f"Listening for {seconds}s...")
        self._log(f"Whisper listening for {seconds}s with model '{model_name}'")
        self.update_idletasks()

        def _voice_worker() -> None:
            try:
                text = transcribe_prompt_with_whisper(seconds=seconds, model_name=model_name)
                if not text:
                    self.after(0, lambda: (
                        self.status_var.set("Ready"),
                        messagebox.showinfo("Voice Input", "No speech detected.", parent=self)
                    ))
                    return
                def _apply_text() -> None:
                    self.prompt_var.set(text)
                    self._log(f"Voice prompt captured: {text}")
                    self.status_var.set("Ready")
                self.after(0, _apply_text)
            except Exception as exc:
                self.after(0, lambda: (
                    self.status_var.set("Ready"),
                    messagebox.showerror("Voice Input Error", str(exc), parent=self)
                ))

        threading.Thread(target=_voice_worker, daemon=True).start()

    def start_generation(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A generation job is already running.")
            return

        prompt = self.prompt_var.get().strip()
        if not prompt:
            messagebox.showwarning("Missing Prompt", "Enter a prompt first.")
            return

        self.save_ui_settings()
        self.generate_btn.configure(state=tk.DISABLED)
        self.status_var.set("Working...")

        self.worker = threading.Thread(target=self._run_pipeline, args=(prompt,), daemon=True)
        self.worker.start()

    def _run_pipeline(self, prompt: str) -> None:
        try:
            self._log(f"Prompt: {prompt}")
            pd = project_dirs(self.cfg)
            output_dir = pd["generated"].resolve()
            output_dir.mkdir(parents=True, exist_ok=True)

            base_name_cfg = str(self.cfg["generation"].get("name", "")).strip()
            base_name = slugify(base_name_cfg if base_name_cfg else prompt)
            scad_path = output_dir / f"{base_name}.scad"
            preview_path = output_dir / f"{base_name}-preview.png"  # legacy; no longer used for Show Preview
            metadata_path = output_dir / f"{base_name}.json"

            generation_prompt = build_generation_prompt(prompt)
            if generation_prompt != prompt:
                if looks_like_name_plate_prompt(prompt):
                    self._log("Applied built-in name-plate prompt scaffold")
                elif looks_like_token_prompt(prompt):
                    self._log("Applied built-in game-token prompt scaffold")
                else:
                    self._log("Applied built-in prompt scaffold")

            dry_run = bool(self.cfg["generation"].get("dry_run", False))

            if dry_run:
                self._log("Dry run enabled; skipping model request and rendering")
                self._log(f"Would write SCAD: {scad_path}")
                self._log(f"Would render preview: {preview_path}")
                return

            if bool(self.cfg["generation"].get("offline_nameplate", False)) and looks_like_name_plate_prompt(prompt):
                requested_name = extract_requested_name(prompt) or "NAME"
                title = f"name_badge_{slugify(requested_name)}"
                description = f"Simple name plate for {requested_name}"
                scad_code = (
                    "// offline name-plate\n"
                    "plate_length=100; plate_width=30; plate_thickness=3;\n"
                    "module plate(){ cube([plate_length, plate_width, plate_thickness], center=false);}\n"
                    "module label(){ translate([plate_length/2, plate_width/2, plate_thickness])\n"
                    " linear_extrude(height=2) text(\"" + requested_name + "\", size=14, halign=\"center\", valign=\"center\", font=\"Liberation Sans:style=Bold\"); }\n"
                    "union(){ plate(); label(); }\n"
                )
                self._log("Used offline nameplate mode")
            else:
                payload = request_scad_from_openai(
                    prompt=generation_prompt,
                    api_key=resolve_api_key(self.cfg),
                    api_base=str(self.cfg["generation"]["api_base"]),
                    model=str(self.cfg["generation"]["model"]),
                    temperature=float(self.cfg["generation"]["temperature"]),
                    max_tokens=int(self.cfg["generation"]["max_tokens"]),
                )
                title, description, scad_code = extract_scad_code(payload)

            scad_code = maybe_postprocess_scad(prompt, scad_code)
            scad_path.write_text(scad_code, encoding="utf-8", newline="\n")
            write_metadata(metadata_path, prompt=prompt, model=str(self.cfg["generation"]["model"]), title=title, description=description)

            self._log(f"Generated title: {title}")
            if description:
                self._log(f"Description: {description}")
            self._log(f"SCAD: {scad_path}")

            if bool(self.show_preview_var.get()):
                # Instead of rendering a PNG and opening it in Paint,
                # launch the model directly in OpenSCAD for interactive preview/edit.
                self._open_in_openscad(scad_path)

            if bool(self.visualize_var.get()) or bool(self.print_var.get()):
                gcode_path = self._run_print_scad_slice_only(scad_path)
                if bool(self.visualize_var.get()):
                    self._run_visualize_gcode(gcode_path)

                if bool(self.print_var.get()):
                    proceed = self._ask_yes_no_ui_thread("Start Print", "Proceed with upload and start print?")
                    if proceed:
                        self._run_print_scad_full(scad_path)
                    else:
                        self._log("Print canceled after visualization")

            self._log("Done")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            self._show_error_ui_thread("Replicator Error", str(exc))
        finally:
            self.after(0, self._on_worker_done)

    def _show_error_ui_thread(self, title: str, message: str) -> None:
        self.after(0, lambda: messagebox.showerror(title, message, parent=self))

    def _ask_yes_no_ui_thread(self, title: str, message: str) -> bool:
        result_queue: queue.Queue[bool] = queue.Queue(maxsize=1)

        def _prompt() -> None:
            result = messagebox.askyesno(title, message, parent=self)
            result_queue.put(bool(result))

        self.after(0, _prompt)
        return result_queue.get()

    def _run_openscad_preview(self, scad_path: Path, preview_path: Path) -> None:
        openscad_exe = Path(str(self.cfg["paths"]["openscad_exe"]))
        if not openscad_exe.exists():
            raise FileNotFoundError(f"OpenSCAD executable not found: {openscad_exe}")
        preview_size = int(self.cfg["generation"]["preview_size"])
        cmd = [
            str(openscad_exe),
            "-o",
            str(preview_path),
            f"--imgsize={preview_size},{preview_size}",
            "--viewall",
            "--autocenter",
            "--projection=p",
            str(scad_path),
        ]
        self._log("OpenSCAD preview command:")
        self._log("  " + subprocess.list2cmdline(cmd))
        completed = run_subprocess(cmd)
        if completed.stdout:
            self._log(completed.stdout.strip())
        if completed.stderr:
            self._log(completed.stderr.strip())
        if completed.returncode != 0:
            raise RuntimeError(f"OpenSCAD preview failed with exit code {completed.returncode}")
        self._log(f"Preview: {preview_path}")

    def _build_print_scad_command(self, scad_path: Path, slice_only: bool, force_yes: bool = False) -> list[str]:
        script = Path(__file__).with_name("print_scad.py")
        cfg = self.cfg
        cmd = [sys.executable, str(script), str(scad_path)]

        host = str(cfg["printer"].get("host", "192.168.1.156")).strip()
        if host:
            cmd.extend(["--host", host])

        filament = normalize_optional_text(cfg["slicing"].get("filament_preset", ""))
        if filament:
            cmd.extend(["--filament-preset", filament])

        upload_url = normalize_optional_text(cfg["slicing"].get("upload_url", ""))
        if upload_url:
            cmd.extend(["--upload-url", upload_url])

        if bool(cfg["printer"].get("auto_level", False)):
            cmd.append("--auto-level")
        else:
            cmd.append("--no-auto-level")

        if bool(cfg["printer"].get("timelapse", False)):
            cmd.append("--timelapse")

        if force_yes or bool(cfg["printer"].get("skip_confirmation", True)):
            cmd.append("--yes")

        if bool(cfg["slicing"].get("allow_missing_thumbnail", False)):
            cmd.append("--allow-missing-thumbnail")

        cmd.extend(["--thumbnail-size", str(int(cfg["slicing"].get("thumbnail_size", 144)))])
        cmd.extend(["--ws-port", str(int(cfg["slicing"].get("ws_port", 3030)))])

        if bool(cfg["slicing"].get("ensure_heat_order", False)):
            cmd.append("--ensure-heat-order")
        if bool(cfg["slicing"].get("ensure_prime_strip", False)):
            cmd.append("--ensure-prime-strip")
        if bool(cfg["slicing"].get("sim_stub_slicer", False)):
            cmd.append("--sim-stub-slicer")

        paths = cfg["paths"]
        pd = project_dirs(cfg)
        gen_dir = pd["generated"].resolve()
        stl_dir = pd["stl"].resolve()
        gcode_dir = pd["gcode"].resolve()
        base_name = scad_path.stem
        stl_out = stl_dir / f"{base_name}.stl"
        gcode_out = gcode_dir / f"{base_name}.gcode"

        cmd.extend(["--openscad-exe", str(paths.get("openscad_exe", ""))])
        cmd.extend(["--orca-exe", str(paths.get("orca_exe", ""))])
        cmd.extend(["--orca-conf", str(paths.get("orca_conf", ""))])
        cmd.extend(["--orca-user-dir", str(paths.get("orca_user_dir", ""))])
        cmd.extend(["--orca-system-dir", str(paths.get("orca_system_dir", ""))])
        # Use generated dir for thumbnails and intermediate previews
        cmd.extend(["--build-dir", str(gen_dir)])
        # Explicit STL/G-code outputs in project folders
        cmd.extend(["--stl-output", str(stl_out)])
        cmd.extend(["--gcode-output", str(gcode_out)])

        if slice_only:
            cmd.append("--skip-upload")
            cmd.append("--skip-print")

        if bool(cfg["generation"].get("dry_run", False)):
            cmd.append("--dry-run")

        return cmd

    def _run_print_scad_slice_only(self, scad_path: Path) -> Path:
        cmd = self._build_print_scad_command(scad_path, slice_only=True)
        rc, out, err = self._run_command_streaming(cmd, label="print_scad (slice only)")
        if rc != 0:
            combined_error = ((out or "") + "\n" + (err or "")).strip()
            lower_error = combined_error.lower()
            is_slice_export_error = (
                "found slicing or export error" in lower_error
                or "slic3r::cli::run found error" in lower_error
                or "orcaslicer failed" in lower_error
            )
            if is_slice_export_error:
                self._log("Orca slicing/export error detected; requesting one-shot printability repair...")
                fixed_code = request_scad_printability_fix(
                    scad_code=scad_path.read_text(encoding="utf-8", errors="replace"),
                    error_text=combined_error,
                    api_key=resolve_api_key(self.cfg),
                    api_base=str(self.cfg["generation"]["api_base"]),
                    model=str(self.cfg["generation"]["model"]),
                )
                scad_path.write_text(fixed_code, encoding="utf-8", newline="\n")
                self._log("Applied printability repair; retrying slice once")
                retry_cmd = self._build_print_scad_command(scad_path, slice_only=True)
                rc, out, err = self._run_command_streaming(retry_cmd, label="print_scad (slice retry)")

            if rc != 0:
                raise RuntimeError(f"print_scad slice failed with exit code {rc}")

        pd = project_dirs(self.cfg)
        gcode_dir = pd["gcode"].resolve()
        base_name = scad_path.stem
        gcode_path = gcode_dir / f"{base_name}.gcode"
        if gcode_path.exists():
            self._log(f"G-code: {gcode_path}")
            return gcode_path

        # Legacy fallback: in case Orca still used default name in prior runs
        plate = gcode_dir / "plate_1.gcode"
        if plate.exists():
            self._log(f"G-code fallback: {plate}")
            return plate

        raise FileNotFoundError("G-code output not found after print_scad slice step")

    def _run_print_scad_full(self, scad_path: Path) -> None:
        cmd = self._build_print_scad_command(scad_path, slice_only=False, force_yes=True)
        rc, _out, _err = self._run_command_streaming(cmd, label="print_scad (full print)")
        if rc != 0:
            raise RuntimeError(f"print_scad full run failed with exit code {rc}")

    def _run_visualize_gcode(self, gcode_path: Path) -> None:
        script = Path(__file__).with_name("visualize_gcode.py")
        # Also save a PNG so the Log can show an inline preview.
        out_png = gcode_path.with_suffix("")
        out_png = out_png.parent / (out_png.stem + "-preview3d.png")
        cmd = [sys.executable, str(script), str(gcode_path), "--view", "3d", "--out", str(out_png)]
        self._log("visualize_gcode command:")
        self._log("  " + subprocess.list2cmdline(cmd))
        # Launch detached so the matplotlib window can stay interactive (if --out omitted). Here we still detach.
        subprocess.Popen(cmd)

    def _on_worker_done(self) -> None:
        self.generate_btn.configure(state=tk.NORMAL)
        self.status_var.set("Ready")
        self.save_ui_settings()


def main() -> int:
    app = ReplicatorApp()
    app.protocol("WM_DELETE_WINDOW", app.on_exit)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
