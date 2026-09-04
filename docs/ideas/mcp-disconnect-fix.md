# MCP client disconnect 被记为 500 的修复设计

状态：待评审，仅设计，不提交。

## 问题边界

生产中的 `POST /mcp/` 500 集中来自单个重试客户端，同时其他 20 至 28 个客户端持续得到
200。修复目标不是改变 MCP 协议或会话模型，而是消除错误归因：客户端已经断开且下游未发送
响应时，应用不应再由外层 HTTP middleware 合成服务端 500 和堆栈日志。

下列行为是约束，不在本次设计中改变：

- MCP 继续使用 `stateless_http=True` 和 `json_response=True`。
- `/mcp` 继续经过全局安全响应头和 legacy host 规则。
- `X-Treg-Body-Encoding` 解码顺序和行为不变。
- `/call/{rest:path}` 的请求、响应和 faithful-relay 合同不变。
- 真正的应用异常仍然向上传播并按现有错误路径处理，不能被当成断连吞掉。

## 根因验证

### 1. 代码路径

`bootstrap.create_app()` 当前组装出的 middleware 顺序由外到内为：

1. `_BodyDecodeMiddleware`，纯 ASGI
2. `BaseHTTPMiddleware(dispatch=_security_headers)`
3. `BaseHTTPMiddleware(dispatch=_legacy_host_redirect)`
4. FastAPI 路由和挂载的 `/mcp` ASGI app

`build_mcp_app()` 创建 stateless Streamable HTTP transport。每个请求结束时，MCP SDK 都会终止
该请求的 transport，所以生产日志中的 `Terminating session: None` 与 stateless 模式一致，不代表
共享会话损坏。

Starlette 的 `BaseHTTPMiddleware.call_next()` 使用 AnyIO memory stream 接收下游 ASGI 响应。若下游
在发送 `http.response.start` 前结束并关闭 stream，且没有可重新抛出的下游异常，`call_next()` 会把
`anyio.EndOfStream` 翻译为：

```text
RuntimeError: No response returned.
```

这与生产堆栈中 `starlette/middleware/base.py`、`_legacy_host_redirect`、`_security_headers` 和
`_BodyDecodeMiddleware` 的顺序完全吻合。`_BodyDecodeMiddleware` 只是堆栈中更外层的纯 ASGI
middleware，不是生成该 RuntimeError 的位置。

### 2. 本地端到端复现

在当前分支用临时 SQLite 启动真实 `treg.api:app`：

```bash
TREG_DATABASE_URL=sqlite+aiosqlite:////tmp/treg-mcp-disconnect/treg.db \
TREG_PUBLIC_URL=http://127.0.0.1:8000 \
TREG_EMAIL_DEV_MODE=true \
uv run --frozen uvicorn treg.api:app --host 127.0.0.1 --port 8000 --log-level debug
```

验证结果：

- 正常 MCP initialize 请求返回 200，并包含现有四个安全响应头。
- 用 raw TCP socket 在完整 POST 发出后立即 RST，以及声明较大 `Content-Length`、只发部分 body
  后 RST，真实 MCP transport 都进入 `Terminating session: None` 路径。
- 当前本地依赖组合下，精确的生产 RuntimeError 竞态不能稳定重现。部分断连由 Uvicorn 取消请求
  task，另一些由当前 MCP SDK 捕获并尝试生成内部响应。因此不能声称本地完整复现了生产 500。

### 3. 确定性边界复现

为隔离竞态，用仓库当前的 `_legacy_host_redirect` 和 `_security_headers` 包住一个最小 ASGI 下游。
该下游收到 `http.disconnect` 后正常返回，但不发送 `http.response.start`。通过
`uv run --frozen python` 直接驱动同样的 ASGI scope，稳定得到：

```text
builtins.RuntimeError: No response returned.
```

因此证据链为：

- 真实 socket abort 可以稳定触发 MCP transport 的 terminate 分支。
- 生产堆栈直接证明该次竞态中 MCP 未发送 response start，并由 BaseHTTP 适配层生成 500。
- 确定性边界测试证明，只要下游以这一方式结束，仓库当前两个 BaseHTTP middleware 必然产生
  同一 RuntimeError。

结论：根因判断成立。缺陷不在 MCP stateless 配置，也不在 `_BodyDecodeMiddleware`，而在下游断连
结束语义与 `BaseHTTPMiddleware.call_next()` 响应流假设不兼容。精确触发窗口受 Uvicorn、AnyIO 和
MCP transport 调度影响，所以本地真实网络复现不是确定性的。

## 方案比较

| 方案 | 做法 | 优点 | 风险和代价 | 结论 |
|---|---|---|---|---|
| A. 两个 middleware 改为纯 ASGI | 将 legacy redirect 和 security headers 各实现为 ASGI middleware class，保持现有嵌套顺序 | 移除产生错误翻译的 memory-stream 和 `call_next` 层；不依赖异常字符串；对所有断连时序都符合 ASGI 语义；真正异常仍传播 | 作用于所有 HTTP 路径，必须严格锁定 headers、redirect、非 HTTP scope 和注册顺序 | 推荐 |
| B. 让 `/mcp` 绕开全局 middleware | 在 FastAPI 外层按 path 分流，或单独挂载一套不含 BaseHTTP 的 app | 可以把变更限制在 MCP 表面 | 会复制 mount、`root_path` 和 lifespan 组合；若直接绕开会丢安全响应头、legacy host 行为和 body decode；若重新包裹又形成第二套容易漂移的 middleware 栈 | 不采用 |
| C. 识别断连后静默或记 499 | 在更外层捕获 `RuntimeError("No response returned.")`，结合 disconnect/path 判断后抑制或改记 499 | 代码改动表面最小 | 依赖 Starlette 内部异常文字；难以可靠区分客户端断连与真正的“应用忘记发响应”；仍保留 BaseHTTP 的 task 和 stream 问题；客户端已断开时无法实际收到 499 | 不采用 |

499 是反向代理常用但非标准的观测状态。连接已经消失时，应用不能向客户端可靠发送 499。若边缘或
访问日志能识别断连，可以把该请求记为 499；应用层修复只需允许 ASGI task 无响应结束，不再虚构
500，也不应新增一个写往已关闭 socket 的响应。

## 推荐设计

选择方案 A，将 `_legacy_host_redirect` 和 `_security_headers` 改成两个纯 ASGI middleware class。

### Legacy host redirect

- 只处理 `scope["type"] == "http"`，其他 scope 原样透传。
- 用 Starlette `Request` 读取现有 method、Host、path、query 和 cookie，不读取 body。
- 完整保留现有判断：仅 GET/HEAD；legacy host 精确匹配；匿名 marketing path 为 301；auth entry
  为 302；session cookie 阻止 marketing redirect；canonical host、自托管 legacy host、lookalike
  host 和所有其他路径原样透传。
- 命中时直接调用 `RedirectResponse(scope, receive, send)`；未命中时直接
  `await self.app(scope, receive, send)`，不创建 memory stream。

### Security headers

- 只处理 HTTP scope，其他 scope 原样透传。
- 包装 `send`，仅在 `http.response.start` 上补齐现有四个 header：
  `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、
  `Referrer-Policy: no-referrer`、
  `Strict-Transport-Security: max-age=31536000; includeSubDomains`。
- header 查找必须大小写不敏感，并保持当前 `setdefault` 语义。若 `/call` 或其他下游已经给出同名
  header，不能覆盖、删除、重排或重复添加它。
- 若下游因断连而在 response start 前正常结束，该 middleware 也正常结束且不发送任何响应。
- 若下游抛出真正异常，原样传播，不按 path、异常类型或文字吞掉。

### 注册顺序

继续由 `bootstrap.create_app()` 统一组装。三个 `add_middleware()` 调用必须保持最终嵌套顺序：

```text
BodyDecode -> SecurityHeaders -> LegacyHostRedirect -> routes/mounts
```

Starlette 的 `add_middleware()` 会 prepend，实施时要用测试和 composition snapshot 核对实际顺序，
不能只根据代码阅读假定顺序。`all`、`control`、`dataplane` 三个 role 使用同一规则。

## 快照影响

这是有意的 composition 行为变更。

`tests/snapshots/composition.json` 预计只有两个 middleware 条目改变：

- `starlette.middleware.base.BaseHTTPMiddleware` 加
  `dispatch=treg.api._security_headers` 改为新的 security 纯 ASGI class，kwargs 为空。
- `starlette.middleware.base.BaseHTTPMiddleware` 加
  `dispatch=treg.api._legacy_host_redirect` 改为新的 legacy redirect 纯 ASGI class，kwargs 为空。
- 三层顺序仍为 BodyDecode、Security、Legacy。

`routes.json`、`openapi.json`、`lifespan.json` 不应有任何 diff。实现时应在同一个代码 commit 中运行：

```bash
uv run --frozen python scripts/dump_surface.py
uv run --frozen python -m pytest -q tests/test_surface_snapshot.py
```

只接受上述 composition diff。commit message 正文要明确说明：composition snapshot 更新是因为两个
BaseHTTP wrapper 被纯 ASGI middleware 替代，用于正确处理客户端断连，不是路由或 OpenAPI 变更。

## 回归风险和验证矩阵

### 客户端断连

- 增加确定性回归测试：以生产相同顺序组装三个真实 middleware，挂一个收到
  `http.disconnect` 后不发 response 的 ASGI sentinel，断言调用正常结束、没有
  `RuntimeError`、没有伪造 `http.response.start`。
- 增加正常 MCP initialize 集成断言，确认 200、JSON response、认证挑战和安全 header 均保持。
- raw socket RST 适合作为实现后的手工 E2E 验证，但它依赖调度时序，不作为要求每次稳定触发日志的
  CI 断言。验收标准是服务无 500 traceback、正常 MCP 客户端仍成功。

### 安全响应头

- 扩充现有 `test_security_headers_on_api`，明确断言四个 header 的值。
- 覆盖普通 2xx、404 或受控错误响应，确保纯 ASGI send wrapper 在不同响应路径都生效。
- 增加下游预设同名 header 的单元测试，证明大小写不敏感的 setdefault，不覆盖 `/call` 更严格的值。
- 保留 `tests/test_mcp_oauth.py` 中 MCP 响应的 `X-Frame-Options` 断言。

### Legacy host 301/302

- 完整运行 `tests/test_legacy_host_redirect.py`。它已覆盖匿名 marketing 301、query 保留、session
  cookie、不带条件的 auth entry 302、API/MCP 相关 surface 不跳转、POST 不跳转、canonical host、
  lookalike host 和自托管边界。
- 增加或确认 HEAD 与 GET 同规则，redirect response 的 Location 和 status 完全一致。

### `X-Treg-Body-Encoding`

- `_BodyDecodeMiddleware` 代码不改，且仍为最外层。
- 完整运行 `tests/test_body_encoding.py`，覆盖 base64、gzip、组合解码、Pydantic JSON、`/call` relay、
  malformed 400 和无 header 透传。
- composition snapshot 明确锁定 BodyDecode 仍在 Security 和 Legacy 之外。

### `/call/` 完全不受影响

- 运行完整 `tests/callmatrix/test_matrix.py`，验证 faithful relay、错误分类、计费和重试矩阵不变。
- 运行 `tests/test_body_encoding.py::test_proxy_relays_decoded_body_upstream`，证明解码后的请求体仍原样
  进入 `/call`。
- 增加或保留一个 `/call` 下游自带安全 header 的断言，证明 security middleware 只补缺失值。
- `routes.json` 和 `openapi.json` 零 diff，证明 `/call/{rest:path}` 路由形态未变。

### 建议验收命令

```bash
uv run --frozen python -m pytest -q \
  tests/test_mcp_oauth.py \
  tests/test_legacy_host_redirect.py \
  tests/test_bughunt_server.py \
  tests/test_body_encoding.py \
  tests/test_surface_snapshot.py
uv run --frozen python -m pytest -q tests/callmatrix/test_matrix.py
uv run --frozen python -m pytest -q
```

实现后还应启动真实 Uvicorn，重复正常 MCP initialize 和 raw socket abort 手工 E2E。最后运行
`scripts/drift.sh`，并确认只有 `composition.json` 产生上述预期 diff。

## 评审后实施范围

若本设计通过，代码改动应限制在：

- `src/treg/api.py`：两个纯 ASGI middleware class。
- `src/treg/bootstrap.py`：以纯 ASGI class 注册并保持顺序。
- 对应 middleware/MCP 回归测试。
- `tests/snapshots/composition.json`：同 commit 更新。
- `docs/context/architecture/composition.md`：同 commit 记录纯 ASGI middleware 约束和断连语义。

不改变 MCP transport 配置，不新增 `/mcp` 特殊分流，不返回应用层 499，不改变 `/call`、路由、
OpenAPI 或 lifespan。
