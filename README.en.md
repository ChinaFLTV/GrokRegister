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
