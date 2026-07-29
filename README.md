# RIT Esports Intake Bot

A face-lift of an older project I made for RIT Esports. This bot turns Google Form submissions into private Discord ticket channels, pings the team's managers, and archives the conversation when the ticket is closed. This helps build a bridge between the Administrative board and the rest of the server through tickets, a higher visibility and user friendly experience over simply direct messaging users.

This bot's only use is for the RIT Esports Discord server.
 
## Setup

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env
python main.py
```

`.env.example` documents every setting. The four you cannot skip are `TOKEN`,
`INTAKE_FORM_URL`, `INTAKE_SHEET_ID` and `INTAKE_TICKET_CATEGORY_ID`; the bot
tells you which are missing and exits rather than half-starting.

### Discord Setup

In the [Developer Portal](https://discord.com/developers/applications):

- Under **Bot > Privileged Gateway Intents**, enable **Server Members Intent**.
  The bot needs it to grant individual members access to their ticket channel.
- Invite the bot with the **Manage Channels**, **Manage Roles**, **View Channels**,
  **Send Messages**, **Embed Links**, **Attach Files** and **Read Message History**
  permissions.
- The bot's own role must sit *above* any role it grants channel access to.

### Google Side

The recommended setup is a service account:

1. Create one in the Google Cloud console and download its JSON key.
2. Save it as `credentials.json` next to `main.py`, or point
   `GOOGLE_SERVICE_ACCOUNT_FILE` at it.
3. Share the response sheet with the service account's email address as a Viewer.

There is a fallback that reads the sheet through its public CSV export, enabled
with `INTAKE_ALLOW_PUBLIC_CSV=1`. It requires the sheet to be shared with
"anyone with the link", which means **anyone who learns the sheet ID can read
every submission**. Use the service account unless you have a reason not to.

### First run

On the first run the bot marks every response already in the sheet as handled,
so it does not open several hundred tickets at once. Only submissions from that
point on become tickets. If you genuinely want the backlog processed, set
`INTAKE_PROCESS_BACKLOG=1` before the first start.

## Commands

All four are manager-only (the ops or competitive role, or Manage Server).

| Command | What it does |
| --- | --- |
| `/post_intake [channel] [guidelines]` | Post the guidelines and the form button |
| `/intake_status` | Sync health, counts, recent errors, config warnings |
| `/intake_sync` | Check the sheet immediately instead of waiting |
| `/close_ticket [reason]` | Close and archive the ticket you are in |

`/intake_status` is the first thing to run when something looks wrong.

## Running It

`python main.py` in a terminal stops the moment that window closes, the user
logs out, or the machine reboots. Nothing announces that it stopped — members
keep submitting the form and nobody gets a ticket. So it needs to run as a
service that starts on boot and restarts on failure.

**Where it runs matters more than how.** A personal desktop or laptop is the
wrong host: it sleeps, it reboots for updates, and it goes home in a backpack.
A cheap VPS or a Raspberry Pi left in the club space is the right answer.

### After setting it up

Restart the host and confirm the bot comes back on its own. A service that was
never tested against a reboot is not a service. Then run `/intake_status` in
Discord to confirm the sync job is actually running.

## Layout

```
main.py              starting the bot, logging, slash commands
tests/               run with `python -m pytest`
deploy/              systemd unit for running it as a service
intake/
  config.py          every setting, read from the environment
  permissions.py     who counts as a manager
  sheets.py          reading responses out of the Google Sheet
  embeds.py          building the messages Discord shows
  tickets.py         opening, routing and closing ticket channels
  views.py           the buttons on those messages
  storage.py         the small JSON files that survive a restart
  sync.py            the background job tying it all together
```

To follow what happens when someone submits the form, start at
`sync.run_once` in [intake/sync.py](intake/sync.py).


## Things worth knowing

**Discord allows 50 channels per category.** When the main ticket category
fills up, the bot uses `INTAKE_OVERFLOW_CATEGORY_IDS` in order. If they are all
full it stops opening tickets and says so in `/intake_status`, so either keep
closing tickets or add an overflow category.

**A closed ticket is only deleted once its transcript is saved.** If
`INTAKE_TRANSCRIPT_CHANNEL_ID` is unset, or posting the transcript fails, the
channel is locked and kept instead. Nothing is ever deleted without a copy.

**State lives in `state/`.** `intake_state.json` remembers which responses have
already become tickets. Deleting it makes the bot re-check the whole sheet and
re-post recent tickets, so keep it with the bot. It is fingerprint-based, so
deleting or reordering rows in the sheet is safe.

**Team names are matched loosely.** `config.COMP_TEAM_ALIASES` lists the
shorthand people actually type ("cs2", "val", "smash"). Adding a team means
adding it there and setting its `COMP_TEAM_ROLE_*` / `COMP_TEAM_USERS_*` env
vars. Teams with no role or users configured are listed by `/intake_status`.

**Logs go to `logs/intake.log`** and rotate at 2 MB. Set `LOG_LEVEL=DEBUG` for
more detail.
