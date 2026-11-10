"""Pure (non-widget) helpers of the GTK dashboard."""

import os
import threading
import time
import unittest
from unittest import mock

from tenfold_tweak import core, gui, system
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



class SystemTextTest(unittest.TestCase):
    def test_summary_points_to_apt_not_in_app_install(self):
        self.assertEqual(gui.system_summary(core.SetupState(True))[:2], ("Installed", "accent"))
        pill, _, text = gui.system_summary(core.SetupState(False, "helper missing"))
        self.assertEqual(pill, "Not installed")
        self.assertIn("tenfold-tweaks package", text)
        self.assertFalse(hasattr(gui, "INSTALL_HINT"))  # single source: system.INSTALL_HINT

    def test_skip_text_names_targets_and_hint(self):
        text = gui.setup_skip_text(["login"])
        self.assertTrue(text.startswith("Login screen: skipped"))
        self.assertIn(system.INSTALL_HINT, text)

    def test_app_id(self):
        self.assertEqual(gui.APP_ID, "io.github.tenfold.tenfold-tweaks")

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
                mock.patch.object(gui, "_detach_after_map", return_value=None), \
                mock.patch.object(gui.sys, "stderr") as err, mock.patch.object(gui.subprocess, "run") as run:
            err.isatty.return_value = True
            self.assertEqual(gui.main(), 2)
        self.assertIn("GTK 4 missing", "".join(c.args[0] for c in err.write.call_args_list))
        run.assert_not_called()


class DetachDecisionTest(unittest.TestCase):
    """The has-tty / should-detach decision (unit-testable without a display)."""

    def test_detaches_with_controlling_terminal(self):
        with mock.patch.object(gui.os, "open", return_value=7) as op, \
                mock.patch.object(gui.os, "close") as cl:
            self.assertTrue(gui._detach_requested())
        op.assert_called_once()
        cl.assert_called_once_with(7)

    def test_no_detach_without_controlling_terminal(self):
        with mock.patch.object(gui.os, "open", side_effect=OSError):
            self.assertFalse(gui._detach_requested())


class _Exit(Exception):
    """Stands in for os._exit so the parent branch can be observed."""


class DetachForkTest(unittest.TestCase):
    """_detach_after_map relaunch/pipe logic, with os.* and Popen mocked (no real spawn)."""

    def setUp(self):
        env = mock.patch.dict(gui.os.environ)
        env.start()
        self.addCleanup(env.stop)
        gui.os.environ.pop(gui._READY_ENV, None)  # we are the launching (parent) process

    def _parent(self, read=b"\x01", code=0, wait=None):
        calls = []
        child = mock.Mock()
        child.wait.return_value = code
        child.wait.side_effect = wait
        with mock.patch.object(gui, "_detach_requested", return_value=True), \
                mock.patch.object(gui.os, "pipe", return_value=(10, 11)), \
                mock.patch.object(gui.subprocess, "Popen", return_value=child) as po, \
                mock.patch.object(gui.os, "close", side_effect=lambda fd: calls.append(("close", fd))), \
                mock.patch.object(gui.os, "read", side_effect=lambda fd, n: calls.append(("read", fd))
                                  or (read() if callable(read) else read)), \
                mock.patch.object(gui.os, "_exit", side_effect=_Exit) as ex:
            with self.assertRaises(_Exit):
                gui._detach_after_map()
        return calls, child, ex, po

    def test_relaunch_in_new_session_with_ready_fd(self):
        _, _, _, po = self._parent()
        kw = po.call_args.kwargs
        self.assertEqual(po.call_args.args[0][0], gui.sys.executable)
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["pass_fds"], (11,))
        self.assertEqual(kw["env"][gui._READY_ENV], "11")
        self.assertNotIn("stderr", kw)  # child keeps the terminal for startup errors

    def test_parent_closes_write_end_before_blocking(self):
        calls, _, _, _ = self._parent()
        self.assertLess(calls.index(("close", 11)), calls.index(("read", 10)))

    def test_parent_exits_zero_once_window_mapped(self):
        _, child, ex, _ = self._parent(read=b"\x01")
        ex.assert_called_once_with(0)
        child.wait.assert_not_called()

    def test_parent_mirrors_child_exit_code_when_child_dies_first(self):
        _, child, ex, _ = self._parent(read=b"", code=2)
        child.wait.assert_called_once()
        ex.assert_called_once_with(2)

    def test_parent_uses_128_plus_signal_when_child_killed(self):
        _, _, ex, _ = self._parent(read=b"", code=-9)  # SIGKILL
        ex.assert_called_once_with(137)

    def test_parent_read_error_falls_back_to_wait(self):
        def boom():
            raise OSError
        _, child, ex, _ = self._parent(read=boom, code=0)
        child.wait.assert_called_once()
        ex.assert_called_once_with(0)

    def test_ctrl_c_before_map_terminates_child_and_exits_130(self):
        def interrupt():
            raise KeyboardInterrupt
        with mock.patch.object(gui.signal, "signal"):
            _, child, ex, _ = self._parent(read=interrupt)
        child.terminate.assert_called_once()
        child.wait.assert_called_once()
        ex.assert_called_once_with(130)

    def _fake_signal(self):
        handlers = {}

        def fake(s, h):
            old, handlers[s] = handlers.get(s, gui.signal.SIG_DFL), h
            return old
        patches = (mock.patch.object(gui.signal, "signal", side_effect=fake),
                   mock.patch.object(gui.signal, "getsignal", return_value=gui.signal.SIG_DFL))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return handlers

    def _signalled(self, signum, wait=None):
        handlers = self._fake_signal()
        _, child, ex, _ = self._parent(read=lambda: handlers[signum](signum, None), wait=wait)
        return child, ex, handlers

    def test_sighup_before_map_terminates_child_and_exits_129(self):
        child, ex, handlers = self._signalled(gui.signal.SIGHUP)
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)
        child.kill.assert_not_called()
        ex.assert_called_once_with(129)
        self.assertIs(handlers[gui.signal.SIGHUP], gui._swallow)  # no re-entry during cleanup

    def test_sigterm_before_map_terminates_child_and_exits_143(self):
        child, ex, _ = self._signalled(gui.signal.SIGTERM)
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)
        ex.assert_called_once_with(143)

    def test_child_ignoring_sigterm_gets_sigkill_after_timeout(self):
        child, ex, _ = self._signalled(
            gui.signal.SIGHUP, wait=[gui.subprocess.TimeoutExpired("gui", 5), -9])
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_args_list, [mock.call(timeout=5), mock.call()])
        ex.assert_called_once_with(129)

    def test_signal_handlers_restored_once_window_mapped(self):
        handlers = self._fake_signal()
        _, child, ex, _ = self._parent(read=b"\x01")
        ex.assert_called_once_with(0)
        child.terminate.assert_not_called()
        self.assertEqual(handlers, {gui.signal.SIGINT: gui.signal.SIG_DFL,
                                    gui.signal.SIGHUP: gui.signal.SIG_DFL,
                                    gui.signal.SIGTERM: gui.signal.SIG_DFL})

    def _signal_lands_on(self, when, signum):
        """Deliver signum (via its current handler) right after the signal.signal(*when) call."""
        handlers = self._fake_signal()
        fake, fired = gui.signal.signal.side_effect, []

        def wrapped(s, h):
            old = fake(s, h)
            if (s, h) == when and not fired and callable(handlers[signum]):
                fired.append(signum)  # a real signal is delivered once
                handlers[signum](signum, None)
            return old
        gui.signal.signal.side_effect = wrapped
        return handlers

    def test_second_signal_during_cleanup_does_not_escape(self):
        # systemd logout: SIGTERM right behind SIGHUP, landing while the abort ignores signals
        handlers = self._signal_lands_on((gui.signal.SIGINT, gui._swallow), gui.signal.SIGTERM)
        _, child, ex, _ = self._parent(read=lambda: handlers[gui.signal.SIGHUP](gui.signal.SIGHUP, None))
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)
        ex.assert_called_once_with(129)  # first signal wins
        for s in (gui.signal.SIGINT, gui.signal.SIGHUP, gui.signal.SIGTERM):
            self.assertIs(handlers[s], gui._swallow)

    def test_signal_after_abort_is_ignored(self):
        handlers = self._fake_signal()

        def hup_then_term():
            try:
                handlers[gui.signal.SIGHUP](gui.signal.SIGHUP, None)
            finally:  # TERM still pending once HUP's handler raised
                self.assertIs(handlers[gui.signal.SIGTERM], gui._swallow)
        _, child, ex, _ = self._parent(read=hup_then_term)
        child.terminate.assert_called_once()
        ex.assert_called_once_with(129)

    def test_signal_at_handler_install_still_terminates_child(self):
        self._signal_lands_on((gui.signal.SIGHUP, gui._abort_wait), gui.signal.SIGHUP)
        calls, child, ex, _ = self._parent()
        self.assertNotIn(("read", 10), calls)
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)
        ex.assert_called_once_with(129)

    def test_ignored_sighup_not_hooked(self):
        with mock.patch.object(gui.signal, "getsignal", return_value=gui.signal.SIG_IGN), \
                mock.patch.object(gui.signal, "signal") as sig:
            self._parent(read=b"\x01")
        sig.assert_not_called()  # nohup launch: SIGHUP keeps being ignored

    def test_no_tty_no_relaunch(self):
        with mock.patch.object(gui, "_detach_requested", return_value=False), \
                mock.patch.object(gui.subprocess, "Popen") as po:
            self.assertIsNone(gui._detach_after_map())
        po.assert_not_called()

    def test_spawn_failure_closes_pipe_and_stays_attached(self):
        with mock.patch.object(gui, "_detach_requested", return_value=True), \
                mock.patch.object(gui.os, "pipe", return_value=(10, 11)), \
                mock.patch.object(gui.subprocess, "Popen", side_effect=OSError), \
                mock.patch.object(gui.os, "close") as cl, \
                mock.patch.object(gui.os, "_exit") as ex:
            self.assertIsNone(gui._detach_after_map())
        ex.assert_not_called()
        self.assertEqual(sorted(c.args[0] for c in cl.call_args_list), [10, 11])

    def test_parent_pipe_on_stdio_fd_no_relaunch(self):
        with mock.patch.object(gui, "_detach_requested", return_value=True), \
                mock.patch.object(gui.os, "pipe", return_value=(0, 3)), \
                mock.patch.object(gui.subprocess, "Popen") as po, \
                mock.patch.object(gui.os, "close") as cl, \
                mock.patch.object(gui.os, "_exit") as ex:
            self.assertIsNone(gui._detach_after_map())
        po.assert_not_called()
        ex.assert_not_called()
        self.assertEqual(sorted(c.args[0] for c in cl.call_args_list), [0, 3])

    def _child(self, value, fstat=None):
        gui.os.environ[gui._READY_ENV] = value
        fstat = fstat or mock.Mock(return_value=mock.Mock(st_mode=gui.stat.S_IFIFO | 0o600))
        with mock.patch.object(gui.subprocess, "Popen") as po, \
                mock.patch.object(gui.os, "fstat", fstat), \
                mock.patch.object(gui.os, "set_inheritable") as inh, \
                mock.patch.object(gui.os, "write") as wr:
            finish = gui._detach_after_map()
        po.assert_not_called()  # never relaunches from the child side
        wr.assert_not_called()
        self.assertNotIn(gui._READY_ENV, gui.os.environ)
        return finish, inh

    def test_child_non_int_fd_runs_attached(self):
        finish, inh = self._child("abc")
        self.assertIsNone(finish)
        inh.assert_not_called()

    def test_child_stdio_fd_runs_attached(self):
        for value in ("2", "0", "-1"):
            finish, inh = self._child(value)
            self.assertIsNone(finish)
            inh.assert_not_called()

    def test_child_non_fifo_fd_runs_attached(self):
        regular = mock.Mock(return_value=mock.Mock(st_mode=gui.stat.S_IFREG | 0o600))
        finish, inh = self._child("11", fstat=regular)
        self.assertIsNone(finish)
        inh.assert_not_called()

    def test_child_closed_fd_runs_attached(self):
        finish, inh = self._child("11", fstat=mock.Mock(side_effect=OSError))
        self.assertIsNone(finish)
        inh.assert_not_called()

    def test_child_valid_fd_made_non_inheritable(self):
        finish, inh = self._child("11")
        self.assertIsNotNone(finish)
        inh.assert_called_once_with(11, False)

    def test_child_finish_redirects_and_signals(self):
        gui.os.environ[gui._READY_ENV] = "11"
        fifo = mock.Mock(st_mode=gui.stat.S_IFIFO | 0o600)
        with mock.patch.object(gui.subprocess, "Popen") as po, \
                mock.patch.object(gui.os, "fstat", return_value=fifo), \
                mock.patch.object(gui.os, "set_inheritable"), \
                mock.patch.object(gui.os, "close") as cl, \
                mock.patch.object(gui.os, "open", return_value=20), \
                mock.patch.object(gui.os, "dup2") as dup, \
                mock.patch.object(gui.os, "write") as wr:
            finish = gui._detach_after_map()
            po.assert_not_called()  # the relaunched child never relaunches again
            self.assertNotIn(gui._READY_ENV, gui.os.environ)  # not leaked to helpers
            wr.assert_not_called()  # nothing signalled before the window maps
            finish()
        self.assertEqual([c.args for c in dup.call_args_list], [(20, 0), (20, 1), (20, 2)])
        wr.assert_called_once_with(11, b"\x01")
        cl.assert_any_call(11)


class FirstMapTest(unittest.TestCase):
    def test_callback_runs_only_on_first_map(self):
        window, cb = mock.Mock(), mock.Mock()
        gui._on_first_map(window, cb)
        handler = window.connect.call_args.args[1]
        self.assertEqual(window.connect.call_args.args[0], "map")
        handler(window)
        handler(window)  # hidden and re-shown
        cb.assert_called_once()


class MainDetachWiringTest(unittest.TestCase):
    def test_detaches_before_toolkit_and_startup_failure_exits_2(self):
        order, fin = [], mock.Mock()
        with mock.patch.object(gui, "build_app", side_effect=lambda: order.append("build") or (None, "boom")), \
                mock.patch.object(gui, "_report_error") as rep, \
                mock.patch.object(gui, "_detach_after_map", side_effect=lambda: order.append("detach") or fin):
            self.assertEqual(gui.main(), 2)
        self.assertEqual(order, ["detach", "build"])  # relaunch before any GLib thread exists
        rep.assert_called_once_with("boom")
        fin.assert_not_called()  # parent is not told "ready": it mirrors exit 2

    def test_child_gets_map_hook_before_run(self):
        app = mock.Mock(state={})
        app.run.side_effect = lambda argv: self.assertIn("on_mapped", app.state) or 0
        fin = mock.Mock()
        with mock.patch.object(gui, "build_app", return_value=(app, None)), \
                mock.patch.object(gui, "_detach_after_map", return_value=fin):
            self.assertEqual(gui.main(), 0)
        self.assertIs(app.state["on_mapped"], fin)

    def test_no_tty_runs_unchanged(self):
        app = mock.Mock(state={})
        app.run.return_value = 0
        with mock.patch.object(gui, "build_app", return_value=(app, None)), \
                mock.patch.object(gui, "_detach_after_map", return_value=None):
            self.assertEqual(gui.main(), 0)
        self.assertNotIn("on_mapped", app.state)


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
