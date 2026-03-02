# SwitchWatch

A lightweight macOS menubar focus coach. Tracks app switches, nudges you when you're context-switching too much, and logs your focus sessions — all locally, no network calls.

## Quick Start

```bash
cd ~/projects/switchwatch
bash run.sh
```

On first launch you'll be prompted to grant Accessibility access so SwitchWatch can observe active-window changes:

> **System Settings → Privacy & Security → Accessibility → enable Terminal** (or the Python process)

No restart needed — once granted the menubar icon appears and tracking begins.

---

## Menubar Display

```
🟢 3
```

| Indicator | Meaning |
|-----------|---------|
| 🟢        | < 8 switches in the last 30 min |
| 🟡        | 8–15 switches |
| 🔴        | > 15 switches |

The number is your switch count for the current 30-minute rolling window.

Click the icon to see:
- Switches in 30 min + color
- Current focus streak (time in the same app)
- Active session countdown + switch count

---

## Focus Sessions

Click the menubar icon and choose:

| Option | Duration |
|--------|----------|
| ▶ Start 25-min session | Pomodoro |
| ▶ Start 50-min session | Deep work block |
| ▶ Start 90-min session | Flow state |

You'll be asked to optionally tag the session (`deep work`, `email processing`, `calls`, etc.).

At the end (or when you click **End session**) you'll see a summary:

```
Tag: deep work
Duration: 25 min
Total switches: 4
Longest streak: 18m42s
Primary app: Cursor
Focus score: 89/100
```

**Focus score formula:** starts at 100, subtracts ~33 points per switch per minute. A focused 25-min session with 2 switches scores ~91.

---

## Notifications

| Trigger | Message |
|---------|---------|
| ≥5 switches in 3 min | "You've switched apps X times in the last 3 minutes. What's the one thing you should be doing right now?" |
| Watchlist app opened during focus session (5s delay) | "You're in a focus session. Do you really need [App] right now?" |

Max 1 nudge notification per 5 minutes to prevent fatigue. Watchlist alerts fire independently.

---

## Watchlist Apps

Default: `Slack, Messages, Superhuman, Twitter, Reddit`

Edit via **⚙️ Edit watchlist…** in the menu, or directly in `~/.switchwatch/config.json`:

```json
{
  "watchlist": ["Slack", "Messages", "Superhuman"],
  "nudge_switches": 5,
  "nudge_window_secs": 180,
  "switch_window_secs": 1800,
  "green_threshold": 8,
  "yellow_threshold": 15
}
```

---

## Daily Logs

Written to `~/.switchwatch/logs/YYYY-MM-DD.json` at the end of each session and when you quit.

```json
{
  "date": "2026-02-26",
  "hourly_switches": { "09": 12, "10": 8, "11": 3 },
  "top5_apps_by_time": [
    { "app": "Cursor", "seconds": 4820 },
    { "app": "Safari", "seconds": 1200 }
  ],
  "focus_sessions": [
    {
      "tag": "deep work",
      "duration_min": 25,
      "actual_duration_secs": 1512,
      "total_switches": 4,
      "longest_streak_secs": 1122,
      "primary_app": "Cursor",
      "focus_score": 89,
      "started_at": "2026-02-26T09:04:11",
      "ended_at": "2026-02-26T09:29:23"
    }
  ]
}
```

Click **📂 Open log folder** to open `~/.switchwatch/logs/` in Finder, or **📊 Today's summary** for a quick in-app view.

---

## Run on Login (optional)

Create a launchd plist to auto-start:

```bash
cat > ~/Library/LaunchAgents/com.switchwatch.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.switchwatch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOUR_USERNAME/projects/switchwatch/switchwatch.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.switchwatch.plist
```

Replace `YOUR_USERNAME` with your actual username.

---

## Dependencies

- Python 3.10+
- [`rumps`](https://github.com/jaredks/rumps) — menubar framework
- [`pyobjc-framework-Cocoa`](https://pyobjc.readthedocs.io/) — NSWorkspace notifications
- [`pyobjc-framework-ApplicationServices`](https://pyobjc.readthedocs.io/) — AXIsProcessTrusted check

Install: `pip3 install rumps pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices`
