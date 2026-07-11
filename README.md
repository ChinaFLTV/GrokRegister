# GrokRegister

[English](README.en.md)

批量注册 xAI / Grok 账号，采集 SSO，并可一键换成 CPA（cliproxyapi）可用的 OAuth JSON。

---

## 快速开始

```bash
# 1. 依赖
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置
cp config.example.toml config.toml
# 编辑 domain / duckmail / [output] 等

# 3. （推荐）预热 Profile，人手过一次 Cloudflare
python register.py --warm-profile

# 4. 注册
python register.py              # 使用 config.toml 中的 total / workers
```

---

## 输出模式

配置见 `[output]`（**默认 `cpa`**）：

```toml
[output]
# cpa | csv（默认 cpa）
type = "cpa"

# csv：追加写入
# csv_path = "accounts.csv"

# cpa：输出目录；留空则自动 {yyyyMMdd-HHmm}-{序号}/
path = ""
```

| type | 产物 | 说明 |
|------|------|------|
| `cpa`（默认） | `{path}/grok-{email}.json` | 扁平 `type=xai` OAuth，可给 cliproxyapi 用 |
| `csv` | `accounts.csv` | 列：邮箱、密码、SSO、姓、名 |

**cpa** 需要 `curl_cffi`（已在 `requirements.txt`）。启动时会预检依赖。

---

## 配置要点

跑起来前至少改这三项（`config.toml`）：

| 配置 | Key | 说明 |
|------|-----|------|
| 注册邮箱域名 | `[email].domain` | **Cloudflare Email Routing 上绑定/验证过的收信域名**（注册账号的 `@` 后部分会用它） |
| 收信邮箱 | `[duckmail].address` | 上述 CF 域名邮件转发到的 **duckmail 目标邮箱**（登录 duckmail 用） |
| 收信密码 | `[duckmail].password` | 该 duckmail 邮箱的密码 |

其余参数（并发、输出、浏览器、超时等）见配置文件内注释：`config.example.toml` / `config.toml`。
