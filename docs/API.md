# 接口与认证

网页根入口为 `https://example.com/`，业务 API 前缀保持 `/highscore/api`。完整的当前路由、请求字段、字段约束和返回结构见 [./openapi.json](./openapi.json)。该文件来自空临时库的离线 schema 导出，不包含部署数据，也不表示真实业务流程已验收。

生产 FastAPI 实例关闭 OpenAPI、Swagger 和 ReDoc 路由；本文件不会重新开放它们。不要把离线 schema 当作生产公开 API Key 服务。

## 用户会话

用户先通过同源网页注册、验证邮箱并登录。登录设置 `highscore_session` HttpOnly Cookie，路径为 `/highscore/`，生产使用 Secure 与 SameSite=Strict。浏览器以 Cookie 会话访问本人任务和鉴权下载，不提供默认账号、密码或对外 API Key。

`GET /highscore/api/auth/me` 返回当前用户的 `csrf_token`。需要写入的业务接口同时检查实时会话、同源 Origin 和 `X-CSRF-Token`；注册、登录等入口按各自源码规则校验 Origin。管理员写操作还会实时检查已验证管理员权限，不以页面曾显示过按钮作为授权。

任务提交的幂等字段、积分冻结/结算、取消和结果有效期由服务器处理。客户端不能决定价格或余额；错误响应和页面只显示业务提示，执行器诊断留在服务端。鉴权下载必须通过对应结果接口，不把 `./runtime/` 或 `./output/` 配置为静态目录。

## 执行器

执行器通过 `Authorization: Bearer <部署者项目内随机令牌>` 访问各自 worker 路由；令牌不返回到用户页面。现有数据库与注册验证邮件使用 `HIGHSCORE_WORKER_TOKEN` 令牌组，并用配置能力隔离任务。文献、提醒、abTEM、pymatgen 使用各自独立令牌，不能互换角色。

领取、心跳和完成回执必须保留服务端租约及原任务身份，不通过直接改数据库代替。数据库和验证邮件执行器保留 HTTPS 同主机校验；Compose 内文献与科学角色使用限定服务网络的 HTTP 地址，不作为公网入口。所有外部输入和返回包仍需服务器独立复验。

参数范围和科学模型限制另见 `./highscore_abtem/contract.py`、`./highscore_pymatgen/contract.py`；来源适配及报告限制见数据库源码。离线 schema 中的通用响应类型不能替代各结果格式的实际校验规则。
