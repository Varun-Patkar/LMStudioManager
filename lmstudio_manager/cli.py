"""
System-tray GUI for managing LM Studio model loading.

Behavior:
- Starts in the system tray.
- Opening/closing the window does not quit the app; close hides to tray.
- Model list refreshes on startup and on explicit user actions.
- No continuous background polling to avoid interfering with inference.
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from tkinter import ttk

import httpx
import yaml

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover - import guard for runtime environment
    pystray = None
    Image = None
    ImageDraw = None


_PI_MODELS_PATH = Path.home() / ".pi" / "agent" / "models.json"
_VSCODE_MODELS_PATH = (
    Path.home() / "AppData" / "Roaming" / "Code" / "User" / "chatLanguageModels.json"
)
_CONTEXT_PREFS_PATH = Path(__file__).parent / "configs" / "context_prefs.json"


class LMStudioTrayApp:
    """Tray + window app for loading, unloading, and configuring LM Studio models."""

    def __init__(self) -> None:
        self.base_url, self.api_key = self._load_connection_config()
        self.client = self._http(self.base_url, self.api_key)

        self.models: list[dict] = []
        self.prefs = self._load_context_prefs()

        self.root = tk.Tk()
        self.root.title("LM Studio Model Manager")
        self.root.geometry("1000x560")
        self.root.protocol("WM_DELETE_WINDOW", self._hide_window)

        self.status_var = tk.StringVar(value="Ready. Click Refresh to call LM Studio API.")
        self.selected_key_var = tk.StringVar(value="")
        self.context_var = tk.StringVar(value="")
        self.busy = False

        self._build_ui()

        self.icon = None
        self._create_tray_icon()

        # First API fetch happens once on startup.
        self._refresh_models_async(reason="Loaded initial model list.")

    # ------------------------------------------------------------------
    # Config and API
    # ------------------------------------------------------------------

    def _load_connection_config(self) -> tuple[str, str]:
        """Read LM Studio connection from lmstudio_manager/configs/default.yaml."""
        config_path = Path(__file__).parent / "configs" / "default.yaml"
        base_url = "http://localhost:1234"
        api_key = "lm-studio"

        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                lms = cfg.get("lmstudio", {})
                raw_url = lms.get("base_url", base_url)
                base_url = raw_url.replace("/v1", "").rstrip("/")
                api_key = lms.get("api_key", api_key)
            except Exception:
                pass

        return base_url, api_key

    def _http(self, base_url: str, api_key: str) -> httpx.Client:
        """Create HTTP client for LM Studio native API."""
        return httpx.Client(
            base_url=base_url,
            timeout=600,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def _openai_base(self) -> str:
        """Return OpenAI-compatible base URL."""
        return self.base_url.rstrip("/") + "/v1"

    def _fetch_models(self) -> tuple[list[dict], bool]:
        """Fetch LM Studio models and connection state from /api/v1/models."""
        try:
            resp = self.client.get("/api/v1/models", timeout=8)
            if resp.status_code == 401:
                return [], False
            resp.raise_for_status()
            data = resp.json()
            return data.get("models", data.get("data", [])), True
        except Exception:
            return [], False

    def _llm_models(self, models: list[dict]) -> list[dict]:
        """Return non-embedding models only."""
        return [m for m in models if m.get("type", "llm") != "embedding"]

    def _loaded_instances(self, models: list[dict]) -> list[dict]:
        """Return all loaded instances from model list."""
        out: list[dict] = []
        for m in models:
            for inst in m.get("loaded_instances", []):
                out.append(
                    {
                        "id": inst.get("id", ""),
                        "key": m.get("key", ""),
                        "display_name": m.get("display_name", ""),
                        "config": inst.get("config", {}),
                    }
                )
        return out

    def _do_load(self, model_key: str, context_length: int | None = None) -> dict:
        """Load model via /api/v1/models/load."""
        body: dict = {"model": model_key, "echo_load_config": True}
        if context_length is not None:
            body["context_length"] = context_length

        resp = self.client.post("/api/v1/models/load", json=body, timeout=600)
        resp.raise_for_status()
        return resp.json()

    def _do_unload(self, instance_id: str) -> dict:
        """Unload model instance via /api/v1/models/unload."""
        resp = self.client.post(
            "/api/v1/models/unload", json={"instance_id": instance_id}, timeout=180
        )
        resp.raise_for_status()
        return resp.json()

    def _do_warmup(self, model_id: str) -> str:
        """Send lightweight chat prompt to initialize KV cache."""
        from openai import OpenAI

        oai = OpenAI(base_url=self._openai_base(), api_key=self.api_key, timeout=120)
        r = oai.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=32,
        )
        return r.choices[0].message.content or ""

    # ------------------------------------------------------------------
    # Preference and sync persistence
    # ------------------------------------------------------------------

    def _load_context_prefs(self) -> dict[str, int]:
        """Load saved model context preferences."""
        if not _CONTEXT_PREFS_PATH.exists():
            return {}
        try:
            with open(_CONTEXT_PREFS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            out: dict[str, int] = {}
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, int) and value > 0:
                    out[key] = value
            return out
        except Exception:
            return {}

    def _save_context_prefs(self) -> None:
        """Persist context preferences to disk."""
        try:
            _CONTEXT_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_CONTEXT_PREFS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f, indent=2)
        except Exception:
            pass

    def _preferred_context(self, model: dict) -> int:
        """Resolve effective context: user preference if set, else model max."""
        key = model.get("key", "")
        max_ctx = int(model.get("max_context_length", 0) or 0)
        if max_ctx <= 0:
            return 4096
        pref = int(self.prefs.get(key, max_ctx))
        if pref < 1:
            return max_ctx
        return min(pref, max_ctx)

    def _set_exact_context_pref(self, model: dict, raw_value: str) -> int:
        """Validate and save exact context preference for selected model."""
        key = model.get("key", "")
        max_ctx = int(model.get("max_context_length", 0) or 0)
        if not key or max_ctx <= 0:
            raise ValueError("Selected model does not expose a valid max context length.")

        try:
            requested = int(raw_value.replace(",", "").strip())
        except ValueError as exc:
            raise ValueError("Context length must be a number.") from exc

        if requested < 1024:
            requested = 1024
        if requested > max_ctx:
            requested = max_ctx

        self.prefs[key] = requested
        self._save_context_prefs()
        return requested

    def _sync_model_configs(self, llm_models: list[dict]) -> None:
        """
        Sync loaded model to PI and VS Code custom endpoint files.

        If no model is loaded, both files keep LMStudio provider but with empty models list.
        """
        loaded = [m for m in llm_models if m.get("loaded_instances")]
        pi_models: list[dict] = []
        vsc_models: list[dict] = []

        for m in loaded:
            key = m.get("key", "")
            name = m.get("display_name", key)
            inst = m["loaded_instances"][0]
            ctx = inst.get("config", {}).get(
                "context_length", m.get("max_context_length", 131072)
            )
            caps = m.get("capabilities", {})
            has_vision = caps.get("vision", False)
            has_reasoning = bool(caps.get("reasoning", {}).get("allowed_options", []))
            has_tools = caps.get("trained_for_tool_use", False)

            inputs = ["text"]
            if has_vision:
                inputs.append("image")

            pi_models.append(
                {
                    "id": key,
                    "name": name,
                    "reasoning": has_reasoning,
                    "input": inputs,
                    "contextWindow": ctx,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                }
            )

            vsc_models.append(
                {
                    "id": key,
                    "name": name,
                    "url": self.base_url.rstrip("/") + "/v1",
                    "toolCalling": has_tools,
                    "vision": has_vision,
                    "maxInputTokens": ctx,
                    "maxOutputTokens": min(16384, max(2048, ctx // 4)),
                }
            )

        try:
            pi_data: dict = {"providers": {}}
            if _PI_MODELS_PATH.exists():
                with open(_PI_MODELS_PATH, "r", encoding="utf-8") as f:
                    pi_data = json.load(f)

            pi_data.setdefault("providers", {})
            pi_data["providers"]["lmstudio"] = {
                "baseUrl": self.base_url.rstrip("/") + "/v1",
                "api": "openai-completions",
                "apiKey": self.api_key,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                    "supportsStrictMode": False,
                    "thinkingFormat": "qwen-chat-template",
                },
                "models": pi_models,
            }

            _PI_MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_PI_MODELS_PATH, "w", encoding="utf-8") as f:
                json.dump(pi_data, f, indent=2)
        except Exception:
            pass

        try:
            vsc_data: list = []
            if _VSCODE_MODELS_PATH.exists():
                with open(_VSCODE_MODELS_PATH, "r", encoding="utf-8") as f:
                    vsc_data = json.load(f)

            lmstudio_entry = None
            for entry in vsc_data:
                if entry.get("vendor") == "customendpoint" and entry.get("name") == "LMStudio":
                    lmstudio_entry = entry
                    break

            if lmstudio_entry:
                lmstudio_entry["models"] = vsc_models
                lmstudio_entry["apiKey"] = self.api_key
            else:
                vsc_data.append(
                    {
                        "name": "LMStudio",
                        "vendor": "customendpoint",
                        "apiKey": self.api_key,
                        "apiType": "responses",
                        "models": vsc_models,
                    }
                )

            _VSCODE_MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_VSCODE_MODELS_PATH, "w", encoding="utf-8") as f:
                json.dump(vsc_data, f, indent="\t")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI and tray
    # ------------------------------------------------------------------

    def _apply_theme(self) -> None:
        """Apply a dark colour theme to the app."""
        BG = "#1e232a"
        FG = "#e0e4ea"
        ACCENT = "#2284e6"
        ACCENT_HOVER = "#3a9af5"
        FIELD_BG = "#282e38"
        TREE_BG = "#22272e"
        TREE_SEL = "#2a4a6e"
        HEADING_BG = "#2a3040"
        GREEN = "#2ea043"
        GREEN_FG = "#c8f7d0"
        AMBER = "#d29922"
        RED = "#da3633"

        self.root.configure(bg=BG)

        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD_BG,
                        bordercolor=BG, troughcolor=FIELD_BG, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("TEntry", fieldbackground=FIELD_BG, foreground=FG,
                        insertcolor=FG)

        # Accent button
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        font=("Segoe UI", 10, "bold"), padding=(12, 6))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_HOVER), ("pressed", ACCENT)],
                  foreground=[("disabled", "#888")])

        # Green button (load)
        style.configure("Green.TButton", background=GREEN, foreground="#ffffff",
                        font=("Segoe UI", 10, "bold"), padding=(12, 6))
        style.map("Green.TButton",
                  background=[("active", "#3cb653"), ("pressed", GREEN)])

        # Red button (unload)
        style.configure("Red.TButton", background=RED, foreground="#ffffff",
                        font=("Segoe UI", 10, "bold"), padding=(12, 6))
        style.map("Red.TButton",
                  background=[("active", "#e5534b"), ("pressed", RED)])

        # Amber button (warmup)
        style.configure("Amber.TButton", background=AMBER, foreground="#ffffff",
                        font=("Segoe UI", 10, "bold"), padding=(12, 6))
        style.map("Amber.TButton",
                  background=[("active", "#e0a82e"), ("pressed", AMBER)])

        # Default button style
        style.configure("TButton", background="#3d444d", foreground=FG,
                        font=("Segoe UI", 10), padding=(12, 6))
        style.map("TButton",
                  background=[("active", "#4a535e"), ("pressed", "#3d444d")])

        # Treeview
        style.configure("Treeview", background=TREE_BG, foreground=FG,
                        fieldbackground=TREE_BG, rowheight=28,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=HEADING_BG,
                        foreground="#ffffff", font=("Segoe UI", 10, "bold"),
                        relief="flat")
        style.map("Treeview",
                  background=[("selected", TREE_SEL)],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading",
                  background=[("active", "#354060")])

        # Status label
        style.configure("Status.TLabel", background="#282e38", foreground="#8b949e",
                        font=("Segoe UI", 9), padding=(8, 4))

        # Store colours for row tags
        self._colors = {
            "green": GREEN, "green_fg": GREEN_FG,
            "tree_bg": TREE_BG, "fg": FG,
        }

    def _build_ui(self) -> None:
        """Build main management window widgets."""
        self._apply_theme()
        self.root.minsize(920, 500)

        wrapper = ttk.Frame(self.root, padding=10)
        wrapper.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(wrapper)
        top.pack(fill=tk.X)

        ttk.Button(top, text="⟳ Refresh", style="Accent.TButton",
                   command=self.refresh).pack(side=tk.LEFT)
        ttk.Button(top, text="▶ Load Selected", style="Green.TButton",
                   command=self.load_selected).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="■ Unload", style="Red.TButton",
                   command=self.unload_loaded).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="☄ Warmup", style="Amber.TButton",
                   command=self.warmup_loaded).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="Hide To Tray", command=self._hide_window).pack(
            side=tk.RIGHT
        )

        columns = ("status", "key", "name", "quant", "size", "context")
        tree = ttk.Treeview(wrapper, columns=columns, show="headings", height=16,
                            selectmode="browse")
        self.tree = tree

        tree.heading("status", text="Status")
        tree.heading("key", text="Model")
        tree.heading("name", text="Name")
        tree.heading("quant", text="Quant")
        tree.heading("size", text="Size")
        tree.heading("context", text="Context")

        tree.column("status", width=95, anchor=tk.CENTER)
        tree.column("key", width=290)
        tree.column("name", width=220)
        tree.column("quant", width=90, anchor=tk.CENTER)
        tree.column("size", width=90, anchor=tk.E)
        tree.column("context", width=190, anchor=tk.E)

        # Row tags for loaded (green) vs idle
        tree.tag_configure("loaded", background=self._colors["green"],
                           foreground=self._colors["green_fg"])
        tree.tag_configure("idle", background=self._colors["tree_bg"],
                           foreground=self._colors["fg"])

        tree.bind("<<TreeviewSelect>>", self._on_select)

        # Pack bottom bar and status BEFORE tree so they are never clipped
        # when the window is shrunk vertically.
        bottom = ttk.Frame(wrapper)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))

        status = ttk.Label(wrapper, textvariable=self.status_var, anchor=tk.W,
                           style="Status.TLabel")
        status.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))

        ttk.Label(bottom, text="Selected Model:").pack(side=tk.LEFT)
        ttk.Label(bottom, textvariable=self.selected_key_var).pack(side=tk.LEFT, padx=(6, 20))

        ttk.Label(bottom, text="Preferred Context:").pack(side=tk.LEFT)
        ctx_entry = ttk.Entry(bottom, textvariable=self.context_var, width=14)
        ctx_entry.pack(side=tk.LEFT, padx=(6, 0))
        self.ctx_entry = ctx_entry

        ttk.Button(bottom, text="Set Exact Context", command=self.set_exact_context).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        # Tree fills remaining space — packed last so it shrinks first
        tree.pack(fill=tk.BOTH, expand=True, pady=(10, 8))

    def _create_tray_icon(self) -> None:
        """Create and run tray icon in detached mode."""
        if pystray is None or Image is None or ImageDraw is None:
            self.status_var.set(
                "pystray/Pillow not installed. Run: pip install pystray pillow"
            )
            return

        image = self._build_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem("Open", self._tray_open, default=True),
            pystray.MenuItem("Refresh", self._tray_refresh),
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self.icon = pystray.Icon("lmstudio_manager", image, "LM Studio Manager", menu)
        self.icon.run_detached()

    def _build_icon_image(self):
        """Create a small in-memory tray icon image."""
        img = Image.new("RGB", (64, 64), color=(30, 35, 42))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=(34, 132, 230))
        draw.rectangle((18, 20, 46, 44), fill=(235, 240, 246))
        draw.rectangle((22, 24, 42, 40), fill=(34, 132, 230))
        return img

    def _tray_open(self, icon=None, item=None) -> None:
        """Show and focus app window from tray."""
        self.root.after(0, self._show_window)

    def _tray_refresh(self, icon=None, item=None) -> None:
        """Refresh from tray menu."""
        self.root.after(0, self.refresh)

    def _tray_quit(self, icon=None, item=None) -> None:
        """Quit entire app from tray menu."""
        self.root.after(0, self.quit)

    def _show_window(self) -> None:
        """Restore hidden window."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_window(self) -> None:
        """Hide window without quitting app."""
        self.root.withdraw()
        self.status_var.set("Window hidden. App is still running in system tray.")

    # ------------------------------------------------------------------
    # Model table and selection
    # ------------------------------------------------------------------

    def _render_models(self) -> None:
        """Render model rows into Treeview using current model data."""
        for row in self.tree.get_children():
            self.tree.delete(row)

        llms = self._llm_models(self.models)
        for m in llms:
            key = m.get("key", "?")
            name = m.get("display_name", key)
            quant = m.get("quantization", {}).get("name", "?")
            size_bytes = int(m.get("size_bytes", 0) or 0)
            size_str = f"{size_bytes / (1024 ** 3):.1f} GB" if size_bytes else "?"
            max_ctx = int(m.get("max_context_length", 0) or 0)
            pref_ctx = self._preferred_context(m)

            insts = m.get("loaded_instances", [])
            if insts:
                loaded_ctx = int(insts[0].get("config", {}).get("context_length", max_ctx) or max_ctx)
                status = "✅ LOADED"
                ctx = f"{loaded_ctx:,} (loaded)"
                row_tag = "loaded"
            else:
                status = "—"
                if pref_ctx == max_ctx:
                    ctx = f"{pref_ctx:,} (max)"
                else:
                    ctx = f"{pref_ctx:,} (pref)"
                row_tag = "idle"

            self.tree.insert(
                "", tk.END, iid=key, values=(status, key, name, quant, size_str, ctx),
                tags=(row_tag,)
            )

        if llms:
            selected = self.tree.selection()
            if not selected:
                self.tree.selection_set(llms[0].get("key", ""))
                self._on_select()

    def _on_select(self, event=None) -> None:
        """Update selection-bound fields when table selection changes."""
        selected = self.tree.selection()
        if not selected:
            self.selected_key_var.set("")
            self.context_var.set("")
            return

        key = selected[0]
        model = next((m for m in self._llm_models(self.models) if m.get("key") == key), None)
        if model is None:
            return

        self.selected_key_var.set(key)
        self.context_var.set(f"{self._preferred_context(model)}")

    # ------------------------------------------------------------------
    # Async operation helpers
    # ------------------------------------------------------------------

    def _run_bg(self, fn) -> None:
        """Run background function if app is idle."""
        if self.busy:
            return

        def _runner():
            self.busy = True
            try:
                fn()
            finally:
                self.busy = False

        threading.Thread(target=_runner, daemon=True).start()

    def _refresh_models_async(self, reason: str = "Refreshed model list.") -> None:
        """Fetch models once in background and refresh UI."""

        def _task() -> None:
            models, connected = self._fetch_models()
            if not connected:
                self.root.after(
                    0,
                    lambda: self.status_var.set(
                        "Could not connect/auth to LM Studio API. Check server and API key."
                    ),
                )
                return

            self.models = models
            self._sync_model_configs(self._llm_models(self.models))
            self.root.after(0, self._render_models)
            self.root.after(0, lambda: self.status_var.set(reason))

        self._run_bg(_task)

    # ------------------------------------------------------------------
    # Button actions
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Manual refresh button action."""
        self.status_var.set("Refreshing from LM Studio API...")
        self._refresh_models_async(reason="Refreshed from API.")

    def set_exact_context(self) -> None:
        """Persist exact context preference for currently selected model."""
        key = self.selected_key_var.get().strip()
        if not key:
            messagebox.showwarning("No model selected", "Select a model first.")
            return

        model = next((m for m in self._llm_models(self.models) if m.get("key") == key), None)
        if model is None:
            messagebox.showerror("Model not found", "Selected model is no longer available.")
            return

        try:
            applied = self._set_exact_context_pref(model, self.context_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid context", str(exc))
            return

        self.context_var.set(str(applied))
        self._render_models()
        self.status_var.set(f"Saved context preference for {key}: {applied:,}")

    def load_selected(self) -> None:
        """Unload current, load selected model with preferred context, then warmup."""
        key = self.selected_key_var.get().strip()
        if not key:
            messagebox.showwarning("No model selected", "Select a model to load.")
            return

        model = next((m for m in self._llm_models(self.models) if m.get("key") == key), None)
        if model is None:
            messagebox.showerror("Model not found", "Selected model is no longer available.")
            return

        def _task() -> None:
            try:
                self.root.after(0, lambda: self.status_var.set("Preparing model switch..."))

                loaded = self._loaded_instances(self.models)
                if loaded:
                    self._do_unload(loaded[0]["id"])

                chosen_ctx = self._preferred_context(model)
                self._do_load(key, context_length=chosen_ctx)

                # Refresh once after load and sync config files.
                self.models, _ = self._fetch_models()
                self._sync_model_configs(self._llm_models(self.models))

                loaded_after = self._loaded_instances(self.models)
                instance_id = loaded_after[0]["id"] if loaded_after else key

                try:
                    self._do_warmup(instance_id)
                    msg = f"Loaded {key} with ctx={chosen_ctx:,} and completed warmup."
                except Exception as warm_err:
                    msg = (
                        f"Loaded {key} with ctx={chosen_ctx:,}. Warmup failed: {warm_err}"
                    )

                self.root.after(0, self._render_models)
                self.root.after(0, lambda: self.status_var.set(msg))
            except Exception as exc:
                self.root.after(0, lambda: self.status_var.set(f"Load failed: {exc}"))

        self._run_bg(_task)

    def unload_loaded(self) -> None:
        """Unload currently loaded model if present."""

        def _task() -> None:
            try:
                loaded = self._loaded_instances(self.models)
                if not loaded:
                    self.root.after(0, lambda: self.status_var.set("Nothing is loaded."))
                    return

                self._do_unload(loaded[0]["id"])
                self.models, _ = self._fetch_models()
                self._sync_model_configs(self._llm_models(self.models))
                self.root.after(0, self._render_models)
                self.root.after(0, lambda: self.status_var.set("Unloaded current model."))
            except Exception as exc:
                self.root.after(0, lambda: self.status_var.set(f"Unload failed: {exc}"))

        self._run_bg(_task)

    def warmup_loaded(self) -> None:
        """Run warmup call against currently loaded model."""

        def _task() -> None:
            try:
                loaded = self._loaded_instances(self.models)
                if not loaded:
                    self.root.after(0, lambda: self.status_var.set("Nothing is loaded."))
                    return

                instance_id = loaded[0]["id"]
                self._do_warmup(instance_id)
                self.root.after(0, lambda: self.status_var.set("Warmup completed."))
            except Exception as exc:
                self.root.after(0, lambda: self.status_var.set(f"Warmup failed: {exc}"))

        self._run_bg(_task)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run GUI event loop."""
        self.root.mainloop()

    def quit(self) -> None:
        """Quit app and remove tray icon."""
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass

        try:
            self.client.close()
        except Exception:
            pass

        self.root.quit()
        self.root.destroy()


def cli() -> None:
    """Entry point for lmstudio executable."""
    if pystray is None or Image is None or ImageDraw is None:
        raise RuntimeError(
            "Missing GUI dependencies. Install with: pip install pystray pillow"
        )

    app = LMStudioTrayApp()
    app.run()


if __name__ == "__main__":
    cli()
