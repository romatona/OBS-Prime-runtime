from __future__ import annotations

import os
import tkinter as tk
from dataclasses import dataclass


@dataclass
class OverlayRect:
    x: int
    y: int
    w: int
    h: int


class OverlayWindow:
    def __init__(self, parent: tk.Misc, click_through: bool = False, opacity: float = 0.92) -> None:
        self.parent = parent
        self.window = tk.Toplevel(parent)
        self.window.title("OBS prime Overlay")
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.attributes("-topmost", True)
        self.window.configure(bg="black")
        self.window.withdraw()
        self.window.resizable(False, False)
        self.window.overrideredirect(True)
        self.set_click_through(click_through)
        self.set_opacity(opacity)

        self.text = tk.Text(self.window, width=72, height=6, bg="black", fg="#00ff66", relief="sunken")
        self.text.pack(fill="both", expand=True, padx=2, pady=2)
        self.text.configure(state="disabled")

    def show(self, payload: str, x: int = 20, y: int = 20, w: int = 620, h: int = 180, topmost: bool = True) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", payload)
        self.text.configure(state="disabled")
        self.window.geometry(f"{max(1, w)}x{max(1, h)}+{max(0, x)}+{max(0, y)}")
        self.window.attributes("-topmost", topmost)
        self.window.deiconify()
        self.window.lift()

    def hide(self) -> None:
        if self.window.winfo_exists():
            self.window.withdraw()

    def is_visible(self) -> bool:
        try:
            return bool(self.window.winfo_exists() and self.window.state() != "withdrawn")
        except tk.TclError:
            return False

    def clear(self) -> None:
        if not self.window.winfo_exists():
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def destroy(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            return

    def set_click_through(self, enabled: bool) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
        except Exception:
            return

        hwnd = self.window.winfo_id()
        gws_exstyle = -20
        ws_ex_layered = 0x00080000
        ws_ex_transparent = 0x00000020

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        current = user32.GetWindowLongW(hwnd, gws_exstyle)
        if enabled:
            user32.SetWindowLongW(hwnd, gws_exstyle, current | ws_ex_layered | ws_ex_transparent)
        else:
            user32.SetWindowLongW(hwnd, gws_exstyle, current & ~ws_ex_transparent)

    def set_opacity(self, opacity: float) -> None:
        alpha = max(0.2, min(1.0, opacity))
        self.window.attributes("-alpha", alpha)

    def get_rect(self) -> OverlayRect:
        geom = self.window.geometry()
        try:
            size, x_offset, y_offset = geom.split("+")
            width, height = size.split("x")
            return OverlayRect(int(x_offset), int(y_offset), int(width), int(height))
        except Exception:
            return OverlayRect(0, 0, 0, 0)


class ObsCaptureOverlayWindow:
    def __init__(self, parent: tk.Misc) -> None:
        self.parent = parent
        self.window = tk.Toplevel(parent)
        self.window.title("OBS prime OBS Overlay Source")
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.configure(bg="black")
        self.window.withdraw()
        self.window.resizable(True, True)

        self.text = tk.Text(
            self.window,
            width=72,
            height=6,
            bg="black",
            fg="#00ff66",
            insertbackground="#00ff66",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 12),
        )
        self.text.pack(fill="both", expand=True, padx=0, pady=0)
        self.text.configure(state="disabled")

    def show(self, payload: str, x: int = 20, y: int = 20, w: int = 620, h: int = 180) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", payload)
        self.text.configure(state="disabled")
        self.window.geometry(f"{max(1, w)}x{max(1, h)}+{max(0, x)}+{max(0, y)}")
        self.window.attributes("-topmost", False)
        self.window.deiconify()
        self.window.lift()

    def hide(self) -> None:
        if self.window.winfo_exists():
            self.window.withdraw()

    def is_visible(self) -> bool:
        try:
            return bool(self.window.winfo_exists() and self.window.state() != "withdrawn")
        except tk.TclError:
            return False

    def clear(self) -> None:
        if not self.window.winfo_exists():
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def destroy(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            return


__all__ = ["OverlayWindow", "ObsCaptureOverlayWindow", "OverlayRect"]
