"""The tray icon in the XFCE panel (StatusNotifierItem through libayatana-appindicator), the
Windows port's tray: a line per provider with its headline figure and one line per limit window,
then Refresh all, Settings… and Quit. Clicking a provider's line re-reads that provider."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3

from gi.repository import Gtk  # noqa: E402

from . import config, i18n, icon  # noqa: E402

ORDER = ("claude", "codex", "glm", "cursor", "grok", "gemini")


def _reset_text(lang: str, ms) -> str:
    if not ms:
        return ""
    diff = ms - config.now_ms()
    if diff <= 0:
        return ""
    minutes = round(diff / 60000)
    if minutes < 60:
        return i18n.tr(lang, "resets_in_min") % max(1, minutes)
    hours = round(diff / 3600000)
    if hours < 48:
        return i18n.tr(lang, "resets_in_h") % hours
    return i18n.tr(lang, "resets_in_d") % round(diff / 86400000)


class Tray:
    def __init__(self, app):
        self.app = app
        self.indicator = AppIndicator3.Indicator.new(
            "codenotch", "codenotch", AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_title("Codenotch")
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.visible = True
        self.last_sig = None
        self.update()

    def set_visible(self, on: bool) -> None:
        self.visible = on
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE if on
                                  else AppIndicator3.IndicatorStatus.PASSIVE)

    def update(self) -> None:
        from .app import LABELS
        from .rings import ring_window
        lang = self.app.cfg.get("lang")
        limit, model = self.app.cfg.get("antigravity_limit"), self.app.cfg.get("antigravity_model")
        menu = Gtk.Menu()
        headline, head_status = None, "ok"
        tooltip = []
        for pid in ORDER:
            snap = self.app.pollers[pid].snapshot
            if snap.status == "absent":
                continue
            w = ring_window(pid, snap.windows, limit, model)
            label = LABELS[pid]
            if snap.status in ("needsAuth",):
                text = f"{label} — {i18n.tr(lang, 'sign_in')}"
            elif w is not None and w.count is None:
                pct = round(max(0.0, min(1.0, w.used)) * 100)
                text = f"{label} — {pct}%"
                tooltip.append(f"{label} {pct}%")
                if headline is None:
                    headline, head_status = w, snap.status
            elif snap.windows:
                text = f"{label} — —"
            else:
                text = f"{label} — {i18n.tr(lang, 'waiting')}"
            if snap.status == "stale":
                text += f" ({i18n.tr(lang, 'stale')})"
            item = Gtk.MenuItem(label=text)
            item.connect("activate", lambda _i, p=pid: self.app.refresh_provider(p))
            menu.append(item)
            for win in snap.windows[:6]:
                if win.count is not None:
                    line = f"~{win.count}"
                else:
                    line = f"{round(max(0.0, min(1.0, win.used)) * 100)}%"
                reset = _reset_text(lang, win.resets_at)
                name = f"{win.group} · {win.label}" if win.group else win.label
                sub = Gtk.MenuItem(label=f"    {name}  {line}" + (f"  ·  {reset}" if reset else ""))
                sub.set_sensitive(False)
                menu.append(sub)
        menu.append(Gtk.SeparatorMenuItem())
        for key, cb in (("refresh_all", self.app.refresh_all), ("settings", self.app.open_settings)):
            it = Gtk.MenuItem(label=i18n.tr(lang, key))
            it.connect("activate", lambda _i, f=cb: f())
            menu.append(it)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label=i18n.tr(lang, "quit_app"))
        quit_item.connect("activate", lambda *_: self.app.quit())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self._menu = menu
        pct = headline.used * 100 if headline else 0.0
        sig = (round(pct), head_status if headline else "absent", self.app.resolved_theme())
        if sig != self.last_sig:
            self.last_sig = sig
            path = icon.render_ring(pct, head_status if headline else "absent")
            self.indicator.set_icon_full(str(path), "Codenotch")
        self.indicator.set_title("Codenotch — " + (" · ".join(tooltip) if tooltip else i18n.tr(lang, "waiting")))
