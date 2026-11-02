# Set up PaperRoute on macOS

This is the setup procedure for both humans and coding agents. Work through it in order. Ask for the user's preferences as needed; keep secrets out of chat, commands, screenshots and the repository.

## 1. Check the Mac and try the demo

For scheduled use, put the clone in `~/Applications/PaperRoute` or another stable folder outside Desktop, Documents and Downloads. macOS privacy restrictions can prevent unattended access to those folders. Create the virtual environment after placing the clone; virtual environments contain absolute paths.

Use Python 3.11 or later. Check `python3 --version`; if it is missing or older, help the user install Python using their preferred installer. Create a virtual environment in this clone and install the pinned dependency:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python demo.py
.venv/bin/python -m unittest discover -s tests -v
```

## 2. Sign in locally

Use a dedicated PaperRoute config directory, normally `~/.config/paperroute`. Never reuse another tool's Telegram session.

The user gets their own API ID and API hash from [Telegram's API development page](https://my.telegram.org). These are application credentials; a bot token is not a substitute. The user must enter the following command in their own terminal because it prompts for secrets:

```sh
.venv/bin/python paperroute.py login
```

The prompts hide the API ID, API hash, phone number, login code and any two-step verification password. Credentials and the authenticated session stay in the config directory with private filesystem permissions. Agents must not ask the user to paste these values into chat or read their contents back.

To find the numeric IDs of channels the account can read, the user can run:

```sh
.venv/bin/python paperroute.py channels
```

That command lists the account's channels locally. Ask before running it on the user's behalf, and don't include its output in documentation or screenshots. Public channel usernames can also be used directly. PaperRoute does not join channels.

## 3. Configure your reading

Create `~/.config/paperroute/config.json` from `config.example.json`. Keep it outside the clone. Ask the user for:

- Each publication, its channel username or numeric ID, and the filename patterns identifying the edition they want. Patterns use `*` and `?` wildcards and match case-insensitively. Several spelling variants can belong to one publication. Use separate publications for supplements or editions that should coexist.
- An empty local reading folder and an empty, dedicated iCloud folder. Confirm iCloud Drive is enabled on the Mac and iPad and the iCloud parent directory already exists. `mirror_dir: null` enables local-only operation.
- Their preferred delivery time, in the Mac's local timezone.
- **Cleanup confirmation:** “Keep the newest matching issue of each publication and remove older PaperRoute downloads only after its replacement is safely downloaded?” Set `cleanup_confirmed: true` only after an explicit yes. Leaving it `false` keeps every downloaded issue. Explain that the same removals reach the mirror.

Give each publication a distinct lowercase folder ID such as `morning-paper` or `research-journal`. Example:

```json
{
  "id": "weekend-review",
  "channel": "@your_channel",
  "patterns": ["Weekend Review*.pdf", "Weekend_Review*.pdf"]
}
```

Use the full example file for the surrounding settings. The default `scan_limit` of 500 counts recent Telegram posts per publication. Increase it, up to 10000, when an infrequent publication is buried in a busy channel. Restrict patterns carefully: the newest matching post becomes the current issue, including when somebody reposts an older document.

```sh
.venv/bin/python paperroute.py validate
.venv/bin/python paperroute.py run
```

Before the first live run, confirm the intended account, configured channels, download folder and mirror destination with the user. A new destination must be empty. PaperRoute records which files it manages and never adopts an existing collection. On the iPad, open **Files → iCloud Drive → PaperRoute** (or the chosen folder) and confirm that a delivered PDF opens. Local mirror success alone doesn't establish that Apple's upload reached the iPad.

## 4. Enable daily delivery

Only after the successful manual run and the user's agreement to scheduling:

```sh
.venv/bin/python paperroute.py schedule --hour 7 --minute 0
```

Inspect the generated plist in the config directory. It must point to this clone's virtual-environment Python and the intended config file. Substitute the user's chosen time, then install it:

```sh
.venv/bin/python paperroute.py schedule --hour 7 --minute 0 --install
launchctl print gui/$(id -u)/com.paperroute.daily
```

This installs only `com.paperroute.daily`; it does not modify other download jobs. The Mac must be on and the account logged in for unattended delivery. macOS runs a missed calendar job after wake; it cannot run while powered off. Logs stay in the config directory as `daily.log` and `daily-error.log`. Check them locally after the first scheduled delivery; don't publish them.

To stop scheduled delivery:

```sh
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/com.paperroute.daily.plist"
rm "$HOME/Library/LaunchAgents/com.paperroute.daily.plist"
```

## 5. Confirm the handoff

Report the publication aliases, destinations, confirmed retention choice, schedule, and checks that passed. Don't report secrets or private channel identifiers. Leave the user with these commands:

```sh
.venv/bin/python paperroute.py run
.venv/bin/python paperroute.py validate
```

For another isolated configuration, put `--config /absolute/path/config.json` before the subcommand. It uses credentials and a session from that config's directory. Never point it at another application's credential folder.

## If a run fails

- **Login expired:** rerun `login` interactively. Scheduled runs never prompt for credentials.
- **No matching PDF:** check a recent filename and the scan limit. The old issue stays available.
- **Edited PDF:** move the annotated copy outside the reading and mirror folders. Rerun to restore the managed issue or complete cleanup.
- **Mirror unavailable:** resolve iCloud access or disk space and rerun. Local downloads remain intact.
- **Missing or invalid metadata:** stop the schedule and restore the folder's metadata from a backup. An established folder with missing tracking metadata stops rather than assuming its files are disposable. Don't remove `.state.json`, `.paperroute`, or `.mirror.json` to force a run; they define ownership and prevent accidental deletion.
- **Another run active:** wait for it to finish. A network run has a ten-minute timeout and bounded connection retries.

To remove PaperRoute, stop its schedule, keep any PDFs you want, then remove its dedicated config, reading, mirror and clone directories. Removing the iCloud folder also removes that copy from other devices.
