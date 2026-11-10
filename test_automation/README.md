<<<<<<< ours
# Signup Automation

Generates user records and submits registrations to the configured AdSelect endpoint.

## Configuration (current)

Settings come from `config.json` and `.env` (env overrides). See `.env.example`.

### Person / sponsor config (`config/person.json`)

Editable file used to target a different person/account. Edit `sponsor_id`, `api_url`, and `source_url` as needed:

```json
{
  "person": "default",
  "sponsor_id": "ASD5437949",
  "api_url": "https://adselect.net/api/auth/register",
  "source_url": "https://adselect.net/register?ref=ASD5437949",
  "country": "United States",
  "default_password": "12345678"
}
```

Values in `config/person.json` override `config.json`. Environment variables (`SPONSOR`, `SIGNUP_API_URL`, `SOURCE_URL`, `COUNTRY`, `DEFAULT_PASSWORD`) take highest precedence.

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

### Auto-delete on success

After a successful submission (HTTP 2xx), the user is automatically removed from the SQLite database (`output/test_users.db`). This means re-running `submit` will not re-submit already-registered users — only remaining users in the DB are processed.

### VPN / server IP change detection

`preflight` detects when your public IP changes (e.g. after switching VPN servers) and logs:
```
VPN/server IP changed: <old_ip> -> <new_ip>
```
The last-seen IP is persisted to `output/.vpn_state.json`. When the IP changes, rate-limit behavior resets for the new network.

### Batch rate limiting

Configure `batch_size` and `batch_delay_seconds` in `config.json` to pause after every N submissions (useful to avoid rate limits):

```json
"batch_size": 10,
"batch_delay_seconds": 20.0
```

This waits 20 seconds after every 10 submissions.

## Network/rate limits

AdSelect may return `429 Too many registrations from this network`. The client retries transient errors (429, 500, 502, 503, 504, 522, 524) with exponential backoff and runs a preflight check. Automatic network rotation only happens if you configure it (it must actually change the public egress/IP AdSelect sees):

```json
"network_reset_command": "nmcli connection down '<name>' && nmcli connection up '<name>'",
"network_reset_wait_seconds": 15
```

Or `NETWORK_RESET_COMMAND` / `NETWORK_RESET_WAIT_SECONDS` in `.env`. Leave empty if not using VPN rotation. Use only with authorization and real/owned data.
=======
# Signup Automation

Generates user records and submits registrations to the configured AdSelect endpoint.

## Configuration (current)

Settings come from `config.json` and `.env` (env overrides). See `.env.example`.

### Person / sponsor config (`config/person.json`)

Editable file used to target a different person/account. Edit `sponsor_id`, `api_url`, and `source_url` as needed:

```json
{
  "person": "default",
  "sponsor_id": "ASD5437949",
  "api_url": "https://adselect.net/api/auth/register",
  "source_url": "https://adselect.net/register?ref=ASD5437949",
  "country": "United States",
  "default_password": "12345678"
}
```

Values in `config/person.json` override `config.json`. Environment variables (`SPONSOR`, `SIGNUP_API_URL`, `SOURCE_URL`, `COUNTRY`, `DEFAULT_PASSWORD`) take highest precedence.

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

### Auto-delete on success

After a successful submission (HTTP 2xx), the user is automatically removed from the SQLite database (`output/test_users.db`). This means re-running `submit` will not re-submit already-registered users — only remaining users in the DB are processed.

### VPN / server IP change detection

`preflight` detects when your public IP changes (e.g. after switching VPN servers) and logs:
```
VPN/server IP changed: <old_ip> -> <new_ip>
```
The last-seen IP is persisted to `output/.vpn_state.json`. When the IP changes, rate-limit behavior resets for the new network.

### Batch rate limiting

Configure `batch_size` and `batch_delay_seconds` in `config.json` to pause after every N submissions (useful to avoid rate limits):

```json
"batch_size": 10,
"batch_delay_seconds": 20.0
```

This waits 20 seconds after every 10 submissions.

## Network/rate limits

AdSelect may return `429 Too many registrations from this network`. The client retries transient errors (429, 500, 502, 503, 504, 522, 524) with exponential backoff and runs a preflight check. Automatic network rotation only happens if you configure it (it must actually change the public egress/IP AdSelect sees):

```json
"network_reset_command": "nmcli connection down '<name>' && nmcli connection up '<name>'",
"network_reset_wait_seconds": 15
```

Or `NETWORK_RESET_COMMAND` / `NETWORK_RESET_WAIT_SECONDS` in `.env`. Leave empty if not using VPN rotation. Use only with authorization and real/owned data.
>>>>>>> theirs
