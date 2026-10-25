# Monitor Remote

Control your monitor from your phone instead of the joystick on the back. Switch inputs, change picture modes and adjust settings like brightness, from any phone browser on your Wi-Fi.

Your Mac sends the commands down the display cable using DDC/CI, the control standard most monitors support. A small server on the Mac gives your phone one page of buttons and sliders. There's no app to install on the phone.

- **Inputs:** one tap to switch between your Mac, consoles and other sources. Most monitors can't say which input they're showing, so the page outlines the last one the remote switched to, with the time.
- **Picture modes:** one tap per mode, if your monitor exposes them.
- **Sliders:** brightness, contrast and any other setting your monitor accepts. They update live as you drag.
- **Confirmed changes:** the page shows a tick only once the monitor reports the new value back.
- **Only on when it matters:** the server listens only while your own monitor is connected, so on any other network nothing is open.

## What you need

- An Apple Silicon Mac (M1 or later). Intel Macs aren't supported.
- Apple's command line tools: `xcode-select --install`
- A monitor with DDC/CI turned on. It's usually on by default, and some monitors have a switch for it in their menu.
- Your phone on the same Wi-Fi as the Mac.

The Mac has to be awake and cabled to the monitor, because that cable is how the commands get there. Your Mac can stay connected while the monitor shows a console.

## Install

```sh
git clone https://github.com/Dr-Stone27/monitor-remote.git ~/monitor-remote
cd ~/monitor-remote
cp profiles/generic.json config.json
./install.sh
```

Use a profile for your monitor if there is one in `profiles/`; otherwise start from `generic.json`. Keep the folder outside Documents, Desktop and Downloads, because macOS doesn't let background items run from there.

`install.sh` builds the small `ddc` tool and asks which monitor the remote should serve. It then sets the remote to start at login and prints your phone link, which is also copied to your clipboard. Open the link in Safari on your phone, then tap Share → Add to Home Screen. If macOS asks whether Python may accept incoming connections, choose Allow.

After you edit `config.json`, run `./install.sh` again.

## Customising

`config.json` holds the codes your monitor uses and the labels you see on the phone. Labels are free text, so "PS5" or "Work laptop" are fine.

```json
{
  "name": "My monitor",
  "inputs": {
    "vcp": "0x60", "method": "standard",
    "options": [{"label": "PS5", "port": "HDMI 1", "value": "0x11"}]
  },
  "presets": {"vcp": "0x15", "options": [{"label": "Game", "value": 45}]},
  "sliders": [
    {"id": "brightness", "label": "Brightness", "vcp": "0x10", "min": 0, "max": 100, "step": 1}
  ]
}
```

- **`inputs.method`:** `standard` for most monitors. Use `lg` for LG monitors, which mostly ignore the standard input command.
- **`presets`:** optional. Set it to `null` if your monitor's picture modes aren't controllable.
- **`sliders[].write`:** set to `step` when a setting reads back as its menu value but has to be written as the step number. LG's Black Stabilizer reads 0–100 but is written 0–20.
- **`sliders[].locked_in_presets`:** picture modes in which that setting can't change. The slider greys out in those modes.

`python3 tools.py check` tells you whether the file is valid.

## Mapping your monitor

Manufacturers rarely publish their codes, so you find them by changing a setting with the monitor's own buttons and seeing which code moves.

1. Pause the remote so it doesn't talk to the monitor while you work: `python3 tools.py pause`
2. Learn which codes change on their own. Run `python3 tools.py scan quiet1`, then `scan quiet2`, then `diff quiet1 quiet2`. Ignore any code listed. On the LG profiled here, 0xAF ticks over by itself.
3. Run `python3 tools.py scan before`. Change one setting with the monitor's buttons and close its menu. Then run `scan after` and `diff before after`. The code that moved is your setting.
4. Test writing it with `./ddc set <code> <value>`, then read it back with `./ddc get <code>`.
   - **If nothing changes,** try the step number instead of the value. A setting that moves in 5s, for example, may want 10 for 50.
   - **If it still doesn't change,** try `./ddc set-alt <code> <value>`.
   - Some monitors ignore commands while their own menu is open.
5. For picture modes and inputs, run `python3 tools.py watch <code>` and step through the options with the monitor's buttons. Each one prints its value.
6. Add what you found to `config.json`, run `./install.sh`, and you're back up.

Monitors often report their current input wrongly or not at all, so input switches show as "Sent" rather than with a tick. The page remembers the last input set from the remote instead. If a scan does find a code that follows the input on your monitor, please say so in an issue.

If you map a monitor, please share it as a profile in `profiles/`. Note which values you confirmed on screen.

## Commands

| Command | What it does |
|---|---|
| `python3 tools.py link` | Show the phone link and copy it |
| `python3 tools.py new-link` | Issue a new link; the old one stops working |
| `python3 tools.py enroll` | Choose which monitor the remote serves |
| `python3 tools.py pause` / `resume` | Stop or start the remote |
| `python3 tools.py scan` / `diff` / `watch` | Mapping helpers, as above |
| `./uninstall.sh` | Stop the remote and remove it from login |

## Security

The remote was tested against the obvious attacks before release. Here is how it holds up:

- **Without the link:** nothing works. Every request without the link's random 128-bit token is refused.
- **Away from home:** the port is only open while your own monitor is connected. It recognises the monitor by a hash of its maker, model and serial number.
- **Malicious websites:** a page you visit can't use the remote, even if it knew the link. The server rejects cross-site requests and DNS-rebinding tricks, and the page can't be embedded elsewhere.
- **Floods and oversized requests:** size limits, timeouts, per-device connection caps and a write rate limit stop the server or the Mac being overwhelmed.
- **Logs:** they skip routine polls and never record the token.

**One known limit:** it uses plain HTTP on your home network. Someone already on your Wi-Fi who can read other people's traffic could pick up the link. At worst they could change your monitor's input or settings, and `python3 tools.py new-link` shuts them out.

## Troubleshooting

- **The page loads but nothing changes:** check DDC/CI is on in the monitor's menu, then map the setting as above. Your monitor may want different codes or the `lg` method.
- **The link doesn't open:** the phone must be on the same Wi-Fi, the Mac awake, and the enrolled monitor connected. `server.out` in the folder shows "monitor connected, listening" when it's ready.
- **Taps feel slow:** reinstall with `./install.sh`. The login item has to run at normal priority, because background priority stretches the short pauses DDC needs.

## Credits

Talking to monitors on Apple Silicon uses the same approach as [m1ddc](https://github.com/waydabber/m1ddc) and [MonitorControl](https://github.com/MonitorControl/MonitorControl). Not affiliated with LG or any monitor maker.

## Licence

MIT. See `LICENSE`.
