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

# 2. 配置：编辑项目根目录 config.toml
#    至少填写 [email].domain（CF Email Routing 域名）、
#    [duckmail].address / password（转发目标 duckmail 账号密码）
#    其余项见文件内注释（含 [output] 产物类型等）

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

**cpa** 需要 `curl-cffi`（已在 `requirements.txt`）。启动时会预检依赖。

---

## 配置要点

跑起来前至少改这三项（`config.toml`）：

| 配置 | Key | 说明 |
|------|-----|------|
| 注册邮箱域名 | `[email].domain` | **Cloudflare Email Routing 上绑定/验证过的收信域名**（注册账号的 `@` 后部分会用它） |
| 收信邮箱 | `[duckmail].address` | 上述 CF 域名邮件转发到的 **duckmail 目标邮箱**（登录 duckmail 用） |
| 收信密码 | `[duckmail].password` | 该 duckmail 邮箱的密码 |

其余参数（并发、输出、浏览器、超时等）见 `config.toml` 内注释。

---

## 免责声明

本项目仅供学习与个人技术研究使用。使用本工具即表示你知悉并同意：

1. **遵守服务条款与法律**：你须自行确保使用方式符合 xAI / Grok、Cloudflare、邮箱服务商等平台的服务条款，以及你所在地区的法律法规；禁止用于批量滥用、绕过风控、侵权或其他违法用途。
2. **风险自负**：自动化注册、采集 Cookie / Token 等行为可能导致账号封禁、功能限制或法律责任，作者与贡献者不承担任何直接或间接责任。
3. **无担保**：软件按「现状」提供，不保证可用性、稳定性或对第三方接口变更的兼容；因接口调整导致的失败或损失自行承担。
4. **凭证安全**：生成的 SSO、access_token、refresh_token、密码等属于敏感信息，请妥善保管，勿提交到公开仓库或泄露给他人。
5. **非官方**：本项目与 xAI、Grok 官方无任何关联，亦非官方工具或授权产品。

若你不同意以上条款，请勿下载、使用或传播本项目。

---

## 致谢

本项目分享于 [LinuxDo 社区](https://linux.do)。
