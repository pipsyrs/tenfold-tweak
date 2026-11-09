"""GTK 4 dashboard over core.status / core.run; uses libadwaita when present."""

import os
import shutil
import subprocess
import sys
import threading

from . import core, detect

TITLE = "Tenfold Tweak"
APP_ID = "io.github.tenfold.TenfoldTweak"
THUMB_W, THUMB_H = 640, 360  # decode size; display crops to fill
TARGET_INFO = {
    "desktop": ("Desktop", "user-desktop-symbolic"),
    "lock": ("Lock screen", "system-lock-screen-symbolic"),
    "login": ("Login screen", "system-users-symbolic"),
}
DE_NAMES = {"gnome": "GNOME", "xfce": "XFCE"}
DM_NAMES = {"gdm3": "GDM", "lightdm": "LightDM"}

# Bound by _load_toolkit(); the pure helpers below need none of them.
Adw = Gdk = GdkPixbuf = Gio = GLib = GObject = Gtk = None


# ---------- pure helpers (unit-tested) ----------

def target_label(target: str) -> str:
    return TARGET_INFO.get(target, (target, ""))[0]


def env_subtitle(de: str, dm: str) -> str:
    return f"{DE_NAMES.get(de, de)} · {DM_NAMES.get(dm, dm)}"


def needs_setup(st) -> bool:
    """Target becomes usable once the system component is installed."""
    return bool(getattr(st, "setup_required", False))  # guard until the backend field lands


def usable(st) -> bool:
    return st.available or needs_setup(st)


def card_actions(st, pending: bool, busy: bool) -> dict:
    """Which card buttons are enabled."""
    live = usable(st) and not busy
    return {"choose": live, "apply": live and pending, "setup": needs_setup(st) and not busy,
            "restore": st.available and not busy and st.restorable}


def status_badge(st) -> tuple:
    """(text, css class) for the pill beside a card title."""
    if needs_setup(st):
        return "Needs setup", "accent"
    if not st.available:
        return "Unavailable", "dim"
    return ("Customized", "accent") if st.restorable else ("Default", "dim")


def thumb_hint(st) -> str:
    """Empty-thumbnail text; "" when there is an image to show."""
    if not usable(st):
        return "Not available on this system"
    if st.current is None:
        return "No preview available"
    return ""


def apply_all_targets(statuses) -> list:
    return [s.target for s in statuses if usable(s)]


def setup_targets(statuses, targets) -> list:
    """Targets of this run that need the system component first."""
    return [s.target for s in statuses if s.target in targets and needs_setup(s)]


def setup_offer_text(targets) -> tuple:
    """(heading, body) offering setup before an apply."""
    names = ", ".join(target_label(t) for t in targets)
    return ("Set up system support first?",
            f"{names}: changing this needs a small system component, installed once with "
            "administrator rights. Install it now and then continue?")


def setup_feedback(action: str, res) -> tuple:
    """(kind, text) for an install/uninstall SetupResult; a cancelled prompt is neutral."""
    message = (getattr(res, "message", "") or "").strip()
    if res.ok:
        done = "installed" if action == "install" else "removed"
        return "success", message or f"System component {done}."
    if "cancel" in message.lower():
        return "info", "Nothing was changed: the administrator prompt was cancelled."
    return ("error" if action == "install" else "warning"), message or f"Could not {action} the system component."


def discard_text(res) -> tuple:
    """(heading, body) when setup cannot keep the old login image."""
    message = (getattr(res, "message", "") or "").strip()
    body = (message + "\n\n" if message else "") + (
        "Continuing resets the login screen to its stock background. "
        "You can apply your image again afterwards.")
    return "Reset the login background?", body


def system_summary(state) -> tuple:
    """(pill text, css class, description) for the system component."""
    if state.installed:
        return ("Installed", "accent",
                "Login screen changes and restores work from this app.")
    return ("Not installed", "dim",
            "Needed only for the login screen. Installs a small root-owned helper with "
            "administrator rights; desktop and lock screen work without it.")


def card_result(action: str, r) -> tuple:
    """(text, kind) for the inline result line on one card."""
    if r.status == core.APPLIED:
        text = "Applied" if action == "apply" else "Restored"
        return (f"{text} — {r.reason}" if r.reason else text), "success"
    word = "Failed" if r.status == core.FAILED else "Skipped"
    kind = "error" if r.status == core.FAILED else "warning"
    return (f"{word} — {r.reason}" if r.reason else word), kind


def summarize(action: str, results, notes=()) -> tuple:
    """(kind, text) for the page feedback banner; kind is success, warning or error."""
    shown = [r for r in results if r.reason != "not selected"]
    applied = [target_label(r.target) for r in shown if r.status == core.APPLIED]
    problems = [f"{target_label(r.target)}: {r.status}" + (f" ({r.reason})" if r.reason else "")
                for r in shown if r.status != core.APPLIED]
    parts = []
    if applied:
        parts.append(f"{'Applied to' if action == 'apply' else 'Restored'} {', '.join(applied)}.")
    parts += problems
    parts += list(notes)
    if any(r.status == core.FAILED for r in shown):
        kind = "error"
    elif problems or not applied:
        kind = "warning"
    else:
        kind = "success"
    return kind, "\n".join(parts) or "Nothing was changed."


def confirm_text(message: str) -> tuple:
    """(heading, body) for a consent dialog built from a core confirm() message."""
    if message == core.GDM_WARNING:
        heading = "Change the login screen?"
    elif "lock target" in message:
        heading = "Change the desktop wallpaper too?"
    else:
        heading = "Continue?"
    return heading, message.removesuffix("Continue?").rstrip()


def working_text(action: str, targets) -> str:
    verb = "Applying to" if action == "apply" else "Restoring"
    text = f"{verb} {', '.join(target_label(t) for t in targets)}…"
    if "login" in targets:
        text += " Approve the administrator prompt when it appears."
    return text


def cover_size(width: int, height: int, box_w: int = THUMB_W, box_h: int = THUMB_H) -> tuple:
    """Smallest decode size that still covers box_w x box_h; never upscales."""
    if width <= 0 or height <= 0:
        return box_w, box_h
    scale = min(1.0, max(box_w / width, box_h / height))
    return max(1, round(width * scale)), max(1, round(height * scale))


# ---------- toolkit ----------

def _load_toolkit(use_adw: bool = True) -> str | None:
    """Import GTK 4 (+ libadwaita when usable); return an error message or None."""
    global Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: F811
    except (ImportError, ValueError) as e:
        return (f"GTK 4 for Python is missing ({e}). "
                "Install it with: sudo apt install python3-gi gir1.2-gtk-4.0")
    Adw = None
    if use_adw:
        try:
            gi.require_version("Adw", "1")
            from gi.repository import Adw as _adw
            # OverlaySplitView/Breakpoint/AlertDialog need libadwaita >= 1.5
            if all(hasattr(_adw, n) for n in ("OverlaySplitView", "Breakpoint", "AlertDialog")):
                Adw = _adw
        except (ImportError, ValueError):
            pass
    return None


CSS = """
.tt-hero { padding: 20px; border: 1px solid alpha(@accent_bg_color, 0.45); }
.tt-card { padding: 12px; }
.tt-thumb { border-radius: 10px; background-color: alpha(currentColor, 0.06); }
.tt-empty { color: alpha(currentColor, 0.6); }
.tt-badge { padding: 2px 10px; border-radius: 999px; font-size: 0.8em; font-weight: 700;
            background-color: alpha(currentColor, 0.10); }
.tt-badge.accent { color: @accent_color; background-color: alpha(@accent_bg_color, 0.16); }
.tt-preview { margin: 8px; padding: 2px 4px 2px 10px; border-radius: 999px;
              background-color: rgba(0, 0, 0, 0.68); color: white; font-size: 0.85em; font-weight: 700; }
.tt-preview button { min-height: 20px; min-width: 20px; padding: 0; color: white; }
.tt-result.success { color: @success_color; }
.tt-result.warning { color: @warning_color; }
.tt-result.error { color: @error_color; }
.tt-feedback { padding: 10px 14px; border-radius: 10px; background-color: alpha(currentColor, 0.06); }
.tt-feedback.success { background-color: alpha(@success_color, 0.16); }
.tt-feedback.warning { background-color: alpha(@warning_color, 0.20); }
.tt-feedback.error { background-color: alpha(@error_color, 0.16); }
"""
# Plain GTK has no .card style.
CSS_NO_ADW = """
.tt-card, .tt-hero { border-radius: 12px; background-color: alpha(currentColor, 0.04);
                     border: 1px solid alpha(currentColor, 0.12); }
.tt-hero { border-color: alpha(@accent_bg_color, 0.45); }
"""


def _label(text="", *classes, **props) -> "Gtk.Label":
    props.setdefault("xalign", 0)
    lbl = Gtk.Label(label=text, **props)
    for c in classes:
        lbl.add_css_class(c)
    return lbl


def _texture(pb) -> "Gdk.Texture":
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pb.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pb.get_width(), pb.get_height(), fmt, pb.read_pixel_bytes(), pb.get_rowstride())


def _decode_thumb(path: str):
    """Scaled GdkPixbuf for path, or None; runs in a worker thread."""
    try:
        info = GdkPixbuf.Pixbuf.get_file_info(path)
        if info is None or info[0] is None:
            return None
        w, h = cover_size(info[1], info[2])
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w, h, False)
    except GLib.Error:
        return None


class FeedbackBar:
    """Inline message bar: info (neutral), success, warning, error; optional spinner."""

    def __init__(self):
        self.widget = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.box = Gtk.Box(spacing=10)
        self.box.add_css_class("tt-feedback")
        self.spinner = Gtk.Spinner(valign=Gtk.Align.START, visible=False)
        self.label = _label("", wrap=True, selectable=True, hexpand=True)
        self.close = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.START, tooltip_text="Dismiss")
        self.close.add_css_class("flat")
        self.close.add_css_class("circular")
        self.close.update_property([Gtk.AccessibleProperty.LABEL], ["Dismiss message"])
        self.close.connect("clicked", lambda *_: self.widget.set_reveal_child(False))
        for w in (self.spinner, self.label, self.close):
            self.box.append(w)
        self.widget.set_child(self.box)

    def show(self, kind: str, text: str, working=False):
        for k in ("success", "warning", "error"):
            self.box.remove_css_class(k)
        if kind in ("success", "warning", "error"):
            self.box.add_css_class(kind)
        self.spinner.set_visible(working)
        self.spinner.set_spinning(working)
        self.close.set_visible(not working)
        self.label.set_label(text)
        self.widget.set_reveal_child(True)


class ThumbLoader:
    """Decodes thumbnails off the UI thread; caches by (path, mtime)."""

    def __init__(self):
        self._cache = {}

    def load(self, path: str, done):
        try:
            key = (path, os.stat(path).st_mtime_ns)
        except OSError:
            done(None)
            return
        if key in self._cache:
            done(self._cache[key])
            return

        def finish(pb):
            try:
                tex = _texture(pb) if pb is not None else None
            except Exception:
                tex = None
            self._cache[key] = tex
            done(tex)
            return False

        def work():
            try:
                pb = _decode_thumb(path)
            except Exception:
                pb = None
            GLib.idle_add(finish, pb)

        threading.Thread(target=work, daemon=True).start()


class Thumb:
    """Fixed-height image area: picture (cropped to fill), loading, or empty state."""

    def __init__(self, loader: ThumbLoader, height: int, icon: str):
        self.loader = loader
        self.path = None
        self.widget = Gtk.Overlay(hexpand=True, vexpand=False, overflow=Gtk.Overflow.HIDDEN)
        self.widget.add_css_class("tt-thumb")

        base = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, height_request=height,
                       valign=Gtk.Align.FILL)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, vexpand=True,
                        valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER, margin_start=12, margin_end=12)
        self.icon = Gtk.Image(icon_name=icon, pixel_size=32)
        self.icon.add_css_class("tt-empty")
        self.spinner = Gtk.Spinner(visible=False)
        self.hint = _label("", "caption", "tt-empty", xalign=0.5, wrap=True, max_width_chars=24,
                           justify=Gtk.Justification.CENTER)
        for w in (self.icon, self.spinner, self.hint):
            inner.append(w)
        base.append(inner)
        self.widget.set_child(base)

        # Overlay children are not measured, so a large image never widens the card.
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, visible=False)
        self.widget.add_overlay(self.picture)

        self.badge = Gtk.Box(spacing=4, halign=Gtk.Align.START, valign=Gtk.Align.START, visible=False)
        self.badge.add_css_class("tt-preview")
        self.badge.append(_label("Preview"))
        self.clear_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Discard this image")
        self.clear_btn.add_css_class("flat")
        self.clear_btn.add_css_class("circular")
        self.clear_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Discard chosen image"])
        self.badge.append(self.clear_btn)
        self.widget.add_overlay(self.badge)

    def _empty(self, text: str, loading=False):
        self.picture.set_visible(False)
        self.icon.set_visible(not loading)
        self.spinner.set_visible(loading)
        self.spinner.set_spinning(loading)
        self.hint.set_label(text)

    def show(self, path: str | None, empty_text: str, preview=False, alt=""):
        self.badge.set_visible(preview and path is not None)
        if path is None:
            self.path = None
            self._empty(empty_text)
            return
        if path == self.path:
            return  # already shown or loading
        self.path = path
        self._empty("Loading preview…", loading=True)

        def done(tex):
            if self.path != path:
                return  # a newer image replaced this one
            if tex is None:
                self._empty("Image file could not be read")
                return
            self.spinner.set_spinning(False)
            self.picture.set_paintable(tex)
            self.picture.set_alternative_text(alt or os.path.basename(path))
            self.picture.set_visible(True)

        self.loader.load(path, done)


class TargetCard:
    """One target (desktop, lock, login): thumbnail, state and its own actions."""

    def __init__(self, page, target: str):
        self.page, self.target = page, target
        self.st = None
        self.pending = None
        label, icon = TARGET_INFO[target]

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        self.widget.add_css_class("card")
        self.widget.add_css_class("tt-card")

        self.thumb = Thumb(page.loader, 150, icon)
        self.thumb.clear_btn.connect("clicked", lambda *_: self.set_pending(None))
        self.widget.append(self.thumb.widget)

        head = Gtk.Box(spacing=8)
        head.append(_label(label, "heading", hexpand=True, ellipsize=3))
        self.badge = _label("", "tt-badge", valign=Gtk.Align.CENTER)
        head.append(self.badge)
        self.widget.append(head)

        # max_width_chars caps natural width so the FlowBox can fit 3 cards per row.
        wrap = {"wrap": True, "selectable": True, "max_width_chars": 28}
        self.method = _label("", "caption", "dim-label", **wrap)
        self.reason = _label("", "caption", "tt-result", "warning", visible=False, **wrap)
        self.result = _label("", "caption", "tt-result", visible=False, **wrap)
        for w in (self.method, self.reason, self.result):
            self.widget.append(w)

        row = Gtk.Box(spacing=6, valign=Gtk.Align.END, vexpand=True)
        self.choose_btn = Gtk.Button(label="_Choose…", use_underline=True,
                                     tooltip_text=f"Pick an image for the {label.lower()}")
        self.choose_btn.connect("clicked", lambda *_: page.choose_image(self.set_pending))
        self.apply_btn = Gtk.Button(label="_Apply", use_underline=True)
        self.apply_btn.connect("clicked", lambda *_: page.run("apply", self.pending, [target]))
        self.restore_btn = Gtk.Button(label="_Restore", use_underline=True,
                                      tooltip_text="Put back the image used before this tool changed it")
        self.restore_btn.add_css_class("flat")
        self.restore_btn.connect("clicked", lambda *_: page.run("restore", None, [target]))
        self.setup_btn = Gtk.Button(label="_Set Up", use_underline=True, visible=False,
                                    tooltip_text="Install the system component (administrator rights)")
        self.setup_btn.connect("clicked", lambda *_: page.shell.run_setup(page))
        if target == "login":
            self.apply_btn.set_tooltip_text("Needs administrator rights")
        self.spinner = Gtk.Spinner(visible=False, halign=Gtk.Align.END, hexpand=True)
        for w in (self.choose_btn, self.apply_btn, self.restore_btn, self.setup_btn, self.spinner):
            row.append(w)
        self.widget.append(row)

    def set_pending(self, path: str | None):
        self.pending = path
        self.page.sync()

    def set_working(self, on: bool):
        self.spinner.set_visible(on)
        self.spinner.set_spinning(on)

    def set_result(self, text: str | None, kind: str = ""):
        for k in ("success", "warning", "error"):
            self.result.remove_css_class(k)
        self.result.set_visible(bool(text))
        if text:
            self.result.set_label(text)
            self.result.add_css_class(kind)

    def update(self, busy: bool):
        st = self.st
        if st is None:
            return
        text, cls = status_badge(st)
        self.badge.set_label(text)
        self.badge.remove_css_class("accent")
        if cls == "accent":
            self.badge.add_css_class("accent")
        self.method.set_label(st.method or "")
        self.method.set_visible(bool(st.method))
        self.reason.set_label(st.reason)
        self.reason.set_visible(not st.available and bool(st.reason))

        acts = card_actions(st, self.pending is not None, busy)
        self.choose_btn.set_sensitive(acts["choose"])
        self.apply_btn.set_sensitive(acts["apply"])
        self.restore_btn.set_sensitive(acts["restore"])
        setup = needs_setup(st)
        self.restore_btn.set_visible(not setup)  # nothing to restore before setup
        self.setup_btn.set_visible(setup)
        self.setup_btn.set_sensitive(acts["setup"])
        if setup and not self.pending:
            self.setup_btn.add_css_class("suggested-action")
        else:
            self.setup_btn.remove_css_class("suggested-action")
        if self.pending:
            self.apply_btn.add_css_class("suggested-action")
        else:
            self.apply_btn.remove_css_class("suggested-action")
        self.thumb.clear_btn.set_sensitive(not busy)

        shown = self.pending or (st.current if usable(st) else None)
        label = target_label(self.target)
        self.thumb.show(shown, thumb_hint(st), preview=self.pending is not None,
                        alt=f"{label}: {'chosen image' if self.pending else 'current image'}")


class BackgroundsPage:
    """Section: wallpaper for desktop, lock screen and login screen."""

    name, title, icon = "backgrounds", "Backgrounds", "preferences-desktop-wallpaper-symbolic"

    def __init__(self, shell):
        self.shell = shell
        self.loader = ThumbLoader()
        self.env = None
        self.statuses = []
        self.loaded = False
        self.all_image = None
        self.last_folder = None

        self.widget = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.widget.add_named(self._build_loading(), "loading")
        self.widget.add_named(self._build_error(), "error")
        self.widget.add_named(self._build_ready(), "ready")
        self.widget.set_visible_child_name("loading")

    # ----- build -----

    def _build_loading(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        box.append(spinner)
        box.append(_label("Reading current settings…", "dim-label", xalign=0.5))
        return box

    def _build_error(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        box.append(Gtk.Image(icon_name="dialog-error-symbolic", pixel_size=48))
        box.append(_label("Could not read the current settings", "title-3", xalign=0.5, wrap=True))
        self.error_label = _label("", "dim-label", xalign=0.5, wrap=True, selectable=True,
                                  justify=Gtk.Justification.CENTER)
        box.append(self.error_label)
        retry = Gtk.Button(label="_Try Again", use_underline=True, halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_: self.refresh())
        box.append(retry)
        return box

    def _build_ready(self):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=24,
                      margin_start=16, margin_end=16)

        top = Gtk.Box(spacing=8)
        self.env_label = _label("", "dim-label", hexpand=True, valign=Gtk.Align.CENTER)
        top.append(self.env_label)
        self.refresh_spinner = Gtk.Spinner(visible=False)
        top.append(self.refresh_spinner)
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload current state")
        self.refresh_btn.add_css_class("flat")
        self.refresh_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Reload current state"])
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        top.append(self.refresh_btn)
        col.append(top)

        self.bar = FeedbackBar()
        col.append(self.bar.widget)
        col.append(self._build_hero())

        col.append(_label("Screens", "heading"))
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=1, max_children_per_line=3,
                                column_spacing=14, row_spacing=14)
        self.cards = {}
        for t in core.TARGETS:
            card = TargetCard(self, t)
            self.cards[t] = card
            self.flow.append(card.widget)
            card.widget.get_parent().set_focusable(False)  # focus the buttons, not the cell
        col.append(self.flow)

        if Adw:
            content = Adw.Clamp(maximum_size=1200, tightening_threshold=1200, child=col)
        else:
            content = col
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=content)

    def _build_hero(self):
        # Focal point: one image applied to every available screen.
        self.hero = Gtk.Box(spacing=20)
        self.hero.add_css_class("card")
        self.hero.add_css_class("tt-hero")
        self.hero_thumb = Thumb(self.loader, 170, "image-x-generic-symbolic")
        self.hero_thumb.widget.set_size_request(300, -1)
        self.hero_thumb.widget.set_hexpand(False)
        self.hero_thumb.clear_btn.connect("clicked", lambda *_: self._set_all_image(None))
        self.hero.append(self.hero_thumb.widget)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(_label("One image, every screen", "title-2", wrap=True))
        self.hero_desc = _label("", "dim-label", wrap=True)
        text.append(self.hero_desc)
        self.hero_file = _label("", "caption", "monospace", ellipsize=2, visible=False)
        text.append(self.hero_file)

        row = self.hero_row = Gtk.Box(spacing=8, margin_top=6)
        self.hero_choose = Gtk.Button(label="C_hoose Image…", use_underline=True)
        self.hero_choose.add_css_class("pill")
        self.hero_choose.connect("clicked", lambda *_: self.choose_image(self._set_all_image))
        self.hero_apply = Gtk.Button(label="Apply to All", use_underline=True)
        self.hero_apply.add_css_class("pill")
        self.hero_apply.add_css_class("suggested-action")
        self.hero_apply.connect("clicked", lambda *_: self.run(
            "apply", self.all_image, apply_all_targets(self.statuses), from_hero=True))
        row.append(self.hero_choose)
        row.append(self.hero_apply)
        text.append(row)
        self.hero.append(text)
        return self.hero

    def breakpoint_setters(self) -> list:
        """(object, property, GObject.Value) applied on narrow windows."""
        vertical = GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL)
        expand = GObject.Value(bool, True)
        no_width = GObject.Value(int, -1)
        return [(self.hero, "orientation", vertical), (self.hero_row, "orientation", vertical),
                (self.hero_thumb.widget, "hexpand", expand), (self.hero_thumb.widget, "width-request", no_width)]

    # ----- state -----

    def _set_all_image(self, path):
        self.all_image = path
        self.sync()

    def sync(self):
        for t, card in self.cards.items():
            card.update(self.shell.busy)
        targets = apply_all_targets(self.statuses)
        if targets:
            self.hero_desc.set_label("Applies to " + ", ".join(target_label(t) for t in targets) + ".")
        else:
            self.hero_desc.set_label("Nothing can be changed in this environment.")
        self.hero_apply.set_label(f"_Apply to {len(targets)} Screen{'s' if len(targets) != 1 else ''}"
                                  if targets else "_Apply to All")
        self.hero_apply.set_sensitive(bool(targets and self.all_image) and not self.shell.busy)
        self.hero_choose.set_sensitive(bool(targets) and not self.shell.busy)
        self.hero_thumb.clear_btn.set_sensitive(not self.shell.busy)
        self.hero_file.set_visible(self.all_image is not None)
        self.hero_file.set_label(os.path.basename(self.all_image or ""))
        self.hero_file.set_tooltip_text(self.all_image)
        self.hero_thumb.show(self.all_image, "No image chosen yet", preview=True, alt="Chosen image")
        self.refresh_btn.set_sensitive(not self.shell.busy)

    def feedback(self, kind: str, text: str, working=False):
        self.bar.show(kind, text, working)

    def refresh(self, after=None):
        """Re-read detect + status off the UI thread; after() runs once it succeeds."""
        self._after = after
        if not self.loaded:
            self.widget.set_visible_child_name("loading")
        self.refresh_btn.set_sensitive(False)
        self.refresh_spinner.set_visible(True)
        self.refresh_spinner.set_spinning(True)

        def work():
            try:
                env = detect.detect()
                out = (env, core.status(env), None)
            except Exception as e:
                out = (None, None, str(e) or type(e).__name__)
            GLib.idle_add(self._on_status, *out)

        threading.Thread(target=work, daemon=True).start()

    def _on_status(self, env, statuses, error):
        self.refresh_spinner.set_spinning(False)
        self.refresh_spinner.set_visible(False)
        self.refresh_btn.set_sensitive(not self.shell.busy)
        if error:
            self._after = None
            if self.loaded:
                self.feedback("error", f"Could not reload the current state: {error}")
            else:
                self.error_label.set_label(error)
                self.widget.set_visible_child_name("error")
            return False
        self.env, self.statuses = env, statuses
        self.env_label.set_label(env_subtitle(env.de, env.dm))
        for st in statuses:
            self.cards[st.target].st = st
        self.loaded = True
        self.widget.set_visible_child_name("ready")
        self.sync()
        after, self._after = getattr(self, "_after", None), None
        if after:
            after()
        return False

    # ----- actions -----

    def choose_image(self, callback):
        dialog = Gtk.FileDialog(title="Choose an Image", modal=True)
        flt = Gtk.FileFilter(name="Images (PNG, JPEG)")
        for mime in ("image/png", "image/jpeg"):
            flt.add_mime_type(mime)
        for ext in ("png", "jpg", "jpeg"):
            flt.add_suffix(ext)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)
        dialog.set_default_filter(flt)
        folder = self.last_folder or os.path.expanduser("~/Pictures")
        if os.path.isdir(folder):
            dialog.set_initial_folder(Gio.File.new_for_path(folder))

        def done(dlg, res):
            try:
                gfile = dlg.open_finish(res)
            except GLib.Error:
                return  # cancelled
            path = gfile.get_path() if gfile else None
            if not path:
                self.feedback("error", "Only local image files can be used.")
                return
            self.last_folder = os.path.dirname(path)
            callback(path)

        dialog.open(self.shell.window, None, done)

    def run(self, action: str, image: str | None, targets: list, from_hero=False):
        if self.shell.busy or not targets or (action == "apply" and not image):
            return
        pending_setup = setup_targets(self.statuses, targets) if action == "apply" else []
        if pending_setup:
            def answered(ok):
                if ok:  # install, re-read status, then run the apply again
                    self.shell.run_setup(self, then=lambda: self.run(action, image, targets, from_hero))
                else:
                    rest = [t for t in targets if t not in pending_setup]
                    if rest:
                        self.run(action, image, rest, from_hero)
                    else:
                        self.feedback("info", "Nothing was changed.")
            heading, body = setup_offer_text(pending_setup)
            self.shell.ask(heading, body, "Set _Up & Continue", False, answered)
            return
        self.shell.set_busy(True)
        for t in targets:
            self.cards[t].set_working(True)
            self.cards[t].set_result(None)
        self.feedback("info", working_text(action, targets), working=True)
        self.sync()
        env = self.env

        def work():
            results, error = None, None
            try:
                results = core.run(action, image, targets, self.shell.confirm_blocking, env)
            except core.ImageError as e:
                error = f"This image cannot be used: {e}. Nothing was changed."
            except Exception as e:
                error = f"Unexpected error: {e or type(e).__name__}"
            GLib.idle_add(self._run_done, action, targets, results, error, from_hero)

        threading.Thread(target=work, daemon=True).start()

    def _run_done(self, action, targets, results, error, from_hero):
        self.shell.set_busy(False)
        for t in targets:
            self.cards[t].set_working(False)
        if error:
            self.feedback("error", error)
            self.sync()
            return False
        for r in results:
            if r.reason == "not selected":
                continue
            self.cards[r.target].set_result(*card_result(action, r))
            if action == "apply" and r.status == core.APPLIED:
                self.cards[r.target].pending = None
        if from_hero and all(r.status == core.APPLIED for r in results if r.target in targets):
            self.all_image = None
        self.feedback(*summarize(action, results, core.notes(results)))
        self.sync()
        self.refresh()
        return False


class SystemPage:
    """Section: the root-owned system component the login screen needs."""

    name, title, icon = "system", "System", "emblem-system-symbolic"

    def __init__(self, shell):
        self.shell = shell
        self.state = None
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=24,
                      margin_start=16, margin_end=16)
        self.bar = FeedbackBar()
        col.append(self.bar.widget)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("card")
        card.add_css_class("tt-card")
        head = Gtk.Box(spacing=10)
        head.append(Gtk.Image(icon_name=self.icon, pixel_size=24))
        head.append(_label("System component", "heading", hexpand=True))
        self.spinner = Gtk.Spinner(spinning=True)
        head.append(self.spinner)
        self.badge = _label("", "tt-badge", valign=Gtk.Align.CENTER, visible=False)
        head.append(self.badge)
        card.append(head)
        self.desc = _label("Checking…", "dim-label", wrap=True)
        self.reason = _label("", "caption", "tt-result", "warning", wrap=True, selectable=True, visible=False)
        card.append(self.desc)
        card.append(self.reason)

        row = Gtk.Box(spacing=8, margin_top=4)
        self.install_btn = Gtk.Button(label="_Install", use_underline=True,
                                      tooltip_text="Install with administrator rights")
        self.install_btn.connect("clicked", lambda *_: shell.run_setup(self))
        self.remove_btn = Gtk.Button(label="_Remove", use_underline=True)
        self.remove_btn.add_css_class("destructive-action")
        self.remove_btn.connect("clicked", lambda *_: self._confirm_remove())
        row.append(self.install_btn)
        row.append(self.remove_btn)
        card.append(row)
        col.append(card)

        content = Adw.Clamp(maximum_size=760, child=col) if Adw else col
        self.widget = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=content)

    def feedback(self, kind: str, text: str, working=False):
        self.bar.show(kind, text, working)

    def sync(self):
        st, busy = self.state, self.shell.busy
        self.install_btn.set_sensitive(bool(st and st.can_install) and not busy)
        self.remove_btn.set_sensitive(bool(st and st.can_uninstall) and not busy)
        if st and st.can_install and not st.installed:
            self.install_btn.add_css_class("suggested-action")
        else:
            self.install_btn.remove_css_class("suggested-action")

    def refresh(self, after=None):
        self.spinner.set_visible(True)

        def work():
            try:
                out = (core.setup_state(), None)
            except Exception as e:
                out = (None, str(e) or type(e).__name__)
            GLib.idle_add(self._on_state, *out, after)

        threading.Thread(target=work, daemon=True).start()

    def _on_state(self, state, error, after):
        self.spinner.set_visible(False)
        if error:
            self.desc.set_label("Could not read the system component state.")
            self.feedback("error", error)
        else:
            self.state = state
            pill, cls, text = system_summary(state)
            self.badge.set_label(pill)
            self.badge.set_visible(True)
            self.badge.remove_css_class("accent")
            if cls == "accent":
                self.badge.add_css_class("accent")
            self.desc.set_label(text)
            self.reason.set_label(state.reason or "")
            self.reason.set_visible(bool(state.reason))
        self.sync()
        if after and not error:
            after()
        return False

    def _confirm_remove(self):
        body = ("The login screen can no longer be changed or restored from this app until it is "
                "installed again. Restore the login background first if you changed it.")
        self.shell.ask("Remove the system component?", body, "_Remove", True,
                       lambda ok: ok and self._remove())

    def _remove(self):
        self.shell.set_busy(True)
        self.feedback("info", "Removing the system component… Approve the administrator prompt.", working=True)

        def work():
            try:
                res, error = core.uninstall_system(), None
            except Exception as e:
                res, error = None, f"Unexpected error: {e or type(e).__name__}"
            GLib.idle_add(self._remove_done, res, error)

        threading.Thread(target=work, daemon=True).start()

    def _remove_done(self, res, error):
        self.shell.set_busy(False)
        self.feedback(*(("error", error) if error else setup_feedback("uninstall", res)))
        self.shell.refresh_all()
        return False


SECTIONS = (BackgroundsPage, SystemPage)  # add a page class here to add a sidebar section


class Dashboard:
    """Main window: section sidebar + stack of pages."""

    def __init__(self, app):
        self.app = app
        self.busy = False  # one change at a time, across all sections
        self.pages = [cls(self) for cls in SECTIONS]
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True)
        for page in self.pages:
            self.stack.add_titled(page.widget, page.name, page.title).set_icon_name(page.icon)
        sidebar = Gtk.StackSidebar(stack=self.stack, width_request=200, vexpand=True)
        self.window = self._build_adw(sidebar) if Adw else self._build_gtk(sidebar)
        self.stack.connect("notify::visible-child-name", lambda *_: self._sync_title())
        self._sync_title()
        self.window.connect("close-request", self._on_close)
        for page in self.pages:
            page.refresh()

    def _build_adw(self, sidebar):
        win = Adw.ApplicationWindow(application=self.app, title=TITLE, default_width=1280, default_height=800)
        win.set_size_request(360, 480)
        side = Adw.ToolbarView(content=sidebar)
        side.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(title=TITLE)))

        self.title = Adw.WindowTitle(title=TITLE)
        header = Adw.HeaderBar(title_widget=self.title)
        toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Sections")
        header.pack_start(toggle)
        content = Adw.ToolbarView(content=self.stack)
        content.add_top_bar(header)

        split = Adw.OverlaySplitView(sidebar=side, content=content)
        flags = GObject.BindingFlags.SYNC_CREATE
        split.bind_property("show-sidebar", toggle, "active", flags | GObject.BindingFlags.BIDIRECTIONAL)
        split.bind_property("collapsed", toggle, "visible", flags)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(split, "collapsed", GObject.Value(bool, True))
        for page in self.pages:
            for obj, prop, value in getattr(page, "breakpoint_setters", lambda: [])():
                bp.add_setter(obj, prop, value)
        win.add_breakpoint(bp)
        win.set_content(split)
        return win

    def _build_gtk(self, sidebar):
        win = Gtk.ApplicationWindow(application=self.app, title=TITLE, default_width=1280, default_height=800)
        self.title = _label(TITLE, "title", xalign=0.5)
        win.set_titlebar(Gtk.HeaderBar(title_widget=self.title))
        box = Gtk.Box()
        box.append(sidebar)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        box.append(self.stack)
        win.set_child(box)
        return win

    def _sync_title(self):
        name = self.stack.get_visible_child_name()
        page = next((p for p in self.pages if p.name == name), None)
        text = page.title if page else TITLE
        if Adw:
            self.title.set_title(text)
        else:
            self.title.set_label(text)

    def _on_close(self, *_):
        if not self.busy:
            return False
        page = next((p for p in self.pages if p.name == self.stack.get_visible_child_name()), self.pages[0])
        page.feedback("warning", "A change is still running; wait for it to finish before closing.")
        return True

    def set_busy(self, on: bool):
        self.busy = on
        for page in self.pages:
            page.sync()

    def refresh_all(self, page=None, after=None):
        """Re-read every section; after() runs when `page` has reloaded."""
        for p in self.pages:
            p.refresh(after if p is page else None)

    def run_setup(self, page, then=None):
        """Install the system component off the UI thread; feedback goes to `page`."""
        if self.busy:
            return
        self.set_busy(True)

        def start(discard: bool):
            page.feedback("info", "Installing the system component… Approve the administrator prompt.",
                          working=True)

            def work():
                try:
                    res, error = core.install_system(discard_login=discard), None
                except Exception as e:
                    res, error = None, f"Unexpected error: {e or type(e).__name__}"
                GLib.idle_add(done, res, error, discard)

            threading.Thread(target=work, daemon=True).start()

        def done(res, error, discard):
            if error:
                self.set_busy(False)
                page.feedback("error", error)
                return False
            if not res.ok and getattr(res, "needs_discard_login", False) and not discard:
                heading, body = discard_text(res)
                self.ask(heading, body, "Reset Login Background & _Continue", True,
                         lambda ok: start(True) if ok else cancelled())
                return False
            self.set_busy(False)
            page.feedback(*setup_feedback("install", res))
            self.refresh_all(page, then if res.ok else None)
            return False

        def cancelled():
            self.set_busy(False)
            page.feedback("info", "Setup cancelled. Nothing was changed.")

        start(False)

    def confirm_blocking(self, message: str):
        """core.run confirm callback; called from a worker thread, asks on the UI thread."""
        if threading.current_thread() is threading.main_thread():
            return None  # cannot block the UI loop
        done, answer = threading.Event(), [False]

        def finish(ok):
            answer[0] = ok
            done.set()

        def ask():
            try:
                self._ask(message, finish)
            except Exception:
                finish(False)  # a failed dialog counts as declined
            return False

        GLib.idle_add(ask)
        done.wait()
        return answer[0]

    def _ask(self, message: str, finish):
        heading, body = confirm_text(message)
        self.ask(heading, body, "_Continue", message == core.GDM_WARNING, finish)

    def ask(self, heading: str, body: str, accept: str, risky: bool, finish):
        """Modal yes/no; finish(True) only when `accept` is chosen."""
        if Adw:
            dlg = Adw.AlertDialog(heading=heading, body=body)
            dlg.add_response("cancel", "_Cancel")
            dlg.add_response("continue", accept)
            dlg.set_response_appearance("continue", Adw.ResponseAppearance.DESTRUCTIVE if risky
                                        else Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            def answered(d, res):
                try:
                    finish(d.choose_finish(res) == "continue")
                except Exception:
                    finish(False)

            dlg.choose(self.window, None, answered)
            return
        dlg = Gtk.AlertDialog(message=heading, detail=body, buttons=["Cancel", accept.replace("_", "")],
                              cancel_button=0, default_button=0, modal=True)

        def done(d, res):
            try:
                finish(d.choose_finish(res) == 1)
            except Exception:
                finish(False)

        dlg.choose(self.window, None, done)


def build_app(use_adw: bool = True):
    """Create the Gtk/Adw application; returns (app, error message)."""
    error = _load_toolkit(use_adw)
    if error:
        return None, error
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        return None, "no graphical display found; use the command line (tenfold-tweak --help)"
    GLib.set_prgname("tenfold-tweak")
    GLib.set_application_name(TITLE)
    app_cls = Adw.Application if Adw else Gtk.Application
    app = app_cls(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    state = {}

    def on_activate(a):
        if "dash" not in state:
            css = Gtk.CssProvider()
            css.load_from_string(CSS + ("" if Adw else CSS_NO_ADW))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            Gtk.Window.set_default_icon_name("preferences-desktop-wallpaper")
            state["dash"] = Dashboard(a)
        state["dash"].window.present()

    app.connect("activate", on_activate)
    app.state = state
    return app, None


def notice_command(message: str, which=shutil.which) -> list | None:
    """argv to show an error without GTK (zenity, else notify-send); None if neither exists."""
    if which("zenity"):
        return ["zenity", "--error", "--no-markup", f"--title={TITLE}", "--width=420", f"--text={message}"]
    if which("notify-send"):
        return ["notify-send", TITLE, message]
    return None


def _report_error(message: str):
    print(f"tenfold-tweak: {message}", file=sys.stderr)
    if sys.stderr.isatty():
        return
    cmd = notice_command(message)  # started from the menu: nobody sees stderr
    if cmd:
        try:
            subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=300, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def main() -> int:
    app, error = build_app()
    if error:
        _report_error(error)
        return 2
    return app.run([])
