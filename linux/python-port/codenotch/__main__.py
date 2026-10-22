from __future__ import annotations

import os
import sys

USAGE = "usage: codenotch [doctor | install-hooks | uninstall-hooks | autostart on|off]"


def _report(fn) -> int:
    try:
        print("OK: " + fn())
        return 0
    except Exception as e:
        print(f"FAILED: {e}")
        return 1


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd in ("-h", "--help"):
        print(USAGE)
        return 0
    if cmd == "doctor":
        from . import doctor
        return doctor.run()
    if cmd == "install-hooks":
        from . import hooks
        return _report(hooks.install)
    if cmd == "uninstall-hooks":
        from . import hooks
        return _report(hooks.uninstall)
    if cmd == "autostart":
        from . import autostart
        if len(args) > 1 and args[1] in ("on", "off"):
            return _report(autostart.enable if args[1] == "on" else autostart.disable)
        print(USAGE)
        return 2
    if cmd:
        print(USAGE)
        return 2
    # The notch is placed in absolute screen coordinates, which only X11 lets a client do.
    os.environ.setdefault("GDK_BACKEND", "x11")
    # WebKitGTK's DMA-BUF renderer paints blank windows on some NVIDIA and VM drivers.
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    from . import app
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
