# Linux 部署

以下命令均从项目根目录执行。部署路径、数据、缓存、环境和秘密都留在本项目，不依赖既有站点、私有 wheel、旧镜像或宿主科学虚拟环境。需要 Docker Engine 与 Compose 插件；直接运行源码时另需 Python 3.12、Node.js 22 和 npm。

**本仓库不提供卡片数据库、派生索引或结构库，只提供程序及适配代码。数据库相关功能须由部署者自行准备数据并确认授权；克隆或构建源码不会取得卡片数据。** pymatgen 与 abTEM 支持上传本人有权使用的 CIF 独立运行，不依赖卡片数据库。

## 初始化与基本网站

先确定自己控制的 HTTPS 根域名，然后初始化：

```sh
sudo python3 ./scripts/init-public-runtime.py --site-url https://example.com/
sudo docker compose -f ./deploy/compose.yaml up -d --build web
```

将示例域名换成自己的域名。初始化生成 `./runtime/web.env`、每个 worker 的窄权限 env、数据库/验证邮件配置占位和空运行目录；不会创建用户、导入数据、开启数据库许可开关或输出秘密。再次执行时使用相同 `--site-url`，已有内容不会覆盖。修改域名或轮换令牌需显式、同步维护相关私有配置，初始化不会猜测或自动改绑。

Linux 私有目录为 0700、文件为 0600，容器使用 UID/GID 10001。配置文件不对宿主普通用户开放，Compose 命令使用 `sudo` 读取。Windows 可生成基础配置，迁至 Linux 后必须修正：

```sh
sudo python3 ./scripts/init-public-runtime.py --site-url https://example.com/ --fix-permissions
```

权限修正只作用于 `./runtime/`、`./runtime-cache/`、`./data/private-databases/` 及初始化的三个根配置文件；遇到符号链接即拒绝。运行中的共享计算锁不得删除或替换。

网站端口只绑定 `127.0.0.1:8787`。用部署者自己的 HTTPS 反向代理转发完整站点，不去掉路径：主页面 `/`，API `/highscore/api`，资源和兼容入口 `/highscore/`。`./deploy/nginx.location.example.conf` 只说明转发规则，证书和服务器配置由部署者提供。代理关闭 access log，或至少排除所有查询参数，避免记录验证令牌。Cookie 的 `/highscore/` 范围及浏览器同源策略保持。

`HIGHSCORE_PUBLIC_URL` 仍为 `https://example.com/highscore/`，用于现有 API/验证链接兼容；不意味着主页面从 `/` 搬走。`HIGHSCORE_TRUSTED_PROXY_IPS` 必须按部署者实际容器网络配置为明确代理 IP 列表，不能使用 `*`。生产保持 `HIGHSCORE_COOKIE_SECURE=true`。

默认数据库目录为空、数据库三个管理开关关闭、abTEM/pymatgen 禁用。健康端点成功只说明 HTTP 进程能响应，不说明任一可选业务已配置。`docker compose ... config` 会展开 env 值，只能使用 `config --quiet` 做语法检查，不能把完整输出附进 issue、日志或回执。

## 角色与挂载

| profile | 服务 | 自备配置/数据 | 允许挂载 |
| --- | --- | --- | --- |
| 默认 | `web` | `./runtime/web.env` | 用户队列和鉴权结果、资料库、匿名统计；无原库或 SMTP |
| `mail` | `mail-worker` | 自己的注册验证 SMTP | 验证邮件专用配置、密钥/密文、独立输出/缓存 |
| `database` | `database-worker` | 自备数据库及格式 profile | 原库只读、专用工作区；可选自己的 XRD SMTP |
| `literature` | `literature-worker` | 自己的合法文献账号 | 文献账号只读、专用工作区/缓存 |
| `literature-mail` | `literature-mail-worker` | 自己的提醒 SMTP | 提醒专用密钥/密文、独立输出/缓存；不挂全文 |
| `abtem` | `abtem-worker` | 独立随机令牌，网站显式启用 | 独立工作区/缓存及共享计算锁 |
| `pymatgen` | `pymatgen-worker` | 另一独立随机令牌，网站显式启用 | 独立工作区/缓存及同一计算锁 |

源码现有认证契约中，数据库与注册验证邮件沿用 `HIGHSCORE_WORKER_TOKEN` 这一令牌组，但二者的配置、SMTP、能力及输出仍隔离。文献、文献提醒、abTEM、pymatgen 四个角色各有独立随机令牌。只有 HTTP 服务读取所有令牌用于认证，不向浏览器返回它们。

Compose 不扩容科学 worker；两个科学角色共享服务器短事务 FIFO 及 `./runtime/science-lock/compute.lock`。每容器 640 MiB、0.5 CPU、不提供额外 swap，保持源码模型限制与计算预算；当前 abTEM 180 秒、pymatgen 120 秒，均不超过 300 秒。取消/失联要等进程组结束才释放，不能通过重建锁文件绕过排队。

## 注册邮件和管理员

注册需要部署者自己的 SMTP，当前发送器使用 SSL 并校验证书。先在项目内安装仅 HTTP 所需依赖：

```sh
python3 -m venv ./.venv-web
./.venv-web/bin/python -m pip install --cache-dir ./.cache/pip -r ./requirements-web.txt
sudo ./.venv-web/bin/python ./scripts/configure-public-mail.py --role verification
sudo python3 ./scripts/init-public-runtime.py --site-url https://example.com/ --fix-permissions
sudo docker compose -f ./deploy/compose.yaml --profile mail up -d --build web mail-worker
```

SMTP 的授权码在本机隐藏输入，保存到 `./runtime/verification-mail/runtime/mail.key` 和 `./runtime/verification-mail/runtime/mail.encrypted.json`。配置命令不联网、不发信；启用 worker 后才处理部署者网站的验证队列。邮件执行器使用现有 HTTPS 校验，需要域名和代理已经可达。

在真实网页正常注册自己的账号、收到验证邮件并验证后，才可在本机授予该邮箱管理员：

```sh
sudo docker compose -f ./deploy/compose.yaml exec web .venv/bin/python -B -m highscore_web admin --root . --email admin@example.com
```

把示例邮箱换为部署者本人已验证邮箱。命令拒绝不存在或未验证账号；没有默认密码，也不提供跳过验证的方法。系统管理中心提供用户、积分、公告和统计操作；保留实时权限、Origin/CSRF、版本与审计检查。

## 数据库与 XRD/结构

本仓库没有可供下载的卡片数据库、派生索引或结构库。部署者自行取得并核实数据许可后，把自己的数据放入 `./data/private-databases/`。根据原文件填写 `./database.local.json`；例子见 `./deploy/database.example.json`。只列真实存在且完成适配的来源，结构类型使用实际 ICSD 派生来源；不把目录同步当成接入成功。JADE/ICSD 派生格式的 profile 须包含部署者从合法原数据构建的真实来源与摘要绑定，不可套用任意占位摘要。

只读检查真实来源：

```sh
sudo python3 ./scripts/init-public-runtime.py --site-url https://example.com/ --fix-permissions
sudo docker compose -f ./deploy/compose.yaml --profile database run --rm --no-deps database-worker .venv/bin/python -B -m highscore_database --inspect
```

将检查输出的真实 `database_id` 填入 `./database-worker.local.json`；在 `./runtime/web.env` 配置 `HIGHSCORE_DATABASE_CATALOG`，每项只有真实 `id` 和公开来源名 `name`。自动集合使用实际集合 ID 填 `HIGHSCORE_DATABASE_AUTO_ID`；候选字段范围可按检查的真实来源、`reader_profile` 和执行器 ID 配置 `HIGHSCORE_DATABASE_SOURCE_TYPES`，字段结构见 `./docs/openapi.json` 对应接口及 `./highscore_web/app.py` 的配置校验。不要填写虚假的来源身份。

```sh
sudo docker compose -f ./deploy/compose.yaml --profile database run --rm --no-deps database-worker .venv/bin/python -B -m highscore_database --config database-worker.local.json --check-config
sudo docker compose -f ./deploy/compose.yaml --profile database up -d --build web database-worker
```

`--check-config` 不联网、不发送邮件。部署者在管理员界面按真实许可和测试结果确认 `license_confirmed`、`cloud_validated`、`public_enabled` 后，才能允许业务使用；不能把这些开关当作许可证明。用户只选内容类型与候选，不选库。新协议检索实际接入来源并完整分页，选中来源后再归档，成功每卡 1 分。

需要 XRD 邮件时，另外运行 `configure-public-mail.py --role database`，其密钥与密文只挂该数据库角色；不需要邮件时保留空目录，正常免费检索和无邮件归档不要求 SMTP。原始快照、报告和邮件投递状态独立保留，未知投递不得盲目重发。

## 文献与下载提醒

文献浏览器镜像自行从公开 Playwright 依赖安装 Chromium；不使用其他项目的账号或运行环境。先用已有配置命令在本机隐藏输入自己的合法账号：

```sh
sudo ./.venv-web/bin/python ./scripts/configure-literature-accounts.py
sudo python3 ./scripts/init-public-runtime.py --site-url https://example.com/ --fix-permissions
sudo docker compose -f ./deploy/compose.yaml --profile literature run --rm --no-deps literature-worker .venv/bin/python -B -m highscore_literature --check-config --root .
sudo docker compose -f ./deploy/compose.yaml --profile literature up -d --build web literature-worker
```

账号的密钥与密文在 `./runtime/literature-credentials/`，只挂载到文献执行器。在管理员界面按实际完成的配置启用文献业务。保留登录、验证码、精确论文身份与未知发布核查边界；第三方高速积分与网站每篇成功 1 分分别记录。

若需成功取文提醒，再配置独立 SMTP 并显式启用提醒 profile：

```sh
sudo ./.venv-web/bin/python ./scripts/configure-public-mail.py --role literature
sudo python3 ./scripts/init-public-runtime.py --site-url https://example.com/ --fix-permissions
sudo docker compose -f ./deploy/compose.yaml --profile literature-mail up -d --build web literature-mail-worker
```

提醒只含题名、期限和部署者网站根网址，不附全文。SMTP 接受状态独立记录，提交不确定时停止自动重发。关闭提醒角色不改变原文献任务和积分结算。

## 科学执行器

在 `./runtime/web.env` 中将需要的 `HIGHSCORE_ABTEM_ENABLED` 或 `HIGHSCORE_PYMATGEN_ENABLED` 改为 `true`，再构建相应 profile。令牌已由初始化分别生成，无需复制或打印。

```sh
sudo docker compose -f ./deploy/compose.yaml --profile abtem --profile pymatgen up -d --build web abtem-worker pymatgen-worker
```

HTTP、abTEM、pymatgen 使用各自镜像；科学依赖只安装在各自科学镜像，不绑定宿主虚拟环境。两个科学容器不挂载网站 SQLite、原库、文献账号或 SMTP。保留单个计算名额和原子积分结算，不用增加副本绕过资源拒绝。支持范围以源码 `./highscore_abtem/contract.py`、`./highscore_pymatgen/contract.py` 为准；CIF 结构限制、对称操作、占有率检查和数值复验都需要实际执行。

## 本地源码与验证

```sh
python3 ./scripts/setup-public.py --checks
python3 ./scripts/init-public-runtime.py --site-url https://example.com/ --source
python3 ./scripts/start-public.py --local-http
```

源码本机运行结果位于 `./output/`，容器把相应子目录映射至 `./runtime/`；二者不要误当作同一已有业务实例。`--source` 按当前本机用户准备私有目录，切换容器部署前须显式运行 `sudo ... --fix-permissions`。`--local-http` 只接受回环地址，不能用于公网生产。安装脚本另外创建 `./.venv-check/`，用于定向验证且不污染 HTTP 环境。

```powershell
pwsh -NoProfile -File ./scripts/verify.ps1 -Scope Repository
```

无 PowerShell 可用 `./.venv-check/bin/python ./scripts/verify-repository.py --root .`。验证首先检查四文件，再检查公开范围、定向业务测试和前端构建；CI 无 SMTP、原库或第三方账号，不执行真实外部投递。接口 schema 是离线生成文件，生产 API 文档路由仍关闭。

上线验收需要部署者授权的真实 HTTPS 浏览器，覆盖相关业务和鉴权下载、桌面与移动端，核对共享积分、部分失败返还、取消、过期及重复请求幂等。记录保存到 `./output/acceptance/`，凭据不得进入截图或日志；未验证项明确记录，不以健康端点替代。

## 备份与更新

保密备份运行数据、密钥及对应密文，保证用户队列/账本、文件和邮件回执一致。SQLite 备份使用安全停机或 SQLite 备份机制，不能只在写入期间复制主文件。更新镜像前核对数据库和报告版本兼容；不删除历史记录、不补扣旧任务、不自动重发未知邮件。不要把 `./runtime/`、`./data/`、`./output/` 或备份推送到公共仓库。
