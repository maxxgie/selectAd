# Signup Automation

Generates user records and submits registrations to the configured AdSelect endpoint.

## Configuration (current)
- Endpoint: `https://adselect.net/api/auth/register`
- Country: `United States` (full name)
- Sponsor: `ASD5437949`
- Source URL: `https://adselect.net/register?ref=ASD5437949`
- Emails: `@gmail.com`, US 10-digit phone numbers
- Safety: `--limit` and interactive `YES` confirmation required for `submit` unless `--yes`

Settings come from `config.json` and `.env` (env overrides). See `.env.example`.

## Setup
```bash
cd /home/njane/select/test_automation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage
```bash
.venv/bin/python3 main.py generate --count 1
.venv/bin/python3 main.py submit --limit 1        # type YES to confirm
.venv/bin/python3 main.py submit --limit 1 --yes  # non-interactive
.venv/bin/python3 main.py export --format json
```
Results: `output/signup_results.json` (includes preflight/public IP visibility and per-user responses).

## Network/rate limits
AdSelect may return `429 Too many registrations from this network`. The client retries transient errors and runs a preflight check. Automatic network rotation only happens if you configure it (it must actually change the public egress/IP AdSelect sees):
```json
"network_reset_command": "nmcli connection down '<name>' && nmcli connection up '<name>'",
"network_reset_wait_seconds": 15
```
Or `NETWORK_RESET_COMMAND` / `NETWORK_RESET_WAIT_SECONDS` in `.env`. Leave empty if not applicable. Use only with authorization and real/owned data.
