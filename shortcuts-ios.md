# iOS Shortcuts

Prerequisite: Tailscale installed and connected on the iPhone (it can stay in the
background, it wakes the tunnel on demand). First check in Safari that
`http://<your-host>:8787/say` answers; if it does, the rest will work.

Each shortcut below uses a single action: **Get Contents of URL**. Expand "Show
More" to pick the method.

## 1. "Turn on the AC", one tap

| Field | Value |
|---|---|
| Action | Get Contents of URL |
| URL | `http://<your-host>:8787/on?temp=23&mode=COOL&fan=SILENT` |
| Method | `POST` |

Name it "Turn on the AC". Siri uses the shortcut name as the trigger phrase: "Hey
Siri, turn on the AC". Add it to the home screen from the share button.

The shortcut shows the JSON state on screen. To make it close silently, add a
`Do Not Show` action first, or just ignore it (iOS only flashes a banner).

## 2. "Turn off the AC"

Same, URL `http://<your-host>:8787/off`, method `POST`.

## 3. "The AC", single-button toggle

If you prefer one shortcut instead of two:

| Field | Value |
|---|---|
| URL | `http://<your-host>:8787/toggle?temp=23&mode=COOL` |
| Method | `POST` |

It reads the state, then flips it. `temp` and `mode` only apply when turning on.

## 4. "What's it like at home?", spoken answer

| Field | Value |
|---|---|
| URL | `http://<your-host>:8787/say` |
| Method | `GET` |

Add a second action, `Speak Text`, with the previous action's result. `/say`
returns plain text, not JSON, exactly for this:

> "The AC is on in cooling mode, set to 23 degrees. It is 22 inside, 26 outside."

## 5. Turn on when arriving home, location automation

In the Shortcuts app, `Automation` tab, "New Personal Automation", `Arrive`. Pick
your home, a wide radius (~500 m, so the AC has time to cool), and turn off "Ask
Before Running". Action: Get Contents of URL `/on?temp=23&mode=COOL`, `POST`.

The Tailscale tunnel must be up for this to work remotely. On the home WiFi it works
without it, the call goes over the LAN.

## If it does not answer

From the machine: `curl http://<your-host>:8787/say`.

- Nothing answers: `launchctl list | grep clim`, then `tail -f clim.log`
- The machine answers but not the iPhone: Tailscale disconnected on the phone
- `502` / timeout: the AC is unreachable on the LAN (power cut, IP changed). Reserve
  a static DHCP lease for the AC on your router to avoid IP drift.
