#!/usr/bin/env python3
"""
tui.py - A Textual terminal UI for re1sid-lib
https://pypi.org/project/re1sid-lib/

re1sid-lib wraps the ReVanced CLI to download assets (revanced-cli.jar +
patches.rvp) and patch APKs programmatically. This TUI puts a keyboard-driven
front end on top of it.

Features
--------
- Download the ReVanced CLI jar and patches.rvp bundle
- Browse for an APK (auto-detects its package name via Patcher.get_apk_info)
- List patches filtered to that package, with live filtering by name
- Toggle each patch on/off (overrides the default enabled state)
- Edit per-patch options in a modal (respects Type / Default / Possible values)
- Run the patch job with the CLI's output streamed live into a log panel

Requirements
------------
    pip install re1sid-lib textual

Also needs a Java runtime on PATH (required by re1sid-lib itself).

Run
---
    python tui.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)

from re1sid_lib import Downloader, Patcher
from re1sid_lib.common import CLI_PATH, OUTPUT_DIR, PATCHES_PATH

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Cycle order for the per-patch enable/disable override.
# None  -> use whatever the patch bundle says by default
# True  -> force-enable (-e / --ei)
# False -> force-disable (-d / --di)
STATE_CYCLE = [None, True, False]
STATE_ICON = {None: " ", True: "[green]✔[/]", False: "[red]✘[/]"}


def state_label(overridden: Optional[bool], default_enabled: bool) -> str:
    if overridden is None:
        return "default (on)" if default_enabled else "default (off)"
    return "force ON" if overridden else "force OFF"


class ApkFileTree(DirectoryTree):
    """A DirectoryTree that only shows directories and .apk files."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [
            p
            for p in paths
            if p.is_dir() or p.suffix.lower() == ".apk"
        ]


# --------------------------------------------------------------------------- #
# Modal: APK file picker
# --------------------------------------------------------------------------- #


class ApkPickerScreen(ModalScreen[Optional[str]]):
    """Browse the filesystem and pick an .apk file."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    ApkPickerScreen {
        align: center middle;
    }
    #picker-box {
        width: 80%;
        height: 80%;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    #picker-box Label {
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("Select an APK file  (Esc to cancel)")
            yield ApkFileTree(str(Path.cwd()))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        self.dismiss(str(event.path))


# --------------------------------------------------------------------------- #
# Modal: edit options for a single patch
# --------------------------------------------------------------------------- #


class OptionsModal(ModalScreen[Optional[dict[str, Any]]]):
    """Edit the option values for one patch."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    OptionsModal {
        align: center middle;
    }
    #opt-box {
        width: 90%;
        height: 90%;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    .opt-name {
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    .opt-desc {
        color: $text-muted;
        margin-bottom: 1;
    }
    #opt-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #opt-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, patch: dict[str, Any], current: dict[str, Any]) -> None:
        super().__init__()
        self.patch = patch
        self.current = dict(current)
        self._widget_ids: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="opt-box"):
            yield Label(f"Options - {self.patch.get('Name', '?')}", classes="opt-name")
            with VerticalScroll():
                options = self.patch.get("Options") or []
                if not options:
                    yield Static("This patch has no configurable options.")
                for i, opt in enumerate(options):
                    key = opt.get("Name", f"opt{i}")
                    widget_id = f"opt-input-{i}"
                    self._widget_ids[key] = widget_id

                    desc = opt.get("Description") or ""
                    required = " (required)" if opt.get("Required") else ""
                    possible = opt.get("Possible values") or []
                    hint = f"  values: {', '.join(possible)}" if possible else ""
                    yield Label(f"{key}{required}", classes="opt-name")
                    if desc or hint:
                        yield Static(f"{desc}{hint}", classes="opt-desc")

                    default_val = opt.get("Default")
                    value = self.current.get(key, default_val)

                    if isinstance(default_val, bool):
                        yield Checkbox(
                            key, value=bool(value), id=widget_id
                        )
                    else:
                        yield Input(
                            value="" if value is None else str(value),
                            placeholder=str(default_val) if default_val is not None else "",
                            id=widget_id,
                        )
            with Horizontal(id="opt-buttons"):
                yield Button("Reset to defaults", id="opt-reset")
                yield Button("Cancel", id="opt-cancel")
                yield Button("Save", id="opt-save", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "opt-cancel":
            self.dismiss(None)
        elif event.button.id == "opt-reset":
            self.dismiss({})
        elif event.button.id == "opt-save":
            result: dict[str, Any] = {}
            for opt in self.patch.get("Options") or []:
                key = opt.get("Name")
                widget_id = self._widget_ids.get(key)
                if widget_id is None:
                    continue
                widget = self.query_one(f"#{widget_id}")
                if isinstance(widget, Checkbox):
                    result[key] = widget.value
                elif isinstance(widget, Input):
                    text = widget.value.strip()
                    if text == "":
                        continue
                    result[key] = _coerce(text)
            self.dismiss(result)


def _coerce(text: str) -> Any:
    """Best-effort turn typed text into bool/int/list/str for -O option values."""
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1]
        return [item.strip() for item in inner.split(",") if item.strip()]
    try:
        return int(text)
    except ValueError:
        pass
    return text


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #


class Re1sidTUI(App):
    """Textual front end for re1sid-lib."""

    TITLE = "re1sid-lib TUI"
    SUB_TITLE = "ReVanced patcher & downloader"

    CSS = """
    #status-bar {
        height: auto;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    #main {
        height: 1fr;
    }
    #left-pane {
        width: 2fr;
        border-right: solid $accent;
        padding: 0 1;
    }
    #right-pane {
        width: 1fr;
        padding: 0 1;
    }
    #filter-row, #action-row, #patch-row {
        height: auto;
        margin-bottom: 1;
    }
    #filter-row Input {
        width: 1fr;
    }
    DataTable {
        height: 1fr;
    }
    RichLog {
        height: 1fr;
        border: solid $accent;
    }
    #output-row Input {
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding("b", "browse_apk", "Browse APK"),
        Binding("l", "load_patches", "Load patches"),
        Binding("d", "download_assets", "Download assets"),
        Binding("o", "edit_options", "Edit options"),
        Binding("space,enter", "toggle_patch", "Toggle patch", show=False),
        Binding("p", "run_patch", "Patch APK"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.downloader = Downloader()
        self.patcher = Patcher()

        self.apk_path: Optional[str] = None
        self.package_name: Optional[str] = None
        self.patches: list[dict[str, Any]] = []
        # Both dicts are keyed by patch Index (unique), not Name (which can
        # repeat across an unfiltered patch list).
        self.overrides: dict[int, Optional[bool]] = {}
        self.patch_options: dict[int, dict[str, Any]] = {}

    # -- layout ------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._status_text(), id="status-bar")
        with Horizontal(id="main"):
            with Vertical(id="left-pane"):
                with Horizontal(id="filter-row"):
                    yield Input(placeholder="Filter package name (optional)", id="filter-input")
                    yield Button("Load patches", id="btn-load")
                with Horizontal(id="action-row"):
                    yield Button("Browse APK...", id="btn-browse")
                    yield Button("Download assets", id="btn-download")
                    yield Button("Edit options", id="btn-options")
                table = DataTable(id="patch-table", cursor_type="row", zebra_stripes=True)
                table.add_columns("On", "Idx", "Name", "Options", "Compatible")
                yield table
                with Horizontal(id="output-row"):
                    yield Input(placeholder="Output path (optional)", id="output-input")
                with Horizontal(id="patch-row"):
                    yield Checkbox("exclusive", id="chk-exclusive")
                    yield Checkbox("force", id="chk-force")
                    yield Checkbox("bypass verification", value=True, id="chk-bypass")
                    yield Checkbox("purge", value=True, id="chk-purge")
                yield Button("Patch APK  [p]", id="btn-patch", variant="primary")
            with Vertical(id="right-pane"):
                yield Label("Log")
                yield RichLog(id="log", wrap=True, highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.log_line(f"CLI jar:      {'found' if os.path.exists(CLI_PATH) else 'missing'}  -> {CLI_PATH}")
        self.log_line(f"patches.rvp:  {'found' if os.path.exists(PATCHES_PATH) else 'missing'}  -> {PATCHES_PATH}")
        self.log_line("Press [b:browse] to pick an APK, then [l:load] to list patches.")

    # -- small helpers -------------------------------------------------- #

    def _status_text(self) -> str:
        apk = self.apk_path or "(none selected)"
        pkg = self.package_name or "(unknown)"
        return f"APK: {apk}    Package: {pkg}    Patches loaded: {len(self.patches)}"

    def refresh_status(self) -> None:
        self.query_one("#status-bar", Static).update(self._status_text())

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def _patch_by_index(self, index: int) -> Optional[dict[str, Any]]:
        for p in self.patches:
            if p.get("Index") == index:
                return p
        return None

    def _selected_patch(self) -> Optional[dict[str, Any]]:
        table = self.query_one("#patch-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        if row_key is None or row_key.value is None:
            return None
        try:
            index = int(row_key.value)
        except (TypeError, ValueError):
            return None
        return self._patch_by_index(index)

    def _rebuild_table(self) -> None:
        # Patch names are not guaranteed unique across an unfiltered patch
        # list (multiple bundled patches can share a display name), so the
        # patch Index -- which re1sid-lib guarantees is unique -- is used as
        # the row key and as the identity for overrides/options everywhere.
        table = self.query_one("#patch-table", DataTable)

        # Remember which row was selected so re-toggling/editing right after
        # a rebuild doesn't silently jump the cursor back to row 0.
        previous_row_index: Optional[int] = None
        if table.row_count > 0:
            try:
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                if row_key is not None and row_key.value is not None:
                    previous_row_index = int(row_key.value)
            except Exception:
                previous_row_index = None

        table.clear()
        new_cursor_row = 0
        for i, p in enumerate(self.patches):
            index = p.get("Index")
            name = p.get("Name", "")
            override = self.overrides.get(index)
            icon = STATE_ICON[override] if override is not None else (
                "[green]on[/]" if p.get("Enabled") else "[dim]off[/]"
            )
            n_opts = len(p.get("Options") or [])
            opts_set = len(self.patch_options.get(index, {}))
            opts_text = f"{n_opts}" + (f" ({opts_set} set)" if opts_set else "")
            compat = ", ".join(
                pkg.get("Package name", "") for pkg in (p.get("Compatible packages") or [])
            ) or "any"
            table.add_row(icon, str(index), name, opts_text, compat, key=str(index))
            if previous_row_index is not None and index == previous_row_index:
                new_cursor_row = i

        if table.row_count > 0:
            table.move_cursor(row=new_cursor_row)

    # -- button dispatch ------------------------------------------------- #

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-browse":
            self.action_browse_apk()
        elif bid == "btn-download":
            self.action_download_assets()
        elif bid == "btn-load":
            self.action_load_patches()
        elif bid == "btn-options":
            self.action_edit_options()
        elif bid == "btn-patch":
            self.action_run_patch()

    # -- actions ---------------------------------------------------------- #

    def action_browse_apk(self) -> None:
        def on_result(path: Optional[str]) -> None:
            if not path:
                return
            self.apk_path = path
            self.log_line(f"Selected APK: {path}")
            try:
                info = self.patcher.get_apk_info(path)
                self.package_name = info.get("package_name")
                self.log_line(
                    f"Detected package: {self.package_name}  "
                    f"(version {info.get('version_name')})"
                )
                self.query_one("#filter-input", Input).value = self.package_name or ""
            except Exception as exc:  # noqa: BLE001
                self.log_line(f"[red]Could not read APK info: {exc}[/]")
            self.refresh_status()

        self.push_screen(ApkPickerScreen(), on_result)

    @work(thread=True)
    def action_download_assets(self) -> None:
        self.call_from_thread(self.log_line, "Downloading ReVanced CLI + patches.rvp ...")
        try:
            self.downloader.download_all()
            self.call_from_thread(
                self.log_line, "[green]Download complete.[/]"
            )
            self.call_from_thread(
                self.log_line,
                f"CLI:      {CLI_PATH}\npatches:  {PATCHES_PATH}",
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.log_line, f"[red]Download failed: {exc}[/]")

    @work(thread=True)
    def action_load_patches(self) -> None:
        filter_text = self.query_one("#filter-input", Input).value.strip() or None
        apk_path = self.apk_path

        self.call_from_thread(self.log_line, "Loading patches ...")
        try:
            if apk_path and not filter_text:
                patches = self.patcher.list_patches(apk_path=apk_path)
            else:
                patches = self.patcher.list_patches(package_name=filter_text)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.log_line, f"[red]Failed to load patches: {exc}[/]")
            return

        def apply() -> None:
            self.patches = patches
            self.overrides = {}
            self.patch_options = {}
            self._rebuild_table()
            self.refresh_status()
            self.log_line(f"[green]Loaded {len(patches)} patch(es).[/]")

        self.call_from_thread(apply)

    def action_toggle_patch(self) -> None:
        patch = self._selected_patch()
        if patch is None:
            return
        index = patch.get("Index")
        name = patch.get("Name", "")
        current = self.overrides.get(index)
        idx = STATE_CYCLE.index(current) if current in STATE_CYCLE else 0
        new_state = STATE_CYCLE[(idx + 1) % len(STATE_CYCLE)]
        self.overrides[index] = new_state
        self._rebuild_table()
        self.log_line(f"[{index}] {name}: {state_label(new_state, patch.get('Enabled', False))}")

    def action_edit_options(self) -> None:
        patch = self._selected_patch()
        if patch is None:
            self.log_line("[yellow]Select a patch first.[/]")
            return
        index = patch.get("Index")
        name = patch.get("Name", "")

        def on_result(values: Optional[dict[str, Any]]) -> None:
            if values is None:
                return
            if values == {}:
                self.patch_options.pop(index, None)
                self.log_line(f"[{index}] {name}: options reset to defaults.")
            else:
                self.patch_options[index] = values
                self.log_line(f"[{index}] {name}: options updated -> {values}")
            self._rebuild_table()

        self.push_screen(OptionsModal(patch, self.patch_options.get(index, {})), on_result)

    def action_run_patch(self) -> None:
        if not self.apk_path:
            self.log_line("[red]No APK selected. Press b to browse for one.[/]")
            return
        if not os.path.exists(CLI_PATH) or not os.path.exists(PATCHES_PATH):
            self.log_line("[red]CLI jar or patches.rvp missing. Press d to download assets.[/]")
            return
        self._run_patch_worker()

    @work(thread=True)
    def _run_patch_worker(self) -> None:
        # enabled_patches/disabled_patches accept names or indices; indices
        # are used here since they're guaranteed unique per patch.
        enabled: list[Any] = [index for index, v in self.overrides.items() if v is True]
        disabled: list[Any] = [index for index, v in self.overrides.items() if v is False]

        combined_options: dict[str, Any] = {}
        for index in self.patch_options:
            combined_options.update(self.patch_options[index])

        output_path = self.query_one("#output-input", Input).value.strip() or None
        if output_path is None:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            base = Path(self.apk_path).stem
            output_path = str(Path(OUTPUT_DIR) / f"{base}-patched.apk")

        exclusive = self.query_one("#chk-exclusive", Checkbox).value
        force = self.query_one("#chk-force", Checkbox).value
        bypass = self.query_one("#chk-bypass", Checkbox).value
        purge = self.query_one("#chk-purge", Checkbox).value

        self.call_from_thread(
            self.log_line,
            f"[bold]Patching[/] {self.apk_path} -> {output_path}\n"
            f"enabled={enabled or '-'} disabled={disabled or '-'} "
            f"options={combined_options or '-'}",
        )

        try:
            stream = self.patcher.patch_apk(
                apk_path=self.apk_path,
                output_path=output_path,
                enabled_patches=enabled or None,
                disabled_patches=disabled or None,
                options=combined_options or None,
                exclusive=exclusive,
                force=force,
                bypass_verification=bypass,
                purge=purge,
                stream_output=True,
            )
            for line in stream:
                self.call_from_thread(self.log_line, line)
            self.call_from_thread(
                self.log_line, f"[green]Done. Patched APK written to {output_path}[/]"
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.log_line, f"[red]Patch failed: {exc}[/]")


def main() -> None:
    Re1sidTUI().run()


if __name__ == "__main__":
    main()