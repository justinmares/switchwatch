#!/usr/bin/env python3
"""
SwitchWatch — a lightweight macOS menubar focus coach.
Tracks app switches, shows focus stats, and offers gentle nudges.
"""

import rumps
import json
import csv
import threading
import time
import os
import subprocess
from datetime import datetime, timedelta
from collections import deque, defaultdict
from pathlib import Path

# ── pyobjc imports ────────────────────────────────────────────────────────────
from AppKit import (
    NSWorkspace,
    NSWorkspaceDidActivateApplicationNotification,
    NSUserNotification,
    NSUserNotificationCenter,
    NSObject,
)
from Foundation import NSNotificationCenter, NSDate

# ── Constants ─────────────────────────────────────────────────────────────────
LOG_DIR = Path.home() / ".switchwatch" / "logs"
CONFIG_PATH = Path.home() / ".switchwatch" / "config.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Notification cooldown — max 1 notification per 5 minutes
NOTIFICATION_COOLDOWN_SECS = 300

# Colors (ANSI not useful in menubar; we use emoji dots)
COLOR_GREEN  = "🟢"
COLOR_YELLOW = "🟡"
COLOR_RED    = "🔴"

FOCUS_DURATIONS = {"25 min": 25, "50 min": 50, "90 min": 90}

DEFAULT_CONFIG = {
    "watchlist": ["Slack", "Messages", "Superhuman", "Twitter", "Reddit"],
    "nudge_switches": 5,
    "nudge_window_secs": 180,
    "switch_window_secs": 1800,
    "green_threshold": 8,
    "yellow_threshold": 15,
}


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            # Merge any missing keys from defaults
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    save_config(DEFAULT_CONFIG.copy())
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Notification helper ───────────────────────────────────────────────────────

class Notifier:
    def __init__(self):
        self._last_sent: float = 0.0

    def send(self, title: str, subtitle: str, body: str, force: bool = False) -> bool:
        """Send a macOS notification, honouring the 5-minute cooldown."""
        now = time.time()
        if not force and (now - self._last_sent) < NOTIFICATION_COOLDOWN_SECS:
            return False
        self._last_sent = now
        # Use rumps.notification (wraps NSUserNotification)
        rumps.notification(
            title=title,
            subtitle=subtitle,
            message=body,
            sound=False,
        )
        return True

    def send_watchlist(self, app_name: str):
        """Watchlist alert — bypasses cooldown (different category)."""
        rumps.notification(
            title="⚠️  Focus Session Active",
            subtitle=f"You opened {app_name}",
            message=f"You're in a focus session. Do you really need {app_name} right now?",
            sound=False,
        )


# ── App Switch Observer (NSWorkspace notifications) ───────────────────────────

class AppObserver(NSObject):
    """Objective-C object that receives NSWorkspace notifications."""

    def initWithCallback_(self, callback):
        self = super().init()
        if self is None:
            return None
        self._callback = callback
        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        nc.addObserver_selector_name_object_(
            self,
            "appDidActivate:",
            NSWorkspaceDidActivateApplicationNotification,
            None,
        )
        return self

    def appDidActivate_(self, notification):
        info = notification.userInfo()
        app = info.get("NSWorkspaceApplicationKey")
        if app:
            name = app.localizedName() or "Unknown"
            self._callback(name)

    def dealloc(self):
        NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(self)
        super().dealloc()


# ── Core Tracker ─────────────────────────────────────────────────────────────

class SwitchTracker:
    """Holds all runtime state about app switches and focus sessions."""

    def __init__(self, config: dict, notifier: Notifier):
        self.config = config
        self.notifier = notifier
        self.lock = threading.Lock()

        # Ring buffers for time-windowed counting
        self._switch_times: deque = deque()   # timestamps of all switches (30-min window)
        self._nudge_times: deque = deque()    # timestamps for 3-min nudge window

        # Current app
        self.current_app: str = ""
        self.current_app_since: float = time.time()

        # Focus session state
        self.session_active: bool = False
        self.session_start: float = 0.0
        self.session_duration_min: int = 0
        self.session_tag: str = ""
        self.session_switches: int = 0
        self.session_app_time: defaultdict = defaultdict(float)  # app -> seconds
        self.session_streak_max: float = 0.0
        self.session_prev_app_time: float = 0.0

        # Daily log accumulator
        self._day_key: str = ""
        self._hourly_switches: defaultdict = defaultdict(int)  # "HH" -> count
        self._daily_app_time: defaultdict = defaultdict(float)
        self._sessions_today: list = []

        self._reset_day_if_needed()

    # ── Public API ─────────────────────────────────────────────────────────

    def record_switch(self, new_app: str):
        now = time.time()
        with self.lock:
            self._reset_day_if_needed()
            old_app = self.current_app
            elapsed = now - self.current_app_since

            # Accumulate time for old app
            if old_app:
                self._daily_app_time[old_app] += elapsed
                if self.session_active:
                    self.session_app_time[old_app] += elapsed
                    streak = elapsed
                    if streak > self.session_streak_max:
                        self.session_streak_max = streak

            # Update current app
            self.current_app = new_app
            self.current_app_since = now

            if not old_app or old_app == new_app:
                return  # first activation or same app re-activated

            # Record switch
            ts = now
            self._switch_times.append(ts)
            self._nudge_times.append(ts)
            hour_key = datetime.now().strftime("%H")
            self._hourly_switches[hour_key] += 1

            if self.session_active:
                self.session_switches += 1

            # Prune old entries
            cutoff_30 = now - self.config["switch_window_secs"]
            cutoff_3  = now - self.config["nudge_window_secs"]
            while self._switch_times and self._switch_times[0] < cutoff_30:
                self._switch_times.popleft()
            while self._nudge_times and self._nudge_times[0] < cutoff_3:
                self._nudge_times.popleft()

            # Check nudge threshold
            n3 = len(self._nudge_times)
            if n3 >= self.config["nudge_switches"]:
                self.notifier.send(
                    title="🧠 SwitchWatch",
                    subtitle="Focus check-in",
                    body=f"You've switched apps {n3} times in the last 3 minutes. "
                         f"What's the one thing you should be doing right now?",
                )

            # Check watchlist during focus session
            if self.session_active and new_app in self.config["watchlist"]:
                # Fire after 5-second delay so app has time to foreground
                threading.Timer(5.0, self.notifier.send_watchlist, args=(new_app,)).start()

    def switches_in_30min(self) -> int:
        now = time.time()
        cutoff = now - self.config["switch_window_secs"]
        return sum(1 for t in self._switch_times if t >= cutoff)

    def focus_streak_seconds(self) -> float:
        return time.time() - self.current_app_since

    def color_indicator(self) -> str:
        n = self.switches_in_30min()
        if n < self.config["green_threshold"]:
            return COLOR_GREEN
        if n < self.config["yellow_threshold"]:
            return COLOR_YELLOW
        return COLOR_RED

    def streak_label(self) -> str:
        secs = int(self.focus_streak_seconds())
        if secs < 60:
            return f"{secs}s"
        m, s = divmod(secs, 60)
        if m < 60:
            return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"

    # ── Focus Session ──────────────────────────────────────────────────────

    def start_session(self, duration_min: int, tag: str = ""):
        with self.lock:
            self.session_active = True
            self.session_start = time.time()
            self.session_duration_min = duration_min
            self.session_tag = tag
            self.session_switches = 0
            self.session_app_time = defaultdict(float)
            self.session_streak_max = 0.0
            self.session_prev_app_time = time.time()

    def end_session(self) -> dict:
        with self.lock:
            if not self.session_active:
                return {}
            now = time.time()
            # Credit time for current app
            elapsed = now - self.current_app_since
            if self.current_app:
                self.session_app_time[self.current_app] += elapsed
                if elapsed > self.session_streak_max:
                    self.session_streak_max = elapsed

            self.session_active = False
            duration_secs = now - self.session_start

            # Primary app
            primary = max(self.session_app_time, key=self.session_app_time.get) \
                      if self.session_app_time else self.current_app

            # Focus score: 100 at 0 switches, drops with frequency
            # ~1 switch/min = score 50; > 2/min = ~0
            switches_per_min = self.session_switches / max(duration_secs / 60, 1)
            score = max(0, min(100, int(100 - switches_per_min * 33)))

            summary = {
                "tag": self.session_tag,
                "duration_min": self.session_duration_min,
                "actual_duration_secs": int(duration_secs),
                "total_switches": self.session_switches,
                "longest_streak_secs": int(self.session_streak_max),
                "primary_app": primary,
                "focus_score": score,
                "app_time": {k: int(v) for k, v in self.session_app_time.items()},
                "started_at": datetime.fromtimestamp(self.session_start).isoformat(),
                "ended_at": datetime.now().isoformat(),
            }
            self._sessions_today.append(summary)
            self._flush_daily_log()
            return summary

    # ── Daily Log ──────────────────────────────────────────────────────────

    def _reset_day_if_needed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._day_key != today:
            if self._day_key:
                self._flush_daily_log()
            self._day_key = today
            self._hourly_switches = defaultdict(int)
            self._daily_app_time = defaultdict(float)
            self._sessions_today = []

    def _flush_daily_log(self):
        if not self._day_key:
            return
        top5 = sorted(self._daily_app_time.items(), key=lambda x: -x[1])[:5]
        record = {
            "date": self._day_key,
            "hourly_switches": dict(self._hourly_switches),
            "top5_apps_by_time": [
                {"app": a, "seconds": int(s)} for a, s in top5
            ],
            "focus_sessions": self._sessions_today,
        }
        log_path = LOG_DIR / f"{self._day_key}.json"
        with open(log_path, "w") as f:
            json.dump(record, f, indent=2)


# ── Main App (rumps) ──────────────────────────────────────────────────────────

class SwitchWatchApp(rumps.App):
    def __init__(self):
        super().__init__(name="SwitchWatch", title="🟢 0", quit_button=None)

        self.config = load_config()
        self.notifier = Notifier()
        self.tracker = SwitchTracker(self.config, self.notifier)

        # ── Menu items ────────────────────────────────────────────────────
        self._stat_item     = rumps.MenuItem("No data yet")
        self._streak_item   = rumps.MenuItem("Streak: —")
        self._session_item  = rumps.MenuItem("No active session")
        self._sep1          = rumps.separator
        self._start_25      = rumps.MenuItem("▶  Start 25-min session",  callback=self._start_25)
        self._start_50      = rumps.MenuItem("▶  Start 50-min session",  callback=self._start_50)
        self._start_90      = rumps.MenuItem("▶  Start 90-min session",  callback=self._start_90)
        self._end_session   = rumps.MenuItem("⏹  End session",           callback=self._end_session_cb)
        self._sep2          = rumps.separator
        self._watchlist_btn = rumps.MenuItem("⚙️  Edit watchlist…",       callback=self._edit_watchlist)
        self._log_btn       = rumps.MenuItem("📂  Open log folder",        callback=self._open_logs)
        self._today_btn     = rumps.MenuItem("📊  Today's summary",        callback=self._show_today)
        self._sep3          = rumps.separator
        self._quit_btn      = rumps.MenuItem("Quit SwitchWatch",           callback=self._quit)

        self.menu = [
            self._stat_item,
            self._streak_item,
            self._session_item,
            self._sep1,
            self._start_25,
            self._start_50,
            self._start_90,
            self._end_session,
            self._sep2,
            self._watchlist_btn,
            self._log_btn,
            self._today_btn,
            self._sep3,
            self._quit_btn,
        ]

        self._end_session.set_callback(None)  # disabled until session active
        self._end_session.title = "⏹  End session (no active session)"

        # ── NSWorkspace observer ──────────────────────────────────────────
        self._observer = AppObserver.alloc().initWithCallback_(self.tracker.record_switch)

        # ── Timer to refresh menubar every 5 seconds ──────────────────────
        self._refresh_timer = rumps.Timer(self._refresh_ui, 5)
        self._refresh_timer.start()

        # ── Session countdown timer ────────────────────────────────────────
        self._session_end_time: float = 0.0

    # ── UI refresh ────────────────────────────────────────────────────────

    def _refresh_ui(self, _=None):
        n = self.tracker.switches_in_30min()
        color = self.tracker.color_indicator()
        streak = self.tracker.streak_label()
        app = self.tracker.current_app or "—"

        # Menubar title
        self.title = f"{color} {n}"

        # Dropdown stats
        self._stat_item.title  = f"Switches (30 min): {n}   {color}"
        self._streak_item.title = f"Streak: {streak} in {app}"

        # Session countdown
        if self.tracker.session_active:
            remaining = max(0, self._session_end_time - time.time())
            rm, rs = divmod(int(remaining), 60)
            tag = f" [{self.tracker.session_tag}]" if self.tracker.session_tag else ""
            self._session_item.title = (
                f"🎯 Session: {rm}m{rs:02d}s left{tag}  |  "
                f"{self.tracker.session_switches} switches"
            )
            if remaining <= 0:
                self._auto_end_session()

    # ── Session control ───────────────────────────────────────────────────

    def _start_session(self, duration_min: int):
        if self.tracker.session_active:
            rumps.alert(
                title="Session already running",
                message="End the current session before starting a new one.",
                ok="OK",
            )
            return
        # Optional tag via input dialog
        tag_resp = rumps.Window(
            message="Tag this session (optional — e.g. 'deep work', 'email'):",
            title="Start Focus Session",
            default_text="",
            ok="Start",
            cancel="Cancel",
        ).run()
        if not tag_resp.clicked:
            return
        tag = tag_resp.text.strip()
        self.tracker.start_session(duration_min, tag=tag)
        self._session_end_time = time.time() + duration_min * 60
        self._end_session.title = "⏹  End session"
        self._end_session.set_callback(self._end_session_cb)
        rumps.notification(
            title="🎯 Focus session started",
            subtitle=f"{duration_min}-minute session{(' · ' + tag) if tag else ''}",
            message="Good luck. I'll stay out of your way.",
            sound=False,
        )

    def _start_25(self, _): self._start_session(25)
    def _start_50(self, _): self._start_session(50)
    def _start_90(self, _): self._start_session(90)

    def _end_session_cb(self, _):
        self._finish_session()

    def _auto_end_session(self):
        self._finish_session(auto=True)

    def _finish_session(self, auto: bool = False):
        summary = self.tracker.end_session()
        if not summary:
            return
        self._end_session.set_callback(None)
        self._end_session.title = "⏹  End session (no active session)"
        self._session_item.title = "No active session"

        longest_m, longest_s = divmod(summary["longest_streak_secs"], 60)
        actual_m = summary["actual_duration_secs"] // 60

        msg = (
            f"Duration: {actual_m} min\n"
            f"Total switches: {summary['total_switches']}\n"
            f"Longest streak: {longest_m}m{longest_s:02d}s\n"
            f"Primary app: {summary['primary_app']}\n"
            f"Focus score: {summary['focus_score']}/100"
        )
        if summary["tag"]:
            msg = f"Tag: {summary['tag']}\n" + msg

        title = "⏱ Session complete!" if auto else "⏹ Session ended"
        rumps.alert(title=title, message=msg, ok="Nice")

    # ── Utility actions ───────────────────────────────────────────────────

    def _edit_watchlist(self, _):
        current = ", ".join(self.config["watchlist"])
        resp = rumps.Window(
            message="Comma-separated app names to flag during focus sessions:",
            title="Edit Watchlist",
            default_text=current,
            ok="Save",
            cancel="Cancel",
        ).run()
        if resp.clicked and resp.text.strip():
            apps = [a.strip() for a in resp.text.split(",") if a.strip()]
            self.config["watchlist"] = apps
            save_config(self.config)

    def _open_logs(self, _):
        subprocess.Popen(["open", str(LOG_DIR)])

    def _show_today(self, _):
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = LOG_DIR / f"{today}.json"
        # Flush current data first
        self.tracker._flush_daily_log()
        if not log_path.exists():
            rumps.alert(title="Today's Summary", message="No data logged yet today.", ok="OK")
            return
        with open(log_path) as f:
            data = json.load(f)

        total_switches = sum(data["hourly_switches"].values())
        top5 = "\n".join(
            f"  {i+1}. {e['app']} ({e['seconds']//60}m)"
            for i, e in enumerate(data["top5_apps_by_time"])
        )
        sessions = len(data["focus_sessions"])
        avg_score = (
            sum(s["focus_score"] for s in data["focus_sessions"]) // sessions
            if sessions else 0
        )

        msg = (
            f"Total app switches: {total_switches}\n"
            f"Focus sessions: {sessions}"
            + (f" (avg score {avg_score}/100)" if sessions else "") + "\n\n"
            f"Top apps by time:\n{top5 if top5 else '  No data yet'}"
        )
        rumps.alert(title=f"📊 Today ({today})", message=msg, ok="OK")

    def _quit(self, _):
        self.tracker._flush_daily_log()
        rumps.quit_application()


# ── Entry point ───────────────────────────────────────────────────────────────

def check_accessibility():
    """
    Check if Accessibility is granted. On macOS 14+ AXIsProcessTrusted
    returns True/False without prompting; the app will prompt when
    NSWorkspace events arrive anyway. We just surface a helpful alert.
    """
    try:
        from ApplicationServices import AXIsProcessTrusted
        trusted = AXIsProcessTrusted()
        if not trusted:
            rumps.alert(
                title="SwitchWatch — Accessibility Access Needed",
                message=(
                    "SwitchWatch uses macOS Accessibility APIs to track which app is active.\n\n"
                    "Please open:\n"
                    "  System Settings → Privacy & Security → Accessibility\n\n"
                    "…and enable SwitchWatch (or the Terminal / Python process running it).\n\n"
                    "The app will work once access is granted — no restart needed."
                ),
                ok="Open System Settings",
            )
            subprocess.Popen(
                ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"]
            )
    except Exception:
        pass  # If we can't import, NSWorkspace notifications still work without Accessibility


if __name__ == "__main__":
    check_accessibility()
    SwitchWatchApp().run()
