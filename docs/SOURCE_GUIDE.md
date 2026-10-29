# 通用源码与二次开发

本仓库介绍科研助手网站，并分享可用于学习、部署和改造科研流程的源码。在线入口见[首页](../README.md)。

## 可以参考和复用什么

| 模块 | 源码位置 | 实现内容 |
| --- | --- | --- |
| 中文科研网页 | [../web/src/](../web/src/) | Vue 输入交互、候选选择、任务进度及结果下载 |
| 网站 API | [../highscore_web/](../highscore_web/) | FastAPI 路由、权限、任务状态与结果接收 |
| 结构输入与校验 | [../highscore_crystal/](../highscore_crystal/) | CIF/POSCAR 解析、结构规范化、坐标设置及对称操作检查 |
| 材料工具 | [../highscore_pymatgen/](../highscore_pymatgen/) | 结构分析、转换、理论 XRD、超胞与结构匹配的执行和复验 |
| 电镜模拟 | [../highscore_abtem/](../highscore_abtem/) | abTEM 受限模型、参数和资源校验、SAED/HRTEM 生成及复验 |
| 科学任务调度 | [../highscore_science/](../highscore_science/) | 任务队列与共享计算锁，供受控单任务服务参考 |
| 卡片与结构适配 | [../highscore_database/](../highscore_database/) | 自备数据库只读适配、报告生成、原字节及多格式复验 |
| 文献获取适配 | [../highscore_literature/](../highscore_literature/) | 文献执行、身份核对与接续，需要合法账号及可用来源 |
| 部署与验证 | [../deploy/](../deploy/)、[../scripts/](../scripts/)、[../tests/](../tests/) | 初始化、独立角色容器、验证入口和合成测试样本 |

这些模块共享项目内的输入、任务及结果约定，不能默认每个目录都是独立 SDK。提取或改造时，同时阅读对应 contract、worker、测试与服务器复验逻辑。

## 两种使用方式

**部署科研工作台**：按[部署指南](../deploy/README.md)启动网站，再启用所需执行器。HTTP 与科学环境分开安装，未启用角色不代表业务可用。

**复用科研实现**：从所需模块的输入契约、核心计算与测试开始，明确格式、支持范围、依赖和输出校验，在自己的流程中保留参数和数值。

## 数据与环境

- 不提供卡片数据库、派生索引或结构库。相关模块只有适配代码，自行接入需合法数据、实际格式配置和完整复验。
- 结构处理和模拟可用自己有权使用的 CIF 独立工作，部分材料工具支持 POSCAR。
- 文献账号、SMTP、HTTPS 和运行配置由部署者准备，源码不含原站用户、全文、任务或密钥。
- 通用性体现在项目相对路径、明确输入输出和按角色配置，不表示任意数据源或结构均可运行。

## 开发与验证

安装与启动见[部署指南](../deploy/README.md)，认证见[API 说明](./API.md)，费用和期限见[业务规则](./SERVICE_RULES.md)。在项目根目录运行：

```powershell
pwsh -NoProfile -File ./scripts/verify.ps1 -Scope Repository
```

无 PowerShell 时使用项目内验证环境：

```sh
./.venv-check/bin/python ./scripts/verify-repository.py --root .
```

入口先检查 PROJECT_PLAN.md、AGENTS.md、README.md、TASKS.md，再检查公开范围、定向测试和前端构建，失败退出码向上传播。CI 记录源码和基础网站容器检查；可选执行器及新站真实业务须在目标环境验收。

参与方式见[贡献指南](../CONTRIBUTING.md)。原创代码 [MIT](../LICENSE)，第三方内容遵循[各自许可](../THIRD_PARTY_NOTICES.md)。
