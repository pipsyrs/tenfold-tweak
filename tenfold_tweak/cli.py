"""Command-line front end."""

import argparse
import os
import sys

from . import core, detect

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tenfold-tweak",
        description="Change the desktop, lock screen and login screen background on Kali "
                    "(GNOME/GDM3, XFCE/LightDM). Runs the GUI when started with no arguments.")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("detect", help="show the detected environment and available targets")
    sub.add_parser("status", help="show the current image and restore state per target")
    sub.add_parser("gui", help="open the graphical dashboard (GTK 4)")

    def targets(sp):
        sp.add_argument("--desktop", action="store_true", help="desktop wallpaper")
        sp.add_argument("--lock", action="store_true", help="lock screen (own image with a GNOME lock-screen extension, "
                                                         "otherwise sets the desktop wallpaper)")
        sp.add_argument("--login", action="store_true", help="login screen (needs admin rights)")
        sp.add_argument("--all", action="store_true", help="all available targets")
        sp.add_argument("--yes", action="store_true", help="accept confirmation prompts")

    ap = sub.add_parser("apply", help="apply IMAGE to the selected targets")
    ap.add_argument("image", metavar="IMAGE", help="PNG or JPEG file")
    targets(ap)
    rp = sub.add_parser("restore", help="restore the original backgrounds")
    targets(rp)
    return p


def _tty_confirm(assume_yes: bool):
    def confirm(message: str):
        if assume_yes:
            return True
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        print(message)
        try:
            return input("[y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False
    return confirm


def cmd_detect() -> int:
    env = detect.detect()
    print(f"desktop-env: {env.de}")
    print(f"display-manager: {env.dm}")
    if env.lock_provider:
        print(f"lock-screen-extension: {env.lock_provider}")
    for t in core.TARGETS:
        reason = env.unavailable.get(t)
        print(f"target {t}: available" if reason is None else f"target {t}: unavailable ({reason})")
    return EXIT_OK


def cmd_status() -> int:
    for st in core.status():
        print(f"{st.target}: {'available' if st.available else 'unavailable (' + st.reason + ')'}")
        print(f"  method: {st.method or '-'}")
        print(f"  current: {st.current or '-'}")
        print(f"  restorable: {'yes' if st.restorable else 'no'}")
    return EXIT_OK


def cmd_run(args, parser) -> int:
    env = detect.detect()
    # Unavailable targets under --all are reported as skipped with their reason.
    targets = list(core.TARGETS) if args.all else [t for t in core.TARGETS if getattr(args, t)]
    if not targets:
        parser.error("select at least one of --desktop, --lock, --login or --all")
    image = getattr(args, "image", None)
    print(f"desktop-env: {env.de}")
    print(f"display-manager: {env.dm}")
    try:
        results = core.run(args.command, image, targets, _tty_confirm(args.yes), env)
    except core.ImageError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    for r in results:
        print(r.line())
    for note in core.notes(results):
        print(note)
    return EXIT_FAILED if any(r.status == core.FAILED for r in results) else EXIT_OK


def main(argv: list) -> int:
    parser = _parser()
    if not argv:
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            argv = ["gui"]
        else:
            parser.print_usage(sys.stderr)
            return EXIT_USAGE
    args = parser.parse_args(argv)
    if args.command == "detect":
        return cmd_detect()
    if args.command == "status":
        return cmd_status()
    if args.command == "gui":
        from . import gui
        return gui.main()
    if args.command in ("apply", "restore"):
        return cmd_run(args, parser)
    parser.print_usage(sys.stderr)
    return EXIT_USAGE
