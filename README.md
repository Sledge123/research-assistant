# 科研助手

科研助手提供中文科研工作台：科研资料库、文献获取、XRD/结构卡片检索及归档、SAED＋HRTEM 模拟、pymatgen 材料工具，以及管理中心与公告。服务使用 Python 源码运行，前端使用 Vue。

**本仓库不提供卡片数据库、派生索引或结构库；只提供程序及适配代码，部署者须自行准备数据并确认授权。克隆源码不会获得任何卡片数据。** pymatgen 与 abTEM 可通过上传本人有权使用的 CIF 独立计算，无需卡片数据库；材料工具也支持其范围内的 POSCAR 输入。

本仓库是独立的公开源码发行版，不包含站点用户、积分、任务、全文、归档、调试账号、数据库文件、授权材料或运行密钥。部署者需要自行提供合法可用的数据、第三方账号、SMTP 和 HTTPS 域名。MIT 许可仅覆盖本项目原创代码，第三方边界见 [./THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。

## 可以做什么

| 功能 | 用户提供什么 | 具体作用 | 可以得到什么 |
| --- | --- | --- | --- |
| 文献获取 | 论文题名 | 由独立执行器识别并核对论文身份，复用仍有效且已验证的正文，或按受控流程发起获取；区分正文与补充材料 | 在本人任务中下载已核验的全文 PDF，查看进度、失败原因及下载期限；配置提醒角色后向已验证邮箱提醒下载 |
| XRD 卡片检索与归档 | 自备并授权接入的卡片数据库；卡片编号或精确化学式，选择衍射内容并勾选候选 | 仓库仅提供适配代码。在部署者实际接入的来源中免费查候选、分页核对真实来源；选中后提取完整可读取字段、峰表和关联原始记录，复验报告后归档 | RTF/TXT 可读报告、CSV/JSON 数值与字段、原始记录快照及下载包；便于查物相、核对峰位和保留可追溯依据 |
| 晶体结构检索与 CIF | 自备并授权接入的结构库；结构编号或精确化学式，选择结构内容并勾选候选 | 仓库不提供结构库或派生索引。查询实际接入的 ICSD 结构来源，按原始坐标设置、占有率及显式对称操作转换并复验 | 结构报告、原子坐标 CSV、JSON/原始记录和 CIF；可供结构查看或后续材料计算，不伪造实验峰表 |
| 结构分析 | 一个 CIF 或含明确元素的 POSCAR，以及容差参数 | 检查并分析晶胞、组成、原子位置和受支持的对称性信息 | 中文报告及结构 JSON/CSV/CIF/POSCAR，便于核对输入结构与计算条件 |
| CIF/POSCAR 转换 | 一个 CIF 或 POSCAR | 从同一完整结构导出两种格式，再核对元素、晶胞和周期坐标，保留原输入 | 可下载的 CIF、POSCAR、位点 CSV 和结构 JSON，供不同计算软件继续使用 |
| XRD衍射峰生成 | 一个结构、波长和 2θ 范围 | 用 pymatgen 根据结构计算理论粉末衍射峰，核对峰位、强度、晶面及数值限制 | 峰表 JSON/CSV、PNG/SVG 科学图及参数报告，用于对照结构与实验趋势；结果属于理论计算 |
| 小超胞生成 | 一个结构和三个整数扩胞倍数 | 生成规模限制内的超胞，并检查周期坐标、元素数目和晶格关系 | 扩胞后 CIF/POSCAR、位点 CSV、结构 JSON 和中文报告，供后续建模使用 |
| 两结构比较 | 两个 CIF/POSCAR 结构及比较容差 | 在固定匹配策略下比较晶格、结构和元素对应关系，避免只按文件名判断相同 | 比较结论、参数和两份结构明细，便于判断不同来源或格式的结构是否匹配 |
| SAED＋HRTEM 模拟 | 上传 CIF，或选择本人已生成并复验的结构 CIF；设置加速电压、晶带轴、厚度、离焦等参数 | abTEM 在受限模型中计算选区电子衍射及高分辨透射图像，两项结果全部成功并复验后才完成任务 | PNG/TIFF 科学图、NPY 原始数值、CSV/JSON、CIF 和参数记录；支持下载后自行分析，不替代实验观测 |
| 科研资料库 | 管理员上传文件、标题和分类，发布时填写许可/权属说明；已验证用户选择资料 | 按分类维护、发布、下架及查找科研资料，下载前核对文件摘要 | 已发布资料的鉴权附件下载；不执行上传的 HTML，不提供在线预览，不扣积分 |
| 管理中心与公告 | 实时已验证管理员，填写积分调整原因或公告内容 | 查看用户、以追加账本方式调整积分，发布纯文本公告并保留版本和操作审计 | 受权限控制的管理记录和已发布公告，普通账号不能执行管理写操作 |

原始数据库、外部账号及邮件服务都由部署者自备。首次部署只启动网站，不会凭空获得文献来源、卡片库或科学计算能力；所需角色的启用条件见 [./deploy/README.md](./deploy/README.md)。

## 积分与期限

| 功能 | 成功费用 | 配置条件 |
| --- | --- | --- |
| 科研资料库 | 免费 | 已验证账号浏览；管理员维护许可说明 |
| XRD/结构检索、候选分页 | 免费 | 自备且通过适配核验的数据库 |
| XRD/结构卡片归档 | 每张 1 积分 | 服务端复验成功才结算 |
| 文献获取 | 每篇 1 积分 | 自备有权使用的第三方账号 |
| SAED＋HRTEM | 两项全部成功合计 1 积分 | 独立 abTEM 执行器 |
| XRD衍射峰生成 | 每次 2 积分 | 独立 pymatgen 执行器 |
| 其余 pymatgen 工具 | 每次 1 积分 | 结构分析、格式转换、超胞及结构比较 |

任务提交时由服务器原子冻结共用积分，成功复验后结算，失败、取消或超时按原冻结额返还。历史免费任务不补扣。文献与科学结果默认从成功起保留 3 天，有效期内鉴权重复下载免费；具体下载期限以接口返回为准。检索、选卡和重复下载不另收费。

## 本地源码启动

需要 Python 3.12、Node.js 22 和 npm；依赖环境及缓存均写入本项目。

```powershell
python ./scripts/setup-public.py --checks
python ./scripts/init-public-runtime.py --source
python ./scripts/start-public.py --local-http
```

本地访问 `http://127.0.0.1:8787/`。`--source` 保留本机用户的运行目录权限，`--local-http` 仅允许回环地址。对外部署使用 HTTPS 与安全 Cookie；Linux 容器初始化使用 `sudo python3 ./scripts/init-public-runtime.py`，以准备 UID/GID 10001 所需的权限，并使用 `sudo docker compose` 读取 0600 的私有配置。

初始化只创建缺失的配置、随机令牌和空目录，重复执行不会覆盖现有令牌或数据，也不会创建默认管理员。Windows 生成的配置在 Linux 部署前须再次运行 `sudo python3 ./scripts/init-public-runtime.py --fix-permissions`。

## Linux 容器部署

完整步骤见 [./deploy/README.md](./deploy/README.md)。默认仅启动网站：

```sh
sudo python3 ./scripts/init-public-runtime.py --site-url https://example.com/
sudo docker compose -f ./deploy/compose.yaml up -d --build web
```

镜像从源码与公开依赖构建；前端按 [./web/package-lock.json](./web/package-lock.json) 执行 `npm ci`。HTTP 镜像不安装 abTEM、pymatgen 或浏览器运行环境。可选 profile 为 `database`、`mail`、`literature`、`abtem`、`pymatgen`、`literature-mail`；未配置的执行器不代表可用服务。

主页面位于根路径 `/`，API 保持 `/highscore/api`，静态资源和兼容路由保持 `/highscore/`。反向代理需要保留全部路径，且不得记录邮箱验证链接的查询参数。默认不对公网开放 OpenAPI、Swagger 或 ReDoc；参数说明见 [./docs/API.md](./docs/API.md)。

## 配置与数据边界

- 运行配置在 `./runtime/*.env`，第三方文献账号在 `./runtime/literature-credentials/`；邮箱密钥与密文按角色隔离。不要提交这些目录或复制他人的运行配置。
- 卡片数据库、派生索引及结构库均不提供。数据由部署者自行合法取得并放入 `./data/private-databases/`，只读挂载到数据库角色；网页、邮件和科学角色不读取原库。PDF-2、JADE/PDF-4+、ICSD 等数据及格式相关许可不随本仓库提供。
- 新库必须经来源、SHA256、原记录和多格式复验后启用；数据库开放开关不构成许可确认。未知、加密或未适配格式会被拒绝。
- abTEM 与 pymatgen 使用独立镜像、令牌、工作目录和缓存，只共享 `./runtime/science-lock/compute.lock`。服务器短事务 FIFO 与物理锁共同限制为一个计算任务；每个科学容器保留 640 MiB、0.5 CPU。计算预算不超过 300 秒，当前源码分别限制 abTEM 180 秒、pymatgen 120 秒。
- 科学输入仅支持源码规定的有序满占有结构，CIF 设置和显式对称操作不能猜测或静默改写。模拟数值和生成报告不能称为实验测量或 HighScore 原生附件。

## 验证

```powershell
pwsh -NoProfile -File ./scripts/verify.ps1 -Scope Repository
```

无 PowerShell 时：

```sh
./.venv-check/bin/python ./scripts/verify-repository.py --root .
```

验证入口首先检查根目录四文件存在、为文件、可读且非空，再执行公开范围检查和针对性的源码、前端验证，失败退出码向上传播。四文件存在不证明内容准确。本仓库的 CI 仅验证公开源码，不等于任何部署的真实业务验收；上线前需用部署者授权的账号实际完成受影响流程、桌面与移动端显示和鉴权下载，并将记录保存在 `./output/acceptance/`。

当前发行工作范围和待验证项见 [./PROJECT_PLAN.md](./PROJECT_PLAN.md) 与 [./TASKS.md](./TASKS.md)。
