# aternos-status: Check whether Aternos (Minecraft Java) servers are online.

Shows: online/offline/starting state, player count, and ping.
Watch mode sends a desktop notification (notify-send) when a server comes online.

Usage:
-  `aternos-status.py myserver.aternos.me`                 : one-shot check
-  `aternos-status.py -w myserver.aternos.me`             : live view + notifications
-  `aternos-status.py -w --until-online myserver.aternos.me`   : notify once, then exit
-  `aternos-status.py -w a.aternos.me b.aternos.me:12345`  : several servers

Player joins/leaves while a server is online trigger *silent* notifications
(use --no-player-alerts to turn them off).

Phone notifications (via ntfy - install the ntfy app and subscribe to your topic):
-  `aternos-status.py -w --ntfy my-secret-topic myserver.aternos.me`
-  `aternos-status.py --ntfy my-secret-topic --test-notify`   : send a test and exit

With no server arguments, addresses are read (one per line) from
`~/.config/aternos-status/servers.txt`

## Requirements
- Python 3 or anything newer
- Only the Python standard library is required. Optional: dnspython, to follow
SRV records (needed for Aternos addresses that don't use port 25565).

## Installation (Linux)
1. Install the script
```
mkdir -p ~/.local/bin ~/.config/systemd/user
install -m755 aternos-status.py ~/.local/bin/aternos-status
```
2. (OPTIONAL) Create the service file at `~/.config/systemd/user/aternos-status.service`:
```
[Unit]
Description=Aternos server status watcher

[Service]
Environment=NTFY_TOPIC=your-topic-name    # set it to your topic name
ExecStart=%h/.local/bin/aternos-status -w yourserver.aternos.me    # set your server you want to have it autostart
Restart=on-failure
RestartSec=10
StandardOutput=null

[Install]
WantedBy=default.target
```
3. Enable and start it
```
systemctl --user daemon-reload
systemctl --user enable --now aternos-status.service
```
## For Other OSes
Make sure that Python3 is installed
```
python3 aternos-status.py
```
This only gives you the functionality of the status only. No Notifications, ntfy, autostart.
