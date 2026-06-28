from __future__ import annotations

import os
import re
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
        self.click_through_enabled = bool(click_through)
        self.opacity = opacity
        self.window = tk.Toplevel(parent)
        self.window.title("OBS prime Overlay")
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.attributes("-topmost", True)
        self.window.configure(bg="black")
        self.window.withdraw()
        self.window.resizable(False, False)
        self.window.overrideredirect(True)
        self.set_opacity(opacity)

        self.text = tk.Text(self.window, width=72, height=6, bg="black", fg="#00ff66", relief="sunken")
        self.text.pack(fill="both", expand=True, padx=2, pady=2)
        self.text.configure(state="disabled")

    def show(self, payload: str, x: int = 20, y: int = 20, w: int = 620, h: int = 180, topmost: bool = True) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", payload)
        self.text.configure(state="disabled")
        width = max(1, w)
        height = max(1, h)
        left = int(x)
        top = int(y)
        self.window.geometry(f"{width}x{height}{left:+d}{top:+d}")
        self.window.overrideredirect(False)
        self.window.deiconify()
        self.window.update_idletasks()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", topmost)
        self._force_window_pos(left, top, width, height, topmost)
        self.window.lift()
        if not self.click_through_enabled:
            try:
                self.window.focus_force()
            except tk.TclError:
                pass
        self.set_click_through(self.click_through_enabled)
        self.set_opacity(self.opacity)
        self._raise_again(left, top, width, height, topmost)

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
        self.click_through_enabled = bool(enabled)
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
        self._refresh_window_style()

    def set_opacity(self, opacity: float) -> None:
        alpha = max(0.2, min(1.0, opacity))
        self.opacity = alpha
        self.window.attributes("-alpha", alpha)
        self._apply_layered_alpha(alpha)

    def _apply_layered_alpha(self, opacity: float) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
        except Exception:
            return
        hwnd = self.window.winfo_id()
        lwa_alpha = 0x00000002
        alpha = max(51, min(255, int(opacity * 255)))
        try:
            ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, alpha, lwa_alpha)  # type: ignore[attr-defined]
        except Exception:
            return

    def _refresh_window_style(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
        except Exception:
            return
        hwnd = self.window.winfo_id()
        swp_nosize = 0x0001
        swp_nomove = 0x0002
        swp_nozorder = 0x0004
        swp_noactivate = 0x0010
        swp_framechanged = 0x0020
        ctypes.windll.user32.SetWindowPos(  # type: ignore[attr-defined]
            hwnd,
            0,
            0,
            0,
            0,
            0,
            swp_nomove | swp_nosize | swp_nozorder | swp_noactivate | swp_framechanged,
        )

    def _force_window_pos(self, x: int, y: int, w: int, h: int, topmost: bool) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
        except Exception:
            return
        hwnd = self.window.winfo_id()
        hwnd_topmost = -1
        hwnd_notopmost = -2
        swp_showwindow = 0x0040
        swp_noactivate = 0x0010
        ctypes.windll.user32.SetWindowPos(  # type: ignore[attr-defined]
            hwnd,
            hwnd_topmost if topmost else hwnd_notopmost,
            int(x),
            int(y),
            int(w),
            int(h),
            swp_showwindow | swp_noactivate,
        )

    def _raise_again(self, x: int, y: int, w: int, h: int, topmost: bool) -> None:
        try:
            self.window.after(80, lambda: self._force_window_pos(x, y, w, h, topmost))
            self.window.after(160, lambda: self._force_window_pos(x, y, w, h, topmost))
        except tk.TclError:
            return

    def get_rect(self) -> OverlayRect:
        geom = self.window.geometry()
        try:
            match = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geom)
            if not match:
                raise ValueError(geom)
            width, height, x_offset, y_offset = match.groups()
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
