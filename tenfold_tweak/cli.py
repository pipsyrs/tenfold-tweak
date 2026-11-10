"""Command-line front end."""

import argparse
import os
import sys

from . import cleaner, core, detect, system

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tenfold-tweaks",
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

    cp = sub.add_parser("clean", help="free disk space (not RAM): preview, then delete junk and cache files",
                        description=f"{cleaner.DISK_NOTE} Without category flags the default categories are used.")
    for c in cleaner.CATEGORIES:
        cp.add_argument("--" + c.id, action="store_true",
                        help=c.label + (" (default)" if c.default else "") + (" (admin)" if c.level != "user" else ""))
    cp.add_argument("--defaults", action="store_true", help="add the default categories to the selected ones")
    cp.add_argument("--yes", action="store_true", help="delete without asking")
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
    for path in system.saved_login_images():
        print(f"saved login image: {path}")
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


def cmd_clean(args) -> int:
    chosen = {c.id for c in cleaner.CATEGORIES if getattr(args, c.id.replace("-", "_"))}
    if args.defaults or not chosen:
        chosen |= set(cleaner.default_ids())
    ids = [c.id for c in cleaner.CATEGORIES if c.id in chosen]
    print(cleaner.DISK_NOTE)
    pv = cleaner.preview(ids)
    for est in pv.items:
        print(f"{est.id}: {cleaner.estimate_text(est)}" + (f" ({est.note})" if est.note else ""))
        for pkg in est.packages:
            print(f"  {pkg}")
    print(f"total: {'≥ ' if pv.total_unknown else ''}{cleaner.human(pv.total)}")
    if not args.yes:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("nothing deleted; re-run with --yes")
            return EXIT_OK
        questions = [cleaner.confirm_text(ids, pv)] + (["Trash: " + cleaner.TRASH_WARNING + " Continue?"]
                                                       if "trash" in ids else [])
        for question in questions:
            try:
                if input(f"{question} [y/N] ").strip().lower() not in ("y", "yes"):
                    print("nothing deleted")
                    return EXIT_OK
            except EOFError:
                print("nothing deleted")
                return EXIT_OK
    elif "trash" in ids:
        print(f"warning: {cleaner.TRASH_WARNING}")
    report = cleaner.clean(ids, pv)
    for r in report.items:
        print(r.line())
    print(f"total freed: {cleaner.human(report.freed)}")
    return EXIT_FAILED if any(r.status == "failed" for r in report.items) else EXIT_OK


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
    if args.command == "clean":
        return cmd_clean(args)
    parser.print_usage(sys.stderr)
    return EXIT_USAGE
