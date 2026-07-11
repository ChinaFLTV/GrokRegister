# GrokRegister

[中文](README.md)

Batch-register xAI/Grok accounts, capture SSO cookies, and optionally convert them into CPA-ready OAuth JSON (for cliproxyapi).

---

## Quick Start

```bash
# 1. Dependencies
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Config: edit config.toml in the project root
#    At minimum set [email].domain (Cloudflare Email Routing domain),
#    [duckmail].address / password (forward target duckmail credentials)
#    See in-file comments for the rest (including [output] product type)

# 3. (Recommended) Warm the browser profile and pass Cloudflare once by hand
python register.py --warm-profile

# 4. Register
python register.py              # uses total / workers from config.toml
```

---

## Output Modes

Configure under `[output]` (**default: `cpa`**):

```toml
[output]
# cpa | csv (default: cpa)
type = "cpa"

# csv: append mode
# csv_path = "accounts.csv"

# cpa: output directory; empty → auto {yyyyMMdd-HHmm}-{seq}/
path = ""
```

| type | Artifact | Notes |
|------|----------|--------|
| `cpa` (default) | `{path}/grok-{email}.json` | Flat `type=xai` OAuth for cliproxyapi |
| `csv` | `accounts.csv` | Columns: email, password, SSO, last name, first name |

**cpa** requires `curl-cffi` (listed in `requirements.txt`). Dependencies are checked at startup.

---

## Config Essentials

Before running, set at least these three in `config.toml`:

| Setting | Key | Notes |
|---------|-----|--------|
| Signup email domain | `[email].domain` | **Your Cloudflare Email Routing domain** (verified/bound in CF; used as the part after `@` for generated accounts) |
| Inbox email | `[duckmail].address` | The **duckmail mailbox** that receives mail forwarded from that CF domain |
| Inbox password | `[duckmail].password` | Password for that duckmail mailbox |

For everything else (concurrency, output, browser, timeouts, etc.), see comments in `config.toml`.

---

## Disclaimer

This project is for learning and personal technical research only. By using it, you acknowledge and agree that:

1. **Comply with terms and law.** You are solely responsible for ensuring your use complies with the terms of service of xAI/Grok, Cloudflare, email providers, and all applicable laws. Do not use it for bulk abuse, evasion of security controls, infringement, or any illegal purpose.
2. **Use at your own risk.** Automated signup and capturing cookies/tokens may result in account bans, limited access, or legal consequences. Authors and contributors accept no liability for any direct or indirect damages.
3. **No warranty.** The software is provided “as is,” without guarantees of availability, stability, or compatibility with third-party APIs. Failures or losses due to API changes are your responsibility.
4. **Protect credentials.** Generated SSO cookies, access tokens, refresh tokens, and passwords are sensitive. Store them securely; do not commit them to public repositories or share them.
5. **Unofficial.** This project is not affiliated with, endorsed by, or an official product of xAI or Grok.

If you do not agree, do not download, use, or distribute this project.
