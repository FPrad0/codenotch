"""A page of the Windows port's UI in a WebKitGTK view, talking to the Python host through the
bridge in ui/bridge.js. Commands arrive as script messages and are answered by id; events are
pushed by evaluating window.__cn_emit in the page."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
from gi.repository import Gdk, Gtk, WebKit2  # noqa: E402

from . import config  # noqa: E402

UI_DIR = Path(__file__).resolve().parent / "ui"
_BRIDGE_JS = (UI_DIR / "bridge.js").read_text(encoding="utf-8")
DEBUG = bool(os.environ.get("CODENOTCH_DEBUG"))
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def snake(name: str) -> str:
    return _CAMEL.sub("_", name).lower()


def to_js(value) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def theme_script(theme: str) -> str:
    return ("window.__CN_THEME__=%s;(function a(){const d=document.documentElement;"
            "if(d){d.dataset.theme=window.__CN_THEME__;}else{document.addEventListener("
            "'readystatechange',a,{once:true});}})();" % to_js(theme))


class Deferred:
    """A command answered later (a menu that has to close first, a sign-in that has to start)."""

    def __init__(self, host: "WebHost", call_id):
        self.host = host
        self.call_id = call_id
        self.done = False

    def resolve(self, value=None):
        if not self.done:
            self.done = True
            self.host.reply(self.call_id, True, value)

    def reject(self, err):
        if not self.done:
            self.done = True
            self.host.reply(self.call_id, False, str(err))


class WebHost:
    def __init__(self, label: str, page: str, window: Gtk.Window, dispatch, theme: str, transparent: bool):
        self.label = label
        self.window = window
        self.dispatch = dispatch
        self.alive = True
        ucm = WebKit2.UserContentManager()
        for src in (_BRIDGE_JS, theme_script(theme)):
            ucm.add_script(WebKit2.UserScript.new(
                src, WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START, None, None))
        ucm.connect("script-message-received::codenotch", self._on_message)
        ucm.register_script_message_handler("codenotch")
        self.ucm = ucm
        self.view = WebKit2.WebView.new_with_user_content_manager(ucm)
        s = self.view.get_settings()
        s.set_enable_developer_extras(DEBUG)
        s.set_enable_write_console_messages_to_stdout(DEBUG)
        s.set_javascript_can_open_windows_automatically(False)
        s.set_enable_back_forward_navigation_gestures(False)
        if not DEBUG:
            self.view.connect("context-menu", lambda *a: True)
        self.view.connect("decide-policy", self._on_policy)
        if transparent:
            self.view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        window.add(self.view)
        self.view.load_uri((UI_DIR / page).as_uri())

    # ---- page -> host ----

    def _on_message(self, _ucm, result):
        try:
            value = result.get_js_value() if hasattr(result, "get_js_value") else result
            msg = json.loads(value.to_string())
        except (ValueError, AttributeError) as e:
            config.log(f"{self.label}: unreadable bridge message ({e})")
            return
        call_id = msg.get("id")
        cmd = msg.get("cmd") or ""
        args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
        args = {snake(k): v for k, v in args.items()}
        try:
            out = self.dispatch(self, cmd, args, Deferred(self, call_id))
        except Exception as e:  # a failed command is the page's to report, not a crash
            if cmd != "log_js":
                config.log(f"{self.label}: {cmd} failed: {e}")
            self.reply(call_id, False, str(e))
            return
        if isinstance(out, Deferred):
            return
        self.reply(call_id, True, out)

    def _on_policy(self, _view, decision, kind):
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if not uri.startswith(UI_DIR.as_uri()):
                decision.ignore()
                return True
        elif kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False

    # ---- host -> page ----

    def run_js(self, js: str) -> None:
        if not self.alive:
            return
        if hasattr(self.view, "evaluate_javascript"):
            self.view.evaluate_javascript(js, -1, None, None, None, None, None)
        else:
            self.view.run_javascript(js, None, None, None)

    def reply(self, call_id, ok: bool, payload) -> None:
        if call_id is None:
            return
        self.run_js(f"window.__cn_reply({to_js(call_id)},{'true' if ok else 'false'},{to_js(payload)})")

    def emit(self, event: str, payload=None) -> None:
        self.run_js(f"window.__cn_emit({to_js(event)},{to_js(payload)})")

    def set_zoom(self, z: float) -> None:
        if abs(self.view.get_zoom_level() - z) > 0.001:
            self.view.set_zoom_level(z)

    def destroy(self) -> None:
        self.alive = False
