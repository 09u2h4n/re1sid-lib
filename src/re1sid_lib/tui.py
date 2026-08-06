"""
re1sid-lib TUI
==============

A Textual-based terminal user interface for `re1sid-lib`
(https://pypi.org/project/re1sid-lib/), a thin Python wrapper around the
ReVanced CLI for downloading patch resources and patching Android APKs.

Written from scratch against the public `Downloader` / `Patcher` API of the
library (does not reuse any code from re1sid_lib's own tui.py).

Features
--------
- Resource status / one-click download of the ReVanced CLI + patch bundle.
- List patches, either for every package or filtered to a single package
  name, with a detail pane (description, compatible packages/versions,
  options).
- Patch an APK: point at a file, auto-detect its package, toggle which
  compatible patches are enabled/disabled, edit each patch's options
  (booleans, choices, arrays, free text) and run the patch job with a live
  streamed log.

Run with:  python tui.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Header,
    Footer,
    Static,
    Button,
    Input,
    ListView,
    ListItem,
    Label,
    Checkbox,
    RichLog,
    Select,
    Rule,
    DirectoryTree,
)

from .downloader import Downloader
from .patcher import Patcher
from .common import PATCHES_PATH, CLI_PATH, OUTPUT_DIR


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def resources_present() -> bool:
    """True if the ReVanced CLI jar and patch bundle have been downloaded."""
    return os.path.exists(CLI_PATH) and os.path.exists(PATCHES_PATH)


def is_array_type(option_type: str) -> bool:
    return "array" in (option_type or "").lower()


def is_bool_type(option_type: str) -> bool:
    return "bool" in (option_type or "").lower()


def format_value_for_input(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


# --------------------------------------------------------------------------- #
# Modal: edit the options for a single patch
# --------------------------------------------------------------------------- #

class OptionEditModal(ModalScreen[Optional[dict]]):
    """Modal that lets the user edit every option of one patch.

    Dismisses with a dict of {option_name: value} on save, or None on cancel.
    """

    CSS = """
    OptionEditModal {
        align: center middle;
    }
    #option-modal-box {
        width: 80%;
        max-width: 100;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #option-modal-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    .opt-row {
        height: auto;
        padding: 0 0 1 0;
    }
    .opt-label {
        text-style: bold;
    }
    .opt-desc {
        color: $text-muted;
        padding-bottom: 1;
    }
    #option-modal-buttons {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }
    """

    def __init__(self, patch_name: str, options: list[dict], current_values: dict) -> None:
        super().__init__()
        self.patch_name = patch_name
        self.options = options
        self.current_values = current_values
        # widget id -> option dict, so we know how to read the value back out
        self._widget_for_option: dict[str, tuple[dict, str]] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="option-modal-box"):
            yield Label(f"Options — {self.patch_name}", id="option-modal-title")
            with VerticalScroll():
                for i, opt in enumerate(self.options):
                    name = opt.get("Name", f"option_{i}")
                    field_id = f"opt-field-{i}"
                    self._widget_for_option[field_id] = (opt, name)
                    with Vertical(classes="opt-row"):
                        req = " (required)" if opt.get("Required") else ""
                        yield Label(f"{name}{req}", classes="opt-label")
                        if opt.get("Description"):
                            yield Static(opt["Description"], classes="opt-desc")
                        yield self._build_field(opt, field_id)
            with Horizontal(id="option-modal-buttons"):
                yield Button("Cancel", id="opt-cancel", variant="default")
                yield Button("Save", id="opt-save", variant="primary")

    def _build_field(self, opt: dict, field_id: str):
        name = opt.get("Name", "")
        opt_type = opt.get("Type", "")
        possible = opt.get("Possible values") or []
        current = self.current_values.get(name, opt.get("Default"))

        if is_bool_type(opt_type):
            value = current if isinstance(current, bool) else bool(current) if current else False
            return Checkbox("Enabled", value=value, id=field_id)

        if possible and not is_array_type(opt_type):
            select_options = [(str(p), str(p)) for p in possible]
            value = str(current) if current not in (None, "") else Select.BLANK
            return Select(select_options, value=value, id=field_id, allow_blank=True)

        placeholder = "comma, separated, values" if is_array_type(opt_type) else "value"
        return Input(value=format_value_for_input(current), placeholder=placeholder, id=field_id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "opt-cancel":
            self.dismiss(None)
            return

        if event.button.id == "opt-save":
            result: dict[str, Any] = {}
            for field_id, (opt, name) in self._widget_for_option.items():
                widget = self.query_one(f"#{field_id}")
                opt_type = opt.get("Type", "")
                if isinstance(widget, Checkbox):
                    result[name] = widget.value
                elif isinstance(widget, Select):
                    if widget.value is Select.BLANK:
                        continue
                    result[name] = widget.value
                elif isinstance(widget, Input):
                    raw = widget.value.strip()
                    if not raw:
                        continue
                    if is_array_type(opt_type):
                        result[name] = [v.strip() for v in raw.split(",") if v.strip()]
                    else:
                        result[name] = raw
            self.dismiss(result)


# --------------------------------------------------------------------------- #
# Widget: a single patch row (used both in "list patches" and "patch apk")
# --------------------------------------------------------------------------- #

class PatchRow(ListItem):
    """One patch entry. In selectable mode it shows a checkbox to
    enable/disable, plus an "Options" button when the patch has options."""

    def __init__(self, patch: dict, selectable: bool = False, enabled: bool = True) -> None:
        super().__init__()
        self.patch = patch
        self.selectable = selectable
        self.enabled = enabled
        self.option_values: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        name = self.patch.get("Name", "(unnamed)")
        idx = self.patch.get("Index")
        n_opts = len(self.patch.get("Options") or [])
        opt_tag = f" [dim]· {n_opts} option{'s' if n_opts != 1 else ''}[/]" if n_opts else ""
        with Horizontal(classes="patch-row"):
            if self.selectable:
                yield Checkbox(value=self.enabled, id="patch-toggle")
            yield Label(f"#{idx}  {name}{opt_tag}", classes="patch-row-label")
            if self.selectable and n_opts:
                yield Button("Options", id="patch-options-btn", classes="patch-options-btn")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "patch-toggle":
            self.enabled = event.value
            event.stop()


# --------------------------------------------------------------------------- #
# Screen: Main menu
# --------------------------------------------------------------------------- #

class MainMenu(Screen):
    BINDINGS = [Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="menu-box"):
            yield Static("re1sid-lib TUI", id="menu-title")
            yield Static("", id="resource-status")
            yield Button("List patches", id="menu-list", variant="primary")
            yield Button("Patch an APK", id="menu-patch", variant="primary")
            yield Button("Download / update resources", id="menu-download")
            yield Button("Quit", id="menu-quit", variant="error")
        yield Footer()

    def on_screen_resume(self) -> None:
        self._refresh_status()

    def on_mount(self) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        ok = resources_present()
        status = self.query_one("#resource-status", Static)
        if ok:
            status.update("[green]✔ ReVanced CLI + patch bundle found[/]")
        else:
            status.update(
                "[yellow]⚠ ReVanced CLI / patch bundle not found — "
                "use 'Download / update resources' first[/]"
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "menu-list":
            self.app.push_screen(ListPatchesScreen())
        elif event.button.id == "menu-patch":
            self.app.push_screen(PatchApkScreen())
        elif event.button.id == "menu-download":
            self.app.push_screen(DownloadScreen())
        elif event.button.id == "menu-quit":
            self.app.exit()


# --------------------------------------------------------------------------- #
# Screen: Download resources
# --------------------------------------------------------------------------- #

class DownloadScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="download-box"):
            yield Static("Download ReVanced CLI + patch bundle", id="download-title")
            yield Static(f"CLI target:     {CLI_PATH}")
            yield Static(f"Patches target: {PATCHES_PATH}")
            yield Button("Start download", id="dl-start", variant="primary")
            yield Button("Back", id="dl-back")
            yield RichLog(id="dl-log", wrap=True, highlight=True)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dl-start":
            self.notify("Starting download...")
            event.button.disabled = True
            self.run_download()
        elif event.button.id == "dl-back":
            self.app.pop_screen()

    @work(thread=True, exclusive=True)
    def run_download(self) -> None:
        log = self.query_one("#dl-log", RichLog)
        start_btn = self.query_one("#dl-start", Button)
        downloader = Downloader()
        self.app.call_from_thread(log.write, "[bold]Downloading ReVanced CLI...[/bold]")
        try:
            downloader.download_cli()
            self.app.call_from_thread(log.write, "[green]✔ CLI downloaded[/green]")
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(log.write, f"[red]✘ CLI download failed: {exc}[/red]")
            self.app.call_from_thread(self.notify, f"CLI download failed: {exc}", severity="error", timeout=8)
            self.app.call_from_thread(setattr, start_btn, "disabled", False)
            return

        self.app.call_from_thread(log.write, "[bold]Downloading patch bundle...[/bold]")
        try:
            downloader.download_patches_rvp()
            self.app.call_from_thread(log.write, "[green]✔ Patch bundle downloaded[/green]")
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(log.write, f"[red]✘ Patch bundle download failed: {exc}[/red]")
            self.app.call_from_thread(self.notify, f"Patch bundle download failed: {exc}", severity="error", timeout=8)
            self.app.call_from_thread(setattr, start_btn, "disabled", False)
            return

        self.app.call_from_thread(log.write, "[bold green]All resources ready.[/bold green]")
        self.app.call_from_thread(self.notify, "Resources downloaded successfully.", severity="information")
        self.app.call_from_thread(setattr, start_btn, "disabled", False)


# --------------------------------------------------------------------------- #
# Screen: List patches (read-only browser)
# --------------------------------------------------------------------------- #

class ListPatchesScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="list-box"):
            yield Static("List patches - leave the field empty to list every patch", id="list-title")
            with Horizontal(id="list-search-row"):
                yield Input(placeholder="Package name (optional), e.g. com.google.android.youtube", id="list-pkg-input")
                yield Button("Search", id="list-search-btn", variant="primary")
            with Horizontal(id="list-body"):
                yield ListView(id="list-patches-view")
                with VerticalScroll(id="list-detail-pane"):
                    yield Static("Select a patch to see details.", id="list-detail")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "list-search-btn":
            self.do_search()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "list-pkg-input":
            self.do_search()

    def do_search(self) -> None:
        if not resources_present():
            msg = "Resources not downloaded yet. Go back and use 'Download / update resources' first."
            self.query_one("#list-detail", Static).update(f"[yellow]{msg}[/yellow]")
            self.notify(msg, severity="warning")
            return
        pkg = self.query_one("#list-pkg-input", Input).value.strip() or None
        self.notify(f"Searching patches for {pkg}..." if pkg else "Loading all patches...")
        self.fetch_patches(pkg)

    @work(thread=True, exclusive=True)
    def fetch_patches(self, package_name: Optional[str]) -> None:
        view = self.query_one("#list-patches-view", ListView)
        detail = self.query_one("#list-detail", Static)
        self.app.call_from_thread(detail.update, "[dim]Loading patches...[/dim]")
        patcher = Patcher()
        try:
            patches = patcher.list_patches(package_name=package_name)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(detail.update, f"[red]Failed to list patches: {exc}[/red]")
            self.app.call_from_thread(self.notify, f"Failed to list patches: {exc}", severity="error", timeout=8)
            return

        def populate() -> None:
            view.clear()
            for patch in patches:
                view.append(PatchRow(patch, selectable=False))
            if not patches:
                detail.update("[yellow]No patches found for that package.[/yellow]")
                self.notify("No patches found for that package.", severity="warning")
            else:
                detail.update(f"[dim]{len(patches)} patch(es) loaded. Select one for details.[/dim]")
                self.notify(f"Loaded {len(patches)} patch(es).")

        self.app.call_from_thread(populate)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "list-patches-view":
            return
        item = event.item
        if isinstance(item, PatchRow):
            self.query_one("#list-detail", Static).update(self._render_detail(item.patch))

    @staticmethod
    def _render_detail(patch: dict) -> str:
        lines = [
            f"[bold]{patch.get('Name', '?')}[/bold]  (index {patch.get('Index')})",
            f"Enabled by default: {'yes' if patch.get('Enabled') else 'no'}",
            "",
            patch.get("Description") or "[dim]No description.[/dim]",
            "",
        ]
        opts = patch.get("Options") or []
        if opts:
            lines.append("[bold underline]Options[/bold underline]")
            for opt in opts:
                req = " [red](required)[/red]" if opt.get("Required") else ""
                lines.append(f"• [bold]{opt.get('Name')}[/bold]{req} — {opt.get('Type', '?')}")
                if opt.get("Description"):
                    lines.append(f"    {opt['Description']}")
                if opt.get("Default") not in (None, ""):
                    lines.append(f"    default: {opt['Default']}")
                if opt.get("Possible values"):
                    lines.append(f"    choices: {', '.join(opt['Possible values'])}")
            lines.append("")

        pkgs = patch.get("Compatible packages") or []
        if pkgs:
            lines.append("[bold underline]Compatible packages[/bold underline]")
            for pkg in pkgs:
                versions = pkg.get("Compatible versions") or []
                v_txt = ", ".join(versions) if versions else "all versions"
                lines.append(f"• {pkg.get('Package name')} ({v_txt})")
        else:
            lines.append("[dim]Compatible with all packages.[/dim]")

        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Modal: APK file picker (a directory tree filtered to folders + .apk files)
# --------------------------------------------------------------------------- #

class ApkDirectoryTree(DirectoryTree):
    """A DirectoryTree that only shows directories and .apk files."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [
            p for p in paths
            if p.is_dir() or p.suffix.lower() == ".apk"
        ]


class ApkFilePickerModal(ModalScreen[Optional[str]]):
    """Browse the filesystem and pick an .apk file.

    Dismisses with the chosen path (str) on selection/confirm, or None on cancel.
    """

    CSS = """
    ApkFilePickerModal {
        align: center middle;
    }
    #picker-box {
        width: 90%;
        max-width: 120;
        height: 85%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #picker-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #picker-path-row {
        height: auto;
        padding-bottom: 1;
    }
    #picker-path-row Input {
        width: 1fr;
    }
    #picker-tree {
        height: 1fr;
        border: round $accent;
    }
    #picker-selected {
        padding: 1 0;
        color: $text-muted;
    }
    #picker-buttons {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, start_path: Optional[str] = None) -> None:
        super().__init__()
        self.start_path = start_path or os.getcwd()
        self.selected_path: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("Pick an APK file", id="picker-title")
            with Horizontal(id="picker-path-row"):
                yield Input(value=self.start_path, placeholder="Jump to path...", id="picker-goto-input")
                yield Button("Go", id="picker-goto-btn")
            yield ApkDirectoryTree(self.start_path, id="picker-tree")
            yield Static("No file selected.", id="picker-selected")
            with Horizontal(id="picker-buttons"):
                yield Button("Cancel", id="picker-cancel", variant="default")
                yield Button("Select", id="picker-select", variant="primary", disabled=True)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker-cancel":
            self.dismiss(None)
        elif event.button.id == "picker-select":
            self.dismiss(self.selected_path)
        elif event.button.id == "picker-goto-btn":
            self._goto_path()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "picker-goto-input":
            self._goto_path()

    def _goto_path(self) -> None:
        raw = self.query_one("#picker-goto-input", Input).value.strip()
        if not raw:
            return
        path = Path(raw).expanduser()
        if path.is_dir():
            tree = self.query_one("#picker-tree", ApkDirectoryTree)
            tree.path = path
            self.notify(f"Browsing {path}")
        elif path.is_file() and path.suffix.lower() == ".apk":
            self.selected_path = str(path)
            self.query_one("#picker-selected", Static).update(f"[green]Selected:[/green] {path}")
            self.query_one("#picker-select", Button).disabled = False
        else:
            self.notify(f"Not a directory or .apk file: {path}", severity="warning")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        path = str(event.path)
        self.selected_path = path
        self.query_one("#picker-selected", Static).update(f"[green]Selected:[/green] {path}")
        self.query_one("#picker-select", Button).disabled = False

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        # Just navigating; nothing to select yet.
        pass


# --------------------------------------------------------------------------- #
# Screen: Patch an APK
# --------------------------------------------------------------------------- #

class PatchApkScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self.apk_path: Optional[str] = None
        self.package_name: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="patch-box"):
            yield Static("Patch an APK", id="patch-title")
            with Horizontal(id="patch-apk-row"):
                yield Input(placeholder="Path to .apk file", id="patch-apk-input")
                yield Button("Browse...", id="patch-browse-btn")
                yield Button("Load", id="patch-load-btn", variant="primary")
            yield Static("", id="patch-apk-info")
            yield Rule()
            yield ListView(id="patch-list-view")
            yield Rule()
            with Vertical(id="patch-run-config"):
                yield Input(placeholder=f"Output path (default: {OUTPUT_DIR}/<name>-patched.apk)", id="patch-output-input")
                with Horizontal(id="patch-flags-row"):
                    yield Checkbox("Exclusive (only enabled patches)", id="patch-flag-exclusive", value=False)
                    yield Checkbox("Force (ignore compatibility)", id="patch-flag-force", value=False)
                    yield Checkbox("Bypass verification", id="patch-flag-bypass", value=True)
                    yield Checkbox("Purge temp files", id="patch-flag-purge", value=True)
                yield Button("Run patch", id="patch-run-btn", variant="success")
            yield RichLog(id="patch-log", wrap=True, highlight=True)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "patch-load-btn":
            self.load_apk()
        elif event.button.id == "patch-browse-btn":
            self.browse_for_apk()
        elif event.button.id == "patch-run-btn":
            self.run_patch()
        elif event.button.id == "patch-options-btn":
            row = event.button
            item = row
            while item is not None and not isinstance(item, PatchRow):
                item = item.parent
            if isinstance(item, PatchRow):
                self.edit_options(item)

    def browse_for_apk(self) -> None:
        current = self.query_one("#patch-apk-input", Input).value.strip()
        start_dir = os.path.dirname(current) if current and os.path.exists(os.path.dirname(current) or ".") else os.getcwd()

        def handle_pick(path: Optional[str]) -> None:
            if not path:
                return
            self.query_one("#patch-apk-input", Input).value = path
            self.notify(f"Picked {os.path.basename(path)}")
            self.apk_path = path
            self.load_apk()

        self.app.push_screen(ApkFilePickerModal(start_dir), handle_pick)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "patch-apk-input":
            self.load_apk()

    def load_apk(self) -> None:
        path = self.query_one("#patch-apk-input", Input).value.strip()
        info = self.query_one("#patch-apk-info", Static)
        if not path:
            info.update("[yellow]Enter a path to an APK first.[/yellow]")
            self.notify("Enter a path to an APK first.", severity="warning")
            return
        if not os.path.exists(path):
            info.update(f"[red]File not found: {path}[/red]")
            self.notify(f"File not found: {path}", severity="error")
            return
        if not resources_present():
            msg = "Resources not downloaded yet — go to the main menu and download them first."
            info.update(f"[yellow]{msg}[/yellow]")
            self.notify(msg, severity="warning")
            return
        self.apk_path = path
        self.notify(f"Loading {os.path.basename(path)}...")
        self.fetch_apk_patches(path)

    @work(thread=True, exclusive=True)
    def fetch_apk_patches(self, path: str) -> None:
        info = self.query_one("#patch-apk-info", Static)
        self.app.call_from_thread(info.update, "[dim]Reading APK metadata...[/dim]")
        patcher = Patcher()
        try:
            apk_info = patcher.get_apk_info(path)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(info.update, f"[red]Failed to read APK: {exc}[/red]")
            self.app.call_from_thread(self.notify, f"Failed to read APK: {exc}", severity="error", timeout=8)
            return

        self.package_name = apk_info["package_name"]
        self.app.call_from_thread(
            info.update,
            f"[green]{apk_info['package_name']}[/green]  v{apk_info.get('version_name', '?')}"
            "  — loading compatible patches...",
        )

        try:
            patches = patcher.list_patches(package_name=self.package_name)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(info.update, f"[red]Failed to list patches: {exc}[/red]")
            self.app.call_from_thread(self.notify, f"Failed to list patches: {exc}", severity="error", timeout=8)
            return

        def populate() -> None:
            view = self.query_one("#patch-list-view", ListView)
            view.clear()
            for patch in patches:
                view.append(PatchRow(patch, selectable=True, enabled=bool(patch.get("Enabled"))))
            info.update(
                f"[green]{apk_info['package_name']}[/green]  v{apk_info.get('version_name', '?')}"
                f"  — {len(patches)} compatible patch(es)"
            )
            out_input = self.query_one("#patch-output-input", Input)
            if not out_input.value:
                base = os.path.splitext(os.path.basename(path))[0]
                out_input.value = os.path.join(OUTPUT_DIR, f"{base}-patched.apk")
            if patches:
                self.notify(f"{apk_info['package_name']}: {len(patches)} compatible patch(es) loaded.")
            else:
                self.notify(f"{apk_info['package_name']}: no compatible patches found.", severity="warning")

        self.app.call_from_thread(populate)

    def edit_options(self, row: "PatchRow") -> None:
        patch = row.patch
        options = patch.get("Options") or []
        if not options:
            return
        patch_name = patch.get("Name", "")

        def handle_result(result: Optional[dict]) -> None:
            if result is None:
                self.notify(f"{patch_name}: option changes discarded.")
                return
            row.option_values = result
            if result:
                summary = ", ".join(f"{k}={v}" for k, v in result.items())
                self.notify(f"{patch_name}: options updated — {summary}")
            else:
                self.notify(f"{patch_name}: all options cleared (using defaults).")

        self.app.push_screen(
            OptionEditModal(patch.get("Name", ""), options, row.option_values),
            handle_result,
        )

    def run_patch(self) -> None:
        log = self.query_one("#patch-log", RichLog)
        run_btn = self.query_one("#patch-run-btn", Button)

        if not self.apk_path:
            log.write("[yellow]Load an APK first.[/yellow]")
            self.notify("Load an APK first.", severity="warning")
            return

        view = self.query_one("#patch-list-view", ListView)
        rows = [item for item in view.children if isinstance(item, PatchRow)]
        if not rows:
            log.write("[yellow]No patches loaded — load an APK first.[/yellow]")
            self.notify("No patches loaded — load an APK first.", severity="warning")
            return

        if shutil.which("java") is None:
            msg = "Java was not found on PATH. It's required to run the ReVanced CLI."
            log.write(f"[bold red]✘ {msg}[/bold red]")
            self.notify(msg, severity="error", timeout=10)
            return

        enabled_patches: list[str] = []
        disabled_patches: list[str] = []
        options: dict[str, Any] = {}
        for row in rows:
            name = row.patch.get("Name", "")
            default_enabled = bool(row.patch.get("Enabled"))
            if row.enabled:
                options.update(row.option_values)
                # Only pass it explicitly if it differs from the patch's own default,
                # or if it has option overrides that need it named on the command line.
                if not default_enabled or row.option_values:
                    enabled_patches.append(name)
            else:
                if default_enabled:
                    disabled_patches.append(name)

        output_path = self.query_one("#patch-output-input", Input).value.strip() or None
        exclusive = self.query_one("#patch-flag-exclusive", Checkbox).value
        force = self.query_one("#patch-flag-force", Checkbox).value
        bypass = self.query_one("#patch-flag-bypass", Checkbox).value
        purge = self.query_one("#patch-flag-purge", Checkbox).value

        total_enabled = sum(1 for row in rows if row.enabled)
        total_disabled = len(rows) - total_enabled

        log.clear()
        log.write(
            f"[bold]Patching[/bold] {os.path.basename(self.apk_path)}  "
            f"({total_enabled} enabled / {total_disabled} disabled patch(es) selected)"
        )
        log.write(
            f"[dim]{len(enabled_patches)} enable-override(s), {len(disabled_patches)} "
            f"disable-override(s) sent to the CLI (only ones that differ from the patch's "
            f"own default, or that carry option values).[/dim]"
        )
        self.notify(f"Starting patch job — {total_enabled} patch(es) enabled...")
        run_btn.disabled = True
        run_btn.label = "Running..."

        self.do_patch(
            apk_path=self.apk_path,
            output_path=output_path,
            enabled_patches=enabled_patches,
            disabled_patches=disabled_patches,
            options=options,
            exclusive=exclusive,
            force=force,
            bypass_verification=bypass,
            purge=purge,
        )

    @work(thread=True, exclusive=True)
    def do_patch(
        self,
        apk_path: str,
        output_path: Optional[str],
        enabled_patches: list[str],
        disabled_patches: list[str],
        options: dict[str, Any],
        exclusive: bool,
        force: bool,
        bypass_verification: bool,
        purge: bool,
    ) -> None:
        log = self.query_one("#patch-log", RichLog)
        run_btn = self.query_one("#patch-run-btn", Button)

        def reset_button() -> None:
            run_btn.disabled = False
            run_btn.label = "Run patch"

        patcher = Patcher()
        try:
            gen = patcher.patch_apk(
                apk_path=apk_path,
                output_path=output_path,
                enabled_patches=enabled_patches or None,
                disabled_patches=disabled_patches or None,
                options=options or None,
                exclusive=exclusive,
                force=force,
                bypass_verification=bypass_verification,
                purge=purge,
                stream_output=True,
            )
            for line in gen:
                self.app.call_from_thread(log.write, line)
            self.app.call_from_thread(log.write, "[bold green]✔ Patching finished.[/bold green]")
            self.app.call_from_thread(self.notify, "Patching finished successfully.", severity="information")
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(log.write, f"[bold red]✘ Patch job failed: {exc}[/bold red]")
            self.app.call_from_thread(self.notify, f"Patch job failed: {exc}", severity="error", timeout=10)
        finally:
            self.app.call_from_thread(reset_button)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class Re1sidTui(App):
    TITLE = "re1sid-lib TUI"
    CSS = """
    MainMenu {
        align: center middle;
    }
    #menu-box {
        width: 60%;
        border: round $accent;
        padding: 2 4;
        height: auto;
    }
    #menu-title {
        text-style: bold;
        text-align: center;
        color: $accent;
        padding-bottom: 1;
    }
    #resource-status {
        padding-bottom: 1;
        text-align: center;
    }
    #menu-box Button {
        width: 100%;
        margin-bottom: 1;
    }

    #download-box, #list-box, #patch-box {
        padding: 1 2;
        height: 1fr;
    }
    #download-title, #list-title, #patch-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #dl-log, #patch-log {
        height: 15;
        border: round $accent;
        margin-top: 1;
    }

    #list-search-row {
        height: auto;
    }
    #list-search-row Input {
        width: 1fr;
    }
    #list-body {
        height: 1fr;
    }
    #list-patches-view {
        width: 45%;
        border: round $accent;
    }
    #list-detail-pane {
        width: 55%;
        border: round $accent;
        padding: 0 1;
    }

    #patch-apk-row {
        height: auto;
    }
    #patch-apk-row Input {
        width: 1fr;
    }
    #patch-list-view {
        height: 12;
        border: round $accent;
    }
    .patch-row {
        height: auto;
        padding: 0 1;
    }
    .patch-row-label {
        width: 1fr;
        padding-left: 1;
    }
    .patch-options-btn {
        min-width: 10;
    }
    #patch-run-config {
        height: auto;
    }
    #patch-flags-row {
        height: auto;
    }
    #patch-flags-row Checkbox {
        margin-right: 2;
    }
    """

    def on_mount(self) -> None:
        self.push_screen(MainMenu())


def main() -> None:
    Re1sidTui().run()


if __name__ == "__main__":
    main()