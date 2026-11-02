# PaperRoute agent instructions

For a setup request, read and follow [SETUP.md](SETUP.md) in order. Gather the user's publication rules and preferences, explicitly confirm cleanup, verify a manual delivery, then request scheduling consent. Ask for secrets only through the program's local hidden terminal prompts. Do not put credentials, phone numbers, channel IDs, sessions, real PDFs, logs or personal paths in this repository or chat output.

For development, keep the single-script structure and use the synthetic demo and unittest suite. Preserve atomic downloads, file ownership records, checksum checks, path validation and failure behavior. Test deletion and mirroring only in isolated temporary directories. Never run against a user's real reading folder or install a schedule as part of tests.

Run `python3 demo.py` and `python3 -m unittest discover -s tests -v`. Live tests require explicit account, channel and download-scope authorization. Publishing or changing repository visibility requires user authorization; never infer it from a setup request.
