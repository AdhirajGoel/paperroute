# PaperRoute

**Your reading, delivered to your iPad.**

PaperRoute runs on your Mac, collects the newspapers, magazines and journals you choose from Telegram, and copies them to iCloud Drive for reading in Files on your iPad. Once you enable cleanup, it keeps each title's latest issue until its replacement has downloaded successfully.

It grew out of a daily routine: opening Telegram channels, finding newspapers, magazines and journals, downloading them, and clearing yesterday's files. The channel and filename rules are yours to choose.

## Try it without an account

With Python 3.11 or later:

```sh
python3 demo.py
```

The offline demo delivers synthetic newspapers, magazines and journals, replaces yesterday's newspaper, and keeps the monthly reading. It uses temporary folders and needs no Telegram account.

![Second synthetic delivery: the new newspaper arrives, its previous issue is removed, the magazine and journal stay up to date, and the local mirror contains three PDFs.](docs/assets/demo.png)

*An excerpt from the second offline delivery. [Read the full transcript](docs/assets/demo.txt). The demo uses a local mirror; setup verifies iCloud delivery separately.*

## Set it up with your agent

Give Codex or Claude Code this repository and say:

> Set up PaperRoute on my Mac. Follow SETUP.md, ask me for my publications and preferences, and verify a delivery before enabling the daily schedule.

You'll need your own Telegram account, access to the chosen channels, and iCloud Drive enabled on your Mac and iPad. Your agent follows the [setup procedure](SETUP.md); you enter login secrets in your own terminal and confirm your reading list, cleanup choice and delivery time.

## What happens each day

PaperRoute matches your publication rules, downloads new issues into separate folders, and mirrors them to iCloud. Confirmed cleanup removes older PaperRoute issues after their replacements arrive. If there's no new issue, the existing one stays. Files unrelated to PaperRoute are left alone.

See [publication matching](SETUP.md#3-configure-your-reading) and [delivery and recovery details](SETUP.md#how-delivery-is-checked) when configuring your reading list.

## About this release

I chose the workflow, publication filters, and reading experience, and used coding agents to implement and refine the automation. This is a static MIT-licensed release, with no ongoing maintenance planned.

For tests and code changes, see the [developer instructions](AGENTS.md). Telegram access uses [Telethon](https://github.com/LonamiWebs/Telethon), also MIT licensed.
