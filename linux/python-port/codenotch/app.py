"""The Linux host — what main.rs is to the Windows port.

The notch, its hover card, Settings and the drop zones are the Windows port's own pages, run in
WebKitGTK. This side does what Tauri and Win32 did there:
  - an override-redirect, transparent, always-on-top window welded to a screen edge (X11)
  - click-through outside the pill/card, via the X11 input shape the page's hot rects define
  - a pointer watchdog (hover in/out, fold on hover), the Alt-drag slide and the move-handle carry
  - the right-click menu, the tray indicator, Settings, and every command the pages invoke
"""
from __future__ import annotations

import subprocess
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import (__version__, activity, autostart, claude_auth, config, geometry, glyphs,  # noqa: E402
               hooks, hookserver, i18n, sessions, watcher)
from .models import UsageSnapshot  # noqa: E402
from .rings import ring_window  # noqa: E402
from .providers import antigravity, claude, codex, cursor, glm, grok  # noqa: E402
from .webhost import Deferred, WebHost  # noqa: E402

# id the pages use, module, event name, persisted file, label
PROVIDERS = (
    ("claude", claude, "usage", "usage", "Claude"),
    ("codex", codex, "codex", "codex", "Codex"),
    ("glm", glm, "glm", "glm-usage", "z.ai"),
    ("cursor", cursor, "cursor", "cursor", "Cursor"),
    ("grok", grok, "grok", "grok", "Grok"),
    ("gemini", antigravity, "antigravity", "antigravity", "Antigravity"),
)
LABELS = {p[0]: p[4] for p in PROVIDERS}
PAGES = {
    "claude": ("https://claude.ai/settings/usage", "claude.ai"),
    "codex": ("https://chatgpt.com/#settings/Account", "chatgpt.com"),
    "cursor": ("https://cursor.com/dashboard", "cursor.com"),
    "grok": ("https://grok.com/?_s=usage", "grok.com"),
    "gemini": ("https://antigravity.google", "antigravity.google"),
    "glm": ("https://z.ai/manage-apikey/apikey-list", "z.ai"),
}
AUTHOR_PAGE = "https://x.com/hivinz_"
WATCHDOG_MS = 50
LEAVE_TICKS = 6  # 300 ms of the pointer being away before the card folds
HOVER_REFRESH_MS = 5 * 60 * 1000


def open_uri(uri: str) -> None:
    try:
        Gtk.show_uri_on_window(None, uri, Gdk.CURRENT_TIME)
    except GLib.Error:
        try:
            subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as e:
            config.log(f"open {uri}: {e}")


def _clear_draw(_widget, cr):
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    return False


def _region(rects):
    return cairo.Region([cairo.RectangleInt(int(x), int(y), int(w), int(h)) for x, y, w, h in rects])


# ---------------- providers ----------------

class Poller:
    def __init__(self, app, pid, module, event, store_name):
        self.app, self.pid, self.module, self.event, self.store_name = app, pid, module, event, store_name
        self.snapshot = config.load_snapshot(store_name)
        if pid != "claude" and not self.snapshot.windows and self.snapshot.status not in ("absent", ""):
            self.snapshot.status = "absent"
        self.wake = threading.Event()

    def start(self):
        threading.Thread(target=self._run, name=f"poll-{self.pid}", daemon=True).start()

    def request(self):
        self.wake.set()

    def _interval(self) -> float:
        now = config.now_ms()
        if self.pid == "claude":
            base = 60 if self.app.store.has_active() else 300
        elif self.snapshot.status == "absent":
            base = 600
        else:
            base = getattr(self.module, "poll_secs", 300)
        until = self.snapshot.backoff_until
        if until > now:
            base = min(base, max(1.0, min(30.0, (until - now) / 1000)))
        return base

    def _run(self):
        first = True
        while True:
            if not first:
                self.wake.wait(timeout=self._interval())
                self.wake.clear()
            first = False
            if self.pid == "claude" and claude_auth.busy():
                time.sleep(2)
                self.wake.set()
                continue
            try:
                if self.pid != "claude" and not self.module.present():
                    snap = UsageSnapshot(status="absent")
                else:
                    snap = self.module.read_once(self.snapshot)
            except Exception as e:  # one provider's bug must not stop its poller
                config.log(f"{self.pid}: {e}")
                continue
            self.snapshot = snap
            config.persist_snapshot(self.store_name, snap)
            if self.pid == "claude" and snap.status == "ok":
                claude_auth.usage_succeeded()
            GLib.idle_add(self.app.on_reading, self.pid)


# ---------------- the app ----------------

class CodenotchApp:
    def __init__(self, gtk_app: Gtk.Application):
        self.gtk_app = gtk_app
        self.cfg = config.Config()
        self.store = sessions.Store()
        self.pollers = {pid: Poller(self, pid, mod, ev, store) for pid, mod, ev, store, _ in PROVIDERS}
        self.glyphs: dict = {}
        self.activity_list: list = []
        self.hot: list = []
        self.expanded = False
        self.pointer_inside = None
        self.miss = 0
        self.tick = 0
        self.dragging = False
        self.menu_open = False
        self.menu = None
        self.geom = (0, 0, 1, 1)
        self.insets = [0.0, 0.0, 0.0, 0.0]
        self.page_dpr = 1.0
        self.page_css_w = 0.0
        self.work_sig = None
        self.theme_seen = None
        self.zones = None
        self.dropzones = None
        self.dropzones_win = None
        self.settings = None
        self.settings_win = None
        self.hosts: list = []
        self.tray = None
        self.composited = False
        self.commands = {name[4:]: getattr(self, name) for name in dir(self) if name.startswith("cmd_")}

    # ---- start / stop ----

    def start(self):
        config.log(f"codenotch-linux {__version__} starting")
        self._build_notch()
        self.place_notch()
        self.apply_visibility()
        try:
            from .tray import Tray
            self.tray = Tray(self)
        except (ImportError, ValueError) as e:
            config.log(f"tray: unavailable ({e}); the notch is the way in")
            self.tray = None
        self.apply_visibility()
        if not hookserver.start(self.cfg.get("port"), lambda ev: GLib.idle_add(self._on_hook, ev)):
            config.log("hook events will not arrive: port in use")
        watcher.Watcher(lambda ev: GLib.idle_add(self._on_hook, ev)).start()
        self.activity = activity.ActivityProbe(lambda acts: GLib.idle_add(self._on_activity, acts))
        self.activity.start()
        for p in self.pollers.values():
            p.start()
        threading.Thread(target=self._reload_glyphs, daemon=True).start()
        GLib.timeout_add(WATCHDOG_MS, self._watchdog)
        GLib.timeout_add_seconds(30, self._sweep)
        disp = Gdk.Display.get_default()
        disp.connect("monitor-added", lambda *a: GLib.idle_add(self._placement_changed))
        disp.connect("monitor-removed", lambda *a: GLib.idle_add(self._placement_changed))
        self.cfg.save()

    def quit(self):
        self.gtk_app.quit()

    # ---- emitting ----

    def emit_all(self, event, payload=None):
        for h in list(self.hosts):
            h.emit(event, payload)

    def emit_notch(self, event, payload=None):
        if self.notch:
            self.notch.emit(event, payload)

    def state_snapshot(self):
        lang = self.cfg.get("lang")
        return self.store.snapshot(lang, i18n.resolved(lang), i18n.clock_24h())

    def broadcast_state(self):
        self.emit_all("state", self.state_snapshot())
        return False

    def on_reading(self, pid):
        p = self.pollers[pid]
        self.emit_all(p.event, p.snapshot.to_dict())
        if self.tray:
            self.tray.update()
        return False

    def _on_hook(self, ev):
        if self.store.apply(ev):
            self.broadcast_state()
        return False

    def _on_activity(self, acts):
        self.activity_list = acts
        self.emit_all("activity", acts)
        return False

    def _sweep(self):
        if self.store.sweep():
            self.broadcast_state()
        return True

    def _reload_glyphs(self):
        g = glyphs.collect()
        GLib.idle_add(self._set_glyphs, g)

    def _set_glyphs(self, g):
        self.glyphs = g
        self.emit_all("glyphs", g)
        return False

    # ---- theme ----

    def resolved_theme(self) -> str:
        choice = self.cfg.get("theme")
        if choice in ("light", "dark"):
            return choice
        s = Gtk.Settings.get_default()
        if s is not None:
            if s.get_property("gtk-application-prefer-dark-theme"):
                return "dark"
            name = (s.get_property("gtk-theme-name") or "").lower()
            if "dark" in name or "black" in name:
                return "dark"
            return "light"
        return "dark"

    def apply_theme(self):
        self.theme_seen = self.resolved_theme()
        self.emit_all("theme_resolved", self.theme_seen)

    def accent_palette(self):
        """The GTK theme's selection colour as the page's seven-step accent palette."""
        try:
            ctx = self.notch_win.get_style_context()
            ok, rgba = ctx.lookup_color("theme_selected_bg_color")
        except (AttributeError, TypeError):
            ok = False
        if not ok:
            return []

        def mix(t, towards):
            r, g, b = (rgba.red + (towards - rgba.red) * t, rgba.green + (towards - rgba.green) * t,
                       rgba.blue + (towards - rgba.blue) * t)
            return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))

        return [mix(0.6, 1), mix(0.4, 1), mix(0.2, 1), mix(0, 1), mix(0.2, 0), mix(0.4, 0), mix(0.6, 0)]

    # ---- screens and placement ----

    def screens(self):
        disp = Gdk.Display.get_default()
        out = []
        for i in range(disp.get_n_monitors()):
            m = disp.get_monitor(i)
            g, wa = m.get_geometry(), m.get_workarea()
            model = m.get_model() or f"Monitor {i + 1}"
            s = geometry.Screen(id=f"{model}@{g.x},{g.y}", x=g.x, y=g.y, w=g.width, h=g.height,
                                work=(wa.x, wa.y, wa.width, wa.height))
            if m.is_primary():
                out.insert(0, s)
            else:
                out.append(s)
        return out

    def target_screen(self):
        lst = self.screens()
        want = self.cfg.get("notch_monitor")
        if want:
            for s in lst:
                if s.id == want:
                    return s
        return lst[0] if lst else None

    def _build_notch(self):
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_title("Codenotch")
        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        self.composited = bool(visual is not None and screen.is_composited())
        if self.composited:
            win.set_visual(visual)
        else:
            config.log("no compositor: the notch is cut to its shape instead of drawn translucent")
        win.set_app_paintable(True)
        win.connect("draw", _clear_draw)
        win.set_accept_focus(False)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        screen.connect("composited-changed", lambda *a: config.log("compositor changed; restart Codenotch if the notch looks wrong"))
        self.notch_win = win
        self.notch = WebHost("notch", "notch.html", win, self.dispatch, self.resolved_theme(), transparent=True)
        self.hosts.append(self.notch)
        self.theme_seen = self.resolved_theme()
        win.realize()
        self._apply_shape()

    def place_notch(self):
        mon = self.target_screen()
        if mon is None:
            return
        edge = config.edge_or_right(self.cfg.get("notch_edge"))
        size = config.snap_scale(self.cfg.get("scale"))
        w, h = geometry.notch_window_size(edge)
        ww, wh = min(round(w * size), mon.w), min(round(h * size), mon.h)
        self.notch_win.resize(ww, wh)
        self.notch.set_zoom(size)
        x, y = geometry.edge_origin(mon, edge, ww, wh, self.cfg.along(edge))
        self.notch_win.move(x, y)
        self.geom = (x, y, ww, wh)
        self.work_sig = mon.work
        self.emit_notch("notch_edge", edge)
        self.insets = [v / size for v in geometry.work_insets(mon, x, y, ww, wh)]
        self.emit_notch("notch_insets", self.insets)
        self._apply_shape()
        config.log(f"notch placed: edge={edge} pos=({x},{y}) size=({ww}x{wh}) scale={size} "
                   f"monitor={mon.id} work={mon.work} composited={self.composited}")

    def _placement_changed(self):
        if not self.dragging:
            self.place_notch()
        return False

    def reset_bar(self):
        want = self.cfg.get("notch_monitor")
        if want and not any(s.id == want for s in self.screens()):
            self.cfg.set("notch_edge", "right", save=False)
            self.cfg.set("notch_monitor", None, save=False)
        self.cfg.set_along(config.edge_or_right(self.cfg.get("notch_edge")), 0.5)
        self.place_notch()

    # ---- click-through and hover ----

    def _hot_rects_from_page(self, rects):
        """Page rects are CSS px × devicePixelRatio; the window wants its own logical px."""
        dpr = self.page_dpr or 1.0
        ww = self.geom[2]
        k = (ww / self.page_css_w) if self.page_css_w > 0 else config.snap_scale(self.cfg.get("scale"))
        out = []
        for r in rects:
            if isinstance(r, (list, tuple)) and len(r) == 4:
                try:
                    out.append([float(v) / dpr * k for v in r])
                except (TypeError, ValueError):
                    continue
        return out

    def _apply_shape(self):
        gdkwin = self.notch_win.get_window()
        if gdkwin is None:
            return
        _, _, ww, wh = self.geom
        rects = geometry.input_region_rects(self.hot, ww, wh)
        self.notch_win.input_shape_combine_region(_region(rects))
        if self.composited:
            # A bounding shape covering the whole window: no visible change, and a shaped window
            # is one the compositor draws no drop shadow around.
            self.notch_win.shape_combine_region(_region([(0, 0, ww, wh)]))
        else:
            self.notch_win.shape_combine_region(_region(rects) if rects else _region([(0, 0, 1, 1)]))

    def _pointer(self):
        seat = Gdk.Display.get_default().get_default_seat()
        dev = seat.get_pointer()
        root = Gdk.get_default_root_window()
        _w, x, y, mask = root.get_device_position(dev)
        return x, y, bool(mask & Gdk.ModifierType.BUTTON1_MASK)

    def _watchdog(self):
        self.tick += 1
        if self.notch_win.get_visible():
            x, y, _down = self._pointer()
            gx, gy, ww, wh = self.geom
            inside = geometry.cursor_in_hot(self.hot, x - gx, y - gy, (ww, wh))
            if inside != self.pointer_inside:
                self.pointer_inside = inside
                self.emit_notch("notch_pointer", inside)
            if self.expanded:
                if inside:
                    self.miss = 0
                else:
                    self.miss += 1
                    if self.miss >= LEAVE_TICKS:
                        self.miss = 0
                        self.expanded = False
                        self.emit_all("pointer_left")
            else:
                self.miss = 0
        if self.tick % 20 == 0:
            self._housekeeping()
        return True

    def _housekeeping(self):
        if self.notch_win.get_visible() and not self.menu_open:
            gdkwin = self.notch_win.get_window()
            if gdkwin is not None:
                gdkwin.raise_()
        if not self.dragging:
            mon = self.target_screen()
            if mon is not None and mon.work != self.work_sig:
                config.log(f"work area changed: {self.work_sig} -> {mon.work}")
                self.place_notch()
        theme = self.resolved_theme()
        if theme != self.theme_seen:
            config.log(f"appearance changed: {self.theme_seen} -> {theme}")
            self.apply_theme()

    # ---- visibility ----

    def ui_flags(self):
        return {"notch_visible": self.cfg.get("notch_visible"), "notch_on_hover": self.cfg.get("notch_on_hover"),
                "tray_visible": self.cfg.get("tray_visible")}

    def apply_visibility(self):
        self.emit_all("ui_flags", self.ui_flags())
        if self.cfg.get("notch_visible"):
            if not self.notch_win.get_visible():
                self.notch_win.show_all()
                self._apply_shape()
            self.place_notch()
        else:
            self.notch_win.hide()
        if self.tray:
            self.tray.set_visible(self.cfg.get("tray_visible") or not self.cfg.get("notch_visible"))

    def toggle_keep_open(self):
        self.cfg.set("notch_on_hover", not self.cfg.get("notch_on_hover"))
        self.apply_visibility()

    # ---- refreshing ----

    def refresh_provider(self, pid: str) -> bool:
        p = self.pollers.get(pid)
        if p is None:
            return False
        if pid == "claude" and p.snapshot.backoff_until > config.now_ms():
            return False
        if pid == "glm":
            glm.request_refresh()
        p.request()
        return True

    def refresh_all(self):
        for pid in self.pollers:
            self.refresh_provider(pid)
        threading.Thread(target=self._reload_glyphs, daemon=True).start()

    def ring_pct(self, pid):
        snap = self.pollers[pid].snapshot
        if snap.status == "absent":
            return None
        w = ring_window(pid, snap.windows, self.cfg.get("antigravity_limit"), self.cfg.get("antigravity_model"))
        if w is None or w.count is not None:
            return None
        return int(round(max(0.0, min(1.0, w.used)) * 100))

    # ---- windows ----

    def open_settings(self):
        if self.settings_win is not None:
            self.settings_win.present()
            return
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("Codenotch Settings")
        win.set_icon_name("codenotch")
        win.set_default_size(680, 520)
        win.set_resizable(False)
        win.set_decorated(False)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        host = WebHost("settings", "settings.html", win, self.dispatch, self.resolved_theme(), transparent=False)
        self.settings, self.settings_win = host, win
        self.hosts.append(host)

        def closed(*_):
            host.destroy()
            if host in self.hosts:
                self.hosts.remove(host)
            self.settings = self.settings_win = None

        win.connect("destroy", closed)
        win.show_all()
        win.present()

    def _dropzones_show(self, mon, zones):
        self.zones = zones
        ax, ay, aw, ah = mon.area()
        if self.dropzones_win is None:
            win = Gtk.Window(type=Gtk.WindowType.POPUP)
            screen = win.get_screen()
            visual = screen.get_rgba_visual()
            if visual is not None and screen.is_composited():
                win.set_visual(visual)
            win.set_app_paintable(True)
            win.connect("draw", _clear_draw)
            win.set_accept_focus(False)
            win.set_keep_above(True)
            host = WebHost("dropzones", "dropzones.html", win, self.dispatch, self.resolved_theme(), transparent=True)
            self.dropzones, self.dropzones_win = host, win
            self.hosts.append(host)
            win.realize()
            win.input_shape_combine_region(cairo.Region())
        self.dropzones_win.move(ax, ay)
        self.dropzones_win.resize(aw, ah)
        self.dropzones_win.show_all()
        self.dropzones_win.input_shape_combine_region(cairo.Region())
        self.dropzones.emit("zones", zones)
        gdkwin = self.notch_win.get_window()
        if gdkwin is not None:
            gdkwin.raise_()

    def _dropzones_hide(self):
        self.zones = None
        if self.dropzones_win is not None:
            self.dropzones.destroy()
            if self.dropzones in self.hosts:
                self.hosts.remove(self.dropzones)
            self.dropzones_win.destroy()
            self.dropzones = self.dropzones_win = None

    # ---- the bridge ----

    def dispatch(self, host, cmd, args, deferred):
        fn = self.commands.get("internal_" + cmd[2:] if cmd.startswith("__") else cmd)
        if fn is None:
            raise RuntimeError(f"unknown command {cmd}")
        return fn(host, args, deferred)

    # readings
    def cmd_get_state(self, h, a, d): return self.state_snapshot()
    def cmd_get_usage(self, h, a, d): return self.pollers["claude"].snapshot.to_dict()
    def cmd_get_codex(self, h, a, d): return self.pollers["codex"].snapshot.to_dict()
    def cmd_get_cursor(self, h, a, d): return self.pollers["cursor"].snapshot.to_dict()
    def cmd_get_grok(self, h, a, d): return self.pollers["grok"].snapshot.to_dict()
    def cmd_get_antigravity(self, h, a, d): return self.pollers["gemini"].snapshot.to_dict()
    def cmd_get_glm(self, h, a, d): return self.pollers["glm"].snapshot.to_dict()
    def cmd_get_glyphs(self, h, a, d): return self.glyphs
    def cmd_get_activity(self, h, a, d): return self.activity_list

    def cmd_refresh_ring(self, h, a, d):
        return self.refresh_provider(str(a.get("provider") or "").split("@")[0])

    def cmd_get_claude_auth(self, h, a, d): return claude_auth.state()

    def cmd_claude_sign_in(self, h, a, d):
        claude_auth.start_login(lambda: GLib.idle_add(lambda: (self.refresh_provider("claude"), False)[1]))
        return None

    def cmd_get_tray_options(self, h, a, d):
        return [{"id": pid, "label": LABELS[pid], "status": self.pollers[pid].snapshot.status,
                 "used": self.ring_pct(pid)} for pid in ("claude", "codex", "glm", "cursor", "grok", "gemini")]

    # geometry and hover
    def cmd_set_hot(self, h, a, d):
        was = self.expanded
        self.hot = self._hot_rects_from_page(a.get("rects") or [])
        self.expanded = bool(a.get("expanded"))
        self._apply_shape()
        if self.expanded and not was:
            ag = self.pollers["gemini"].snapshot
            if ag.status != "absent" and config.now_ms() - ag.fetched_at >= HOVER_REFRESH_MS:
                self.pollers["gemini"].request()
        return None

    def cmd_report_dpr(self, h, a, d):
        try:
            self.page_dpr = float(a.get("dpr") or 1.0) or 1.0
            self.page_css_w = float(a.get("w") or 0.0)
        except (TypeError, ValueError):
            pass
        config.log(f"dpr report: dpr={self.page_dpr} viewport={a.get('w')}x{a.get('h')} window={self.geom[2]}x{self.geom[3]}")
        return None

    def cmd_notch_hidden(self, h, a, d): return None

    def cmd_log_js(self, h, a, d):
        config.log(f"js[{h.label}]: {str(a.get('msg'))[:600]}")
        return None

    def cmd_get_notch_insets(self, h, a, d): return self.insets
    def cmd_get_notch_edge(self, h, a, d): return config.edge_or_right(self.cfg.get("notch_edge"))

    def cmd_set_notch_edge(self, h, a, d):
        self.cfg.set("notch_edge", config.edge_or_right(a.get("edge")))
        self.place_notch()
        return self.cfg.get("notch_edge")

    def cmd_reset_notch_position(self, h, a, d):
        self.reset_bar()
        return None

    def cmd_get_monitors(self, h, a, d):
        want = self.cfg.get("notch_monitor")
        chosen = self.target_screen()
        return [{"id": s.id, "label": f"{i + 1}  {s.w} × {s.h}", "primary": i == 0,
                 "current": (s.id == chosen.id) if (want and chosen) else i == 0}
                for i, s in enumerate(self.screens())]

    def cmd_set_notch_monitor(self, h, a, d):
        v = a.get("id")
        self.cfg.set("notch_monitor", v if isinstance(v, str) and v else None)
        self.place_notch()
        return None

    def cmd_get_scale(self, h, a, d): return config.snap_scale(self.cfg.get("scale"))

    def cmd_set_scale(self, h, a, d):
        self.cfg.set("scale", config.snap_scale(a.get("scale")))
        self.place_notch()
        return self.cfg.get("scale")

    def cmd_drag_begin(self, h, a, d):
        if self.dragging:
            return None
        mon = self.target_screen()
        if mon is None:
            return None
        self.dragging = True
        edge = config.edge_or_right(self.cfg.get("notch_edge"))
        vertical = config.edge_is_vertical(edge)
        sx, sy, _ = self._pointer()
        gx, gy, ww, wh = self.geom
        ax, ay, aw, ah = mon.area()
        st = {"moved": False, "x": gx, "y": gy}

        def step():
            x, y, down = self._pointer()
            if not down:
                if st["moved"]:
                    along = (geometry.along_at(st["y"], wh, ay, ah) if vertical
                             else geometry.along_at(st["x"], ww, ax, aw))
                    self.cfg.set_along(edge, along)
                    self.place_notch()
                self.dragging = False
                self.emit_all("drag_end", st["moved"])
                return False
            if vertical:
                nx, ny = gx, max(ay, min(round(gy + (y - sy)), max(ay, ay + ah - wh)))
            else:
                nx, ny = max(ax, min(round(gx + (x - sx)), max(ax, ax + aw - ww))), gy
            if (nx, ny) != (st["x"], st["y"]):
                st.update(moved=True, x=nx, y=ny)
                self.notch_win.move(nx, ny)
                self.geom = (nx, ny, ww, wh)
            return True

        GLib.timeout_add(8, step)
        return None

    def cmd_begin_move(self, h, a, d):
        if self.dragging:
            return None
        start = self.target_screen()
        if start is None:
            self.emit_all("move_end")
            return None
        self.dragging = True
        try:
            depth, length = float(a.get("depth") or 70), float(a.get("length") or 200)
        except (TypeError, ValueError):
            depth, length = 70.0, 200.0
        size = config.snap_scale(self.cfg.get("scale"))
        origin = config.edge_or_right(self.cfg.get("notch_edge"))
        every = self.screens()
        st = {"mon": start, "target": origin}

        def zones_on(s, target):
            _, _, aw, ah = s.area()
            return {"w": aw, "h": ah, "depth": depth * size, "length": length * size, "target": target}

        self._dropzones_show(start, zones_on(start, origin))

        def step():
            x, y, down = self._pointer()
            mon = st["mon"]
            if down:
                s = geometry.screen_at(every, x, y)
                if s is not None and not geometry.same_screen(s, mon):
                    st["mon"] = mon = s
                    self._dropzones_show(mon, zones_on(mon, st["target"]))
                ax, ay, aw, ah = mon.area()
                nxt = geometry.edge_at(x - ax, y - ay, aw, ah)
                if nxt != st["target"]:
                    st["target"] = nxt
                    self.zones = zones_on(mon, nxt)
                    if self.dropzones:
                        self.dropzones.emit("zones", self.zones)
                    self.emit_all("move_target", nxt)
                return True
            crossed = not geometry.same_screen(mon, start)
            config.log(f"notch carry: {origin} -> {st['target']} on {mon.id}")
            if st["target"] != origin or crossed:
                self.cfg.set("notch_edge", st["target"], save=False)
                if crossed:
                    self.cfg.set("notch_monitor", mon.id, save=False)
                self.cfg.save()
                self.place_notch()
            self._dropzones_hide()
            self.dragging = False
            self.emit_all("move_end")
            return False

        GLib.timeout_add(16, step)
        return None

    def cmd_get_zones(self, h, a, d): return self.zones

    def cmd_show_notch_menu(self, h, a, d):
        pid = str(a.get("provider") or "").split("@")[0]
        lang = self.cfg.get("lang")
        menu = Gtk.Menu()

        def add(label, cb):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda *_: cb())
            menu.append(item)

        add(i18n.tr(lang, "refresh_now"), self.refresh_all)
        if pid in PAGES:
            url, host_name = PAGES[pid]
            add(i18n.tr(lang, "open_host").replace("%@", host_name), lambda: open_uri(url))
        menu.append(Gtk.SeparatorMenuItem())
        keep = Gtk.CheckMenuItem(label=i18n.tr(lang, "keep_open"))
        keep.set_active(not self.cfg.get("notch_on_hover"))
        keep.connect("toggled", lambda *_: self.toggle_keep_open())
        menu.append(keep)
        menu.append(Gtk.SeparatorMenuItem())
        add(i18n.tr(lang, "quit_app"), self.quit)
        menu.show_all()

        def closed(*_):
            def later():
                self.menu_open = False
                d.resolve(None)
                return False
            GLib.idle_add(later)

        menu.connect("deactivate", closed)
        self.menu, self.menu_open = menu, True
        menu.popup(None, None, None, None, 3, Gtk.get_current_event_time())
        return d

    # appearance
    def cmd_get_theme(self, h, a, d): return self.cfg.get("theme")
    def cmd_get_theme_resolved(self, h, a, d): return self.resolved_theme()

    def cmd_set_theme(self, h, a, d):
        if a.get("theme") in ("system", "light", "dark"):
            self.cfg.set("theme", a["theme"])
        self.apply_theme()
        self.emit_all("theme", self.cfg.get("theme"))
        return self.cfg.get("theme")

    def cmd_get_system_look(self, h, a, d): return {"mica": False, "accent": self.accent_palette()}
    def cmd_get_weekly_ring(self, h, a, d): return self.cfg.get("weekly_ring")

    def cmd_set_weekly_ring(self, h, a, d):
        if a.get("placement") in ("off", "inside", "outside"):
            self.cfg.set("weekly_ring", a["placement"])
        self.emit_all("weekly_ring", self.cfg.get("weekly_ring"))
        return self.cfg.get("weekly_ring")

    def cmd_get_notch_slots(self, h, a, d): return self.cfg.get("notch_slots")

    def cmd_set_notch_slots(self, h, a, d):
        self.cfg.set("notch_slots", a.get("slots") or [])
        self.emit_all("notch_slots", self.cfg.get("notch_slots"))
        return None

    def cmd_get_antigravity_prefs(self, h, a, d):
        return {"limit": self.cfg.get("antigravity_limit"), "model": self.cfg.get("antigravity_model")}

    def cmd_set_antigravity_prefs(self, h, a, d):
        if a.get("limit") in ("automatic", "5h", "weekly"):
            self.cfg.set("antigravity_limit", a["limit"], save=False)
        if a.get("model") in ("gemini", "3p"):
            self.cfg.set("antigravity_model", a["model"], save=False)
        self.cfg.save()
        prefs = self.cmd_get_antigravity_prefs(h, a, d)
        self.emit_all("antigravity_prefs", prefs)
        if self.tray:
            self.tray.update()
        return prefs

    def cmd_get_ui_flags(self, h, a, d): return self.ui_flags()

    def cmd_set_ui_flags(self, h, a, d):
        notch = bool(a.get("notch_visible", True))
        self.cfg.set("notch_visible", notch, save=False)
        if a.get("notch_on_hover") is not None:
            self.cfg.set("notch_on_hover", bool(a.get("notch_on_hover")), save=False)
        self.cfg.set("tray_visible", bool(a.get("tray_visible", True)) if notch else True, save=False)
        self.cfg.save()
        self.apply_visibility()
        return self.ui_flags()

    def cmd_get_move_handle(self, h, a, d): return self.cfg.get("show_move_handle")

    def cmd_set_move_handle(self, h, a, d):
        self.cfg.set("show_move_handle", bool(a.get("on")))
        self.emit_all("move_handle", self.cfg.get("show_move_handle"))
        return self.cfg.get("show_move_handle")

    def cmd_get_app_icon(self, h, a, d): return None

    # general
    def cmd_get_lang(self, h, a, d): return self.cfg.get("lang")
    def cmd_get_lang_resolved(self, h, a, d): return i18n.resolved(self.cfg.get("lang"))

    def cmd_set_lang(self, h, a, d):
        lang = a.get("lang")
        if lang == "auto" or lang in i18n.LANGS:
            self.cfg.set("lang", lang)
        self.broadcast_state()
        if self.tray:
            self.tray.update()
        return None

    def cmd_get_autostart(self, h, a, d): return autostart.is_enabled()

    def cmd_set_autostart(self, h, a, d):
        return autostart.enable() if a.get("on") else autostart.disable()

    def cmd_get_hooks_installed(self, h, a, d): return hooks.is_installed()

    def cmd_set_hooks_installed(self, h, a, d):
        return hooks.install() if a.get("on") else hooks.uninstall()

    def cmd_open_data_dir(self, h, a, d):
        glyphs.user_dir()
        open_uri(config.config_dir().as_uri())
        return None

    def cmd_open_author_page(self, h, a, d):
        open_uri(AUTHOR_PAGE)
        return None

    def cmd_open_settings(self, h, a, d):
        GLib.idle_add(lambda: (self.open_settings(), False)[1])
        return None

    def cmd_focus_session(self, h, a, d): return False

    def cmd_dismiss_session(self, h, a, d):
        if self.store.dismiss(str(a.get("id") or "")):
            self.broadcast_state()
        return None

    def cmd_get_update_state(self, h, a, d):
        return {"checking": False, "installing": False, "available": None, "message": ""}

    def cmd_check_for_update(self, h, a, d):
        self.emit_all("update_state", {"checking": False, "installing": False, "available": None,
                                       "message": "the Linux build is updated by installing a newer .deb"})
        return None

    def cmd_install_update(self, h, a, d):
        raise RuntimeError("the Linux build is updated by installing a newer .deb")

    def cmd_quit_app(self, h, a, d):
        GLib.idle_add(lambda: (self.quit(), False)[1])
        return None

    # the bridge's own
    def cmd_internal_app_version(self, h, a, d): return __version__

    def cmd_internal_close_window(self, h, a, d):
        if h is self.settings and self.settings_win is not None:
            GLib.idle_add(lambda: (self.settings_win and self.settings_win.destroy(), False)[1])
        return None

    def cmd_internal_start_window_drag(self, h, a, d):
        if h is self.settings and self.settings_win is not None:
            x, y, _ = self._pointer()
            self.settings_win.begin_move_drag(1, x, y, Gtk.get_current_event_time())
        return None


def run() -> int:
    gtk_app = Gtk.Application(application_id="io.github.codenotch.linux", flags=Gio.ApplicationFlags.FLAGS_NONE)
    holder = {}

    def on_activate(app):
        if "core" in holder:
            holder["core"].open_settings()  # a second launch brings Settings forward, as on Windows
            return
        core = CodenotchApp(app)
        holder["core"] = core
        app.hold()
        core.start()

    gtk_app.connect("activate", on_activate)
    return gtk_app.run(None)
