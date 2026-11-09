"""Pure (non-widget) helpers of the GTK dashboard."""

import os
import threading
import time
import unittest
from unittest import mock

from tenfold_tweak import core, gui
from tenfold_tweak.core import Result, TargetStatus


def st(target="desktop", available=True, current="/x.png", restorable=False, reason=""):
    return TargetStatus(target, available, reason, "m", current, restorable)


class CardActionsTest(unittest.TestCase):
    def test_enabled_rules(self):
        self.assertEqual(gui.card_actions(st(restorable=True), True, False),
                         {"choose": True, "apply": True, "restore": True})
        self.assertEqual(gui.card_actions(st(), False, False),
                         {"choose": True, "apply": False, "restore": False})

    def test_busy_or_unavailable_disables_all(self):
        for s, busy in ((st(restorable=True), True), (st(available=False, restorable=True), False)):
            self.assertFalse(any(gui.card_actions(s, True, busy).values()))


class StatusTextTest(unittest.TestCase):
    def test_badge(self):
        self.assertEqual(gui.status_badge(st(available=False)), ("Unavailable", "dim"))
        self.assertEqual(gui.status_badge(st(restorable=True)), ("Customized", "accent"))
        self.assertEqual(gui.status_badge(st()), ("Default", "dim"))

    def test_thumb_hint(self):
        self.assertEqual(gui.thumb_hint(st()), "")
        self.assertIn("No preview", gui.thumb_hint(st(current=None)))
        self.assertIn("Not available", gui.thumb_hint(st(available=False)))

    def test_apply_all_targets(self):
        rows = [st("desktop"), st("lock", available=False), st("login")]
        self.assertEqual(gui.apply_all_targets(rows), ["desktop", "login"])

    def test_env_subtitle(self):
        self.assertEqual(gui.env_subtitle("gnome", "gdm3"), "GNOME · GDM")
        self.assertEqual(gui.env_subtitle("other", "sddm"), "other · sddm")


class ResultTextTest(unittest.TestCase):
    def test_card_result(self):
        self.assertEqual(gui.card_result("apply", Result("lock", core.APPLIED, "blurred")),
                         ("Applied — blurred", "success"))
        self.assertEqual(gui.card_result("restore", Result("desktop", core.APPLIED)), ("Restored", "success"))
        self.assertEqual(gui.card_result("apply", Result("login", core.FAILED, "x"))[1], "error")
        self.assertEqual(gui.card_result("restore", Result("desktop", core.SKIPPED, "nothing to restore")),
                         ("Skipped — nothing to restore", "warning"))

    def test_summarize_success_hides_unselected(self):
        results = [Result("desktop", core.APPLIED), Result("lock", core.SKIPPED, "not selected"),
                   Result("login", core.APPLIED)]
        kind, text = gui.summarize("apply", results, [core.LOGIN_NOTE])
        self.assertEqual(kind, "success")
        self.assertEqual(text, f"Applied to Desktop, Login screen.\n{core.LOGIN_NOTE}")

    def test_summarize_kinds(self):
        failed = [Result("desktop", core.APPLIED), Result("login", core.FAILED, "boom")]
        self.assertEqual(gui.summarize("apply", failed)[0], "error")
        self.assertIn("Login screen: failed (boom)", gui.summarize("apply", failed)[1])
        declined = [Result("login", core.SKIPPED, "declined")]
        self.assertEqual(gui.summarize("apply", declined)[0], "warning")
        self.assertEqual(gui.summarize("restore", [Result("desktop", core.SKIPPED, "not selected")]),
                         ("warning", "Nothing was changed."))


class ConfirmTextTest(unittest.TestCase):
    def test_gdm_warning(self):
        heading, body = gui.confirm_text(core.GDM_WARNING)
        self.assertEqual(heading, "Change the login screen?")
        self.assertFalse(body.endswith("Continue?"))
        self.assertIn("dpkg-divert", body)

    def test_lock_alias_and_other(self):
        msg = "Lock note.\n\nThe lock target will set the desktop wallpaper. Continue?"
        self.assertEqual(gui.confirm_text(msg), ("Change the desktop wallpaper too?",
                                                 "Lock note.\n\nThe lock target will set the desktop wallpaper."))
        self.assertEqual(gui.confirm_text("Something")[0], "Continue?")

    def test_working_text(self):
        self.assertEqual(gui.working_text("restore", ["desktop"]), "Restoring Desktop…")
        self.assertIn("administrator", gui.working_text("apply", ["desktop", "login"]))


class CoverSizeTest(unittest.TestCase):
    def test_covers_box_without_upscaling(self):
        self.assertEqual(gui.cover_size(3840, 2160), (640, 360))
        self.assertEqual(gui.cover_size(1000, 1000), (640, 640))  # square covers a 16:9 box
        self.assertEqual(gui.cover_size(5000, 10), (5000, 10))    # would need upscaling: keep
        self.assertEqual(gui.cover_size(320, 200), (320, 200))
        self.assertEqual(gui.cover_size(0, 0), (640, 360))


class NoticeCommandTest(unittest.TestCase):
    def test_prefers_zenity_then_notify_send(self):
        cmd = gui.notice_command("no <b>display</b>", which=lambda n: "/usr/bin/" + n)
        self.assertEqual(cmd[:3], ["zenity", "--error", "--no-markup"])
        self.assertEqual(cmd[-1], "--text=no <b>display</b>")
        cmd = gui.notice_command("x", which=lambda n: n == "notify-send" and "/usr/bin/notify-send")
        self.assertEqual(cmd, ["notify-send", gui.TITLE, "x"])
        self.assertIsNone(gui.notice_command("x", which=lambda n: None))

    def test_main_reports_to_stderr_when_toolkit_missing(self):
        with mock.patch.object(gui, "build_app", return_value=(None, "GTK 4 missing")), \
                mock.patch.object(gui.sys, "stderr") as err, mock.patch.object(gui.subprocess, "run") as run:
            err.isatty.return_value = True
            self.assertEqual(gui.main(), 2)
        self.assertIn("GTK 4 missing", "".join(c.args[0] for c in err.write.call_args_list))
        run.assert_not_called()


class WorkerSafetyTest(unittest.TestCase):
    """Worker-thread paths must always call back, even when something raises."""

    @classmethod
    def setUpClass(cls):
        err = gui._load_toolkit(use_adw=False)
        if err:
            raise unittest.SkipTest(err)

    def pump(self, until, timeout=5.0):
        ctx = gui.GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not until() and time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.005)

    def test_confirm_returns_false_when_dialog_fails(self):
        stub = mock.Mock()
        stub._ask.side_effect = RuntimeError("no window")
        out = []
        t = threading.Thread(target=lambda: out.append(gui.Dashboard.confirm_blocking(stub, "msg")))
        t.start()
        self.pump(lambda: out)
        t.join(1)
        self.assertEqual(out, [False])

    def test_confirm_on_main_thread_cannot_ask(self):
        self.assertIsNone(gui.Dashboard.confirm_blocking(mock.Mock(), "msg"))

    def test_thumb_errors_call_done_with_none(self):
        path = os.path.abspath(__file__)  # exists, so the loader starts a worker
        for target in ("_decode_thumb", "_texture"):
            got = []
            with mock.patch.object(gui, "_decode_thumb", return_value=object()), \
                    mock.patch.object(gui, target, side_effect=RuntimeError("boom")):
                gui.ThumbLoader().load(path, got.append)
                self.pump(lambda: got)
            self.assertEqual(got, [None], target)


if __name__ == "__main__":
    unittest.main()
