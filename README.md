# 配置变更三方合并 API

基于 **Python + FastAPI + SQLite** 的配置三方合并服务。客户端提交 `base`（共同祖先）、`left`、`right` 三份 JSON，服务按 JSON Pointer 递归合并对象：单侧修改自动采用、双侧同值修改自动合并、双侧不同修改在冲突处保留占位符并返回双方候选。客户端携带当前 `revision` 分批提交冲突取值（也支持规则批量裁决、历史合并单复用与持久化草案），全部解决后生成**不可变**结果；在合并单仍为 `pending` 时，还可基于初始快照重放**撤销上一批裁决**。

## 快速启动（单条命令）

```bash
docker compose up --build
```

依赖安装（镜像构建时，含 `jsonschema` Draft 2020-12 校验）与 SQLite 建表（应用启动时）自动完成，无需任何额外步骤。

- **API 地址**：http://localhost:8000
- **交互式文档（Swagger UI）**：http://localhost:8000/docs
- **端口**：`8000`
- 健康检查：`GET http://localhost:8000/health`

SQLite 数据文件持久化在名为 `merge-data` 的 Docker volume 中（容器内路径 `/data/merges.db`，可通过环境变量 `DATABASE_PATH` 修改）。

## 合并规则

按 JSON Pointer（RFC 6901）逐路径递归：

| 情形 | 结果 |
| --- | --- |
| 仅一侧修改 | 自动采用该侧 |
| 双侧修改为相同值 | 自动合并为该值 |
| 双侧修改不同值（对象） | 按键递归合并 |
| 双侧修改不同值（标量/数组/类型变化） | **冲突**：占位符 + 双方候选 |
| 一侧删除、另一侧未改 | 自动删除 |
| 一侧删除、另一侧修改 | **冲突**（`missing` 字段标记删除侧） |

冲突处在 `document` 中保留占位符：

```json
{"__merge_conflict__": "/db/host"}
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/merge-analysis` | 合并前影响分析：只试算不落库，按 JSON Pointer 返回四类 change records、分类计数与 `would_conflict` |
| `POST` | `/merges` | 提交 base/left/right，创建合并单（支持幂等键、可选 `result_schema` 门禁） |
| `GET` | `/merges/{merge_id}` | 状态查询：状态、revision、剩余冲突、最终结果 |
| `POST` | `/merges/{merge_id}/resolutions` | 携带 revision 分批提交冲突取值（最后一批须通过 schema 门禁） |
| `POST` | `/merges/{merge_id}/resolution-preview` | 预演冲突解决：内存副本上校验并应用，不落库、不改状态 |
| `POST` | `/merges/{merge_id}/rule-resolutions` | 携带 revision 与有序路径规则，按模式批量裁决冲突（最后一批须通过门禁） |
| `POST` | `/merges/{merge_id}/reuse-resolutions` | 从一个已 resolved 的历史合并单按指针复用裁决，原子应用全部匹配项 |
| `POST` | `/merges/{merge_id}/resolution-undo` | 撤销最近一批仍生效的裁决：从初始快照重放重建，revision +1 |
| `POST` | `/merges/{merge_id}/drafts` | 为待处理合并单创建解决草案（固定创建时的 revision） |
| `GET` | `/merges/{merge_id}/drafts/{draft_id}` | 查看草案：状态、钉住的 base_revision、已累积的取值 |
| `POST` | `/merges/{merge_id}/drafts/{draft_id}/resolutions` | 向草案追加或覆盖冲突取值（只改草案，不改合并单） |
| `GET` | `/merges/{merge_id}/drafts/{draft_id}/preview` | 草案预演：应用草案后的文档与剩余冲突，不落库 |
| `POST` | `/merges/{merge_id}/drafts/{draft_id}/commit` | 单事务提交草案：重校验后一次性应用全部取值并使草案失效 |
| `GET` | `/merges/{merge_id}/patch` | resolved 后：base → result 的 RFC 6902 补丁（含 SHA-256） |
| `GET` | `/merges/{merge_id}/audit` | 裁决审计链：按序返回全部裁决事件及逐合并单 SHA-256 链校验结果 |
| `GET` | `/health` | 健康检查 |

### 1. 创建合并单 — `POST /merges`

```bash
curl -s -X POST http://localhost:8000/merges \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-123' \
  -d '{
    "base":  {"replicas": 1, "db": {"host": "a", "port": 5432}},
    "left":  {"replicas": 3, "db": {"host": "b", "port": 5432}},
    "right": {"replicas": 1, "db": {"host": "c", "port": 5433}}
  }'
```

响应（`201`，`replicas`、`port` 已自动合并，`/db/host` 冲突）：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "status": "pending",
  "revision": 1,
  "document": {
    "replicas": 3,
    "db": {"host": {"__merge_conflict__": "/db/host"}, "port": 5433}
  },
  "conflicts": [
    {"pointer": "/db/host", "base": "a", "left": "b", "right": "c", "missing": []}
  ],
  "conflict_count": 1,
  "result": null,
  "created_at": "...", "updated_at": "..."
}
```

**幂等**：在请求体 `idempotency_key` 字段或 `Idempotency-Key` 请求头中携带幂等键。相同键重复提交返回既有合并单（`200`，同一个 `merge_id`），**不会创建新合并单**。

无冲突的合并单创建即 `resolved`，`result` 立即可用且不可变。

#### 最终结果门禁：`result_schema`（可选）

创建合并单时可在请求体中携带 `result_schema`，一个遵循 [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12) 的 JSON Schema，用于约束**不可变最终结果**：

1. **先校验 schema 本身**：服务按 Draft 2020-12 元 schema 校验 `result_schema`。schema 不合法（如 `{"type": "whatever"}`、`required` 不是数组、非法正则等）→ **`422`**，响应体按 `instance_path`、`schema_path` 稳定排序列出**全部**元 schema 错误，且**不创建任何记录**（幂等键仍可再次使用）。
2. **无冲突的初始合并也必须通过**：三份文档合并后若不存在冲突，合并单创建即 `resolved`，此时立即用 schema 校验合并结果；不通过 → **`422`**、不落库。
3. **存在冲突时创建不校验**：工作文档还带着冲突占位符，门禁延后到冲突解决时生效。

```bash
curl -s -X POST http://localhost:8000/merges \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"replicas": 1, "db": {"host": "a", "port": 5432}},
    "left":  {"replicas": 3, "db": {"host": "b", "port": 5432}},
    "right": {"replicas": 1, "db": {"host": "c", "port": 5433}},
    "result_schema": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "required": ["replicas", "db"],
      "properties": {
        "replicas": {"type": "integer", "minimum": 1},
        "db": {
          "type": "object",
          "required": ["host", "port"],
          "properties": {
            "host": {"type": "string", "minLength": 1},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535}
          }
        }
      }
    }
  }'
```

非法 schema 的 `422` 响应（`instance_path` 指向被提交 schema 内的位置；`schema_path` 指向违反的元 schema 关键字）：

```json
{
  "detail": "invalid result_schema: not a valid JSON Schema (Draft 2020-12)",
  "errors": [
    {
      "instance_path": "/type",
      "schema_path": "/allOf/3/properties/type/anyOf",
      "message": "'whatever' is not valid under any of the given schemas"
    }
  ]
}
```

布尔 schema（`true` / `false`）也是合法的 Draft 2020-12 schema：`false` 表示任何最终结果都不被接受（只能用于存在冲突的合并单，否则创建即 `422`）。未携带 `result_schema` 时行为与以前完全一致。

### 2. 状态查询 — `GET /merges/{merge_id}`

```bash
curl -s http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023
```

返回当前 `status`（`pending` / `resolved`）、`revision`、剩余 `conflicts`、带占位符的 `document` 及最终 `result`。

### 3. 分批解决冲突 — `POST /merges/{merge_id}/resolutions`

每条取值二选一：`choice`（采用某侧候选，`"left"` / `"right"`；若该侧为删除侧则删除该键）或 `value`（显式取值，可为 `null`）。可一次提交多条（批量），也可分多批提交。

```bash
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/resolutions \
  -H 'Content-Type: application/json' \
  -d '{
    "revision": 1,
    "resolutions": [
      {"pointer": "/db/host", "choice": "left"}
    ]
  }'
```

- **revision 过期 → `409`**：请求中的 `revision` 必须与当前一致（创建时为 1，每接受一批 +1）。过期时响应体携带 `current_revision`，客户端重新拉取状态后重试。
- **`result_schema` 门禁**：仅当本批将解决**最后一个**冲突（产生最终结果）时，才用创建合并单时携带的 schema 校验候选结果；**部分解决**的批次仍留着占位符文档，不做校验。最终批次校验失败 → **`422`**，响应体按 `instance_path`、`schema_path` 稳定排序列出全部违规（每条含 `instance_path`、`schema_path`、`message`），**整批不写入、revision 不变、冲突列表保持原样**，可修正取值后用原 revision 重试。
- 全部冲突解决后 `status` 变为 `resolved`，`result` 生成且**不可变**；此后再提交取值一律返回 `409`。
- 对不存在的冲突指针取值返回 `400`；合并单不存在返回 `404`。

例如 schema 要求 `/db/host` 为字符串时，最终批次若给出整数会得到：

```json
{
  "detail": "final result does not satisfy result_schema",
  "errors": [
    {
      "instance_path": "/db/host",
      "schema_path": "/properties/db/properties/host/type",
      "message": "5432 is not of type 'string'"
    }
  ]
}
```

### 4. 预演冲突解决 — `POST /merges/{merge_id}/resolution-preview`

请求格式与正式提交（`POST /merges/{merge_id}/resolutions`）完全相同（`revision` + `resolutions`），但只在**内存副本**上按顺序校验并应用：**不写入 SQLite、不递增 revision、不改变状态**，可安全反复调用，用于在正式提交前试算取值方案。

```bash
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/resolution-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "revision": 1,
    "resolutions": [
      {"pointer": "/db/host", "choice": "left"}
    ]
  }'
```

响应（预演结果，合并单本身不受影响）：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "revision": 1,
  "document": {
    "replicas": 3,
    "db": {"host": "b", "port": 5433}
  },
  "remaining_conflicts": [],
  "would_resolve": true,
  "patch": [
    {"op": "replace", "path": "/db/host", "value": "b"},
    {"op": "replace", "path": "/db/port", "value": 5433},
    {"op": "replace", "path": "/replicas", "value": 3}
  ],
  "result_sha256": "5f4a…（64 位十六进制）"
}
```

- `would_resolve` 为 `true`（该批次可解决全部剩余冲突）时，额外返回 `base` → 预演结果的确定性 `patch`（规则同 `GET /merges/{merge_id}/patch`）与 `result_sha256`；否则不携带这两个字段。
- 任一指针对应的冲突不存在或已在本批中被解决（即指针重复）、或同一条目同时给出 `choice` 与 `value`（显式 `null` 也算给出 `value`）→ 整批 **`400`**，同样无任何副作用。
- `revision` 过期、合并单已 `resolved` → 与正式提交相同的 **`409`**；合并单不存在 → **`404`**。

### 5. 规则批量裁决 — `POST /merges/{merge_id}/rule-resolutions`

不想逐条枚举冲突指针时，可携带当前 `revision` 与一组**有序规则**，由服务把每条开放冲突分配给唯一规则后一次性裁决。每条规则给出 `left` 和/或 `right` 一个 [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901) 风格的路径模式，命中即采用对应一侧的候选：

- 普通路径与 JSON Pointer 写法一致（`~` 转义为 `~0`、`/` 转义为 `~1`，`""` 表示文档根）；
- 单段 `*`：恰好匹配**一个**路径段，如 `/services/*/image`；
- 末尾 `**`：匹配剩余的**零个或多个**路径段，如 `/services/**`；
- 通配符必须独占整段，且 `**` 只能出现在最后一段（`/a/**/b`、`/a/x*` 均为非法模式 → `400`）。

**唯一规则的选择次序**：对每个开放冲突，在所有命中它的模式中按以下优先级取最高者——

1. **字面量段更多**（含字面量的段越多越优先）；
2. **`*` 更少**（模式中 `*` 字符数更少：`*` 算 1 个、`**` 算 2 个）；
3. **声明顺序更前**（请求 `rules` 数组中的下标更小）；
4. 同一条规则内 `left` 优先于 `right`。

```bash
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/rule-resolutions \
  -H 'Content-Type: application/json' \
  -d '{
    "revision": 1,
    "rules": [
      {"left":  "/services/*/image", "right": "/services/*/replicas"},
      {"right": "/db/**"},
      {"left":  "/**"}
    ]
  }'
```

响应在普通状态视图（`status` / `revision` / `document` / `conflicts` / `result`）之外，额外返回各规则命中的指针与未匹配冲突：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "status": "pending",
  "revision": 2,
  "document": { "...": "..." },
  "conflicts": [
    {"pointer": "/feature/x", "base": 0, "left": 1, "right": 2, "missing": []}
  ],
  "conflict_count": 1,
  "result": null,
  "rule_matches": [
    {"rule_index": 0, "side": "left",  "pointers": ["/services/api/image"]},
    {"rule_index": 0, "side": "right", "pointers": ["/services/api/replicas"]},
    {"rule_index": 1, "side": "right", "pointers": ["/db/host", "/db/port"]}
  ],
  "unmatched_conflicts": ["/feature/x"],
  "applied": true,
  "created_at": "...", "updated_at": "..."
}
```

- **先整批校验，再一次事务提交**：先编译全部模式、为每个冲突选定唯一规则，并验证所有命中项都可应用；只有全部通过时才在**单条 UPDATE 事务**中写入，`revision` 恰好 **+1**。任何非法模式、缺少 `left`/`right` 的规则、或任一命中项应用失败，整批 **`400`**，**不会产生任何部分修改**（revision 与冲突列表保持原样）。
- **`result_schema` 门禁同显式取值**：仅当本批将解决最后一个冲突（所有冲突均被命中且应用后无剩余）时才校验候选最终结果；部分命中（仍有 `unmatched_conflicts`）或一条都未命中（`applied: false`）不校验。校验失败 → **`422`**，全部违规按 `instance_path`、`schema_path` 稳定排序返回，整批不写入、`revision` 不变。
- **缺失侧沿用删除语义**：规则选中的一侧若处于删除态（冲突的 `missing` 中含该侧），则删除对应键；若缺失的是文档根本身（无法删除），整批 `400`。
- **未匹配冲突**原样保留并通过 `unmatched_conflicts` 返回；它们不阻塞其余命中项的提交，但会使合并单继续处于 `pending`。
- **一条都没命中**时不写入、不递增 revision（响应中 `applied: false`）；这不算错误，返回 `200`。
- **revision 过期 → `409`**（响应体携带 `current_revision`）；合并单已 `resolved` 后同样 `409`；合并单不存在 → `404`。

### 6. 历史裁决复用 — `POST /merges/{merge_id}/reuse-resolutions`

处理新一轮变更时，当前（目标）合并单的开放冲突往往与某个**历史（来源）合并单**裁决过的冲突完全相同。携带当前 `revision` 与一个已 `resolved` 的来源 `source_merge_id`，服务会把来源对相同冲突的最终裁决一次性复用到目标：

```json
{
  "revision": 3,
  "source_merge_id": "bc6d2c3d9a3343bf9177147a10ec4023"
}
```

**逐项匹配规则**（目标的每个开放冲突都会出现在 `applied` 或 `skipped` 之一）：

1. **按 JSON Pointer 定位**：只在来源冲突快照中**相同指针**处寻找来源冲突（指针不同一律不复用）。
2. **规范化后逐项比对**：冲突的 `base`、`left`、`right` 经规范化 JSON（键排序、类型严格：`1` ≠ `true` ≠ `1.0`，数组顺序有意义）序列化后必须**完全一致**，且 `missing` 语义（哪些侧缺键）也必须一致。
3. **严格区分缺失键与显式 `null`**：来源快照中缺失侧的候选序列化为 `null`，但它由 `missing` 字段标记为“键不存在”；目标侧若把该键显式设为 `null`（`missing` 中没有该侧），二者**不匹配**，不会复用。
4. **复用值取自来源最终结果**（不是当时选了哪一侧）：从来源不可变 `result` 的相同指针读取最终值写入目标；来源最终结果中该路径已不存在，说明来源选择了**沿用删除**，目标中同样删除该键。

**来源冲突快照**：每个合并单创建时都会把全部初始冲突（`pointer`/`base`/`left`/`right`/`missing`）原样持久化为不可变快照（SQLite `source_conflicts` 列）；之后分批解决会缩小活跃 `conflicts`，但快照保持不变，因此部分批次解决完的历史合并单同样可作为来源。

响应在普通状态视图之外，返回每个冲突的逐项去向：

```json
{
  "merge_id": "a1b2…",
  "status": "pending",
  "revision": 4,
  "document": { "…": "…" },
  "conflicts": [{ "pointer": "/d", "base": 1, "left": 2, "right": 4, "missing": [] }],
  "conflict_count": 1,
  "result": null,
  "applied": true,
  "source_merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "applied_count": 3,
  "applied_resolutions": [
    {"pointer": "/a", "action": "set", "value": 2},
    {"pointer": "/b", "action": "set", "value": "B"},
    {"pointer": "/c", "action": "delete"}
  ],
  "skipped_count": 1,
  "skipped": [
    {
      "pointer": "/d",
      "reason": "no_source_conflict",
      "detail": "the source merge has no recorded conflict at this pointer"
    }
  ],
  "created_at": "...", "updated_at": "..."
}
```

- `applied_resolutions`：成功复用的逐项结果，`action` 为 `set`（含取自来源结果的 `value`，显式 `null` 照常返回）或 `delete`（来源最终结果中该键已删除，目标同步删除）。
- `skipped`：未复用项及原因，`reason` 取值：
  - `no_source_conflict`：来源快照中不存在相同指针的冲突；
  - `conflict_mismatch`：`base`/`left`/`right` 或 `missing` 规范化后不一致，额外给出 `mismatched_fields`（如 `["right"]`、`["missing"]`）；
  - `source_root_deleted`：来源文档根不存在且根无法删除（防御性情形）。
- **一次请求原子应用全部匹配项**：匹配、取值与门禁校验全部在内存中完成后才执行**单条 UPDATE**，`revision` 恰好 **+1**；任何错误（revision 过期、来源不合法、最终 `result_schema` 失败）都**不写入**。
- **没有任何匹配项也是成功请求（`200`）**：`applied: false`，**不写入、不递增 revision**，所有冲突均出现在 `skipped` 中。
- **`result_schema` 门禁同其它解决路径**：仅当复用批次将解决最后一个冲突时校验候选最终结果，失败 → `422`（全部违规列表）、整批回滚、`revision` 不变；仍有冲突剩余的部分批次不校验。
- **revision 过期 / 目标已 `resolved` → `409`**（过期时携带 `current_revision`）；目标合并单不存在 → `404`。
- **来源不合法 → `400`**：来源合并单不存在、尚为 `pending`（未产生最终结果）、缺少冲突快照（早于快照功能的历史数据），均返回 `400` 且目标不发生任何变化。

### 7. 撤销上一批裁决 — `POST /merges/{merge_id}/resolution-undo`

一批裁决提交后若想反悔（取值选错、批量规则误伤、复用了不合适的历史结果），可对**仍为 `pending`** 的合并单携带当前 `revision` 撤销**最近一条仍生效的裁决事件**：该批涉及的全部指针重新成为开放冲突，工作文档回到该批应用之前的状态，`revision` **+1**。

撤销的实现方式是**重放重建，而不是反向修改当前 JSON**：

1. 从合并单**不可变的初始冲突快照**（创建时的工作文档与初始冲突列表）出发；
2. 按 `seq` 顺序重放审计链中的裁决事件（`resolutions` / `rule-resolutions` / `reuse-resolutions` / `draft-commit`），每条事件都自带该批的全部指针与裁决内容，可直接还原为对初始冲突的取值；
3. **跳过**已经被撤销补偿的裁决事件以及撤销事件本身——当前要撤销的目标事件同样跳过；
4. 重放得到的文档与开放冲突即为撤销后的状态，整体替换工作文档（删除语义、显式 `null`、规则选侧、复用取值/删除都随重放原样恢复）。

```bash
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/resolution-undo \
  -H 'Content-Type: application/json' \
  -d '{"revision": 2}'
```

响应（普通状态视图之外，额外说明被撤事件与恢复的指针）：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "status": "pending",
  "revision": 3,
  "document": {
    "replicas": 3,
    "db": {"host": {"__merge_conflict__": "/db/host"}, "port": 5433}
  },
  "conflicts": [
    {"pointer": "/db/host", "base": "a", "left": "b", "right": "c", "missing": []}
  ],
  "conflict_count": 1,
  "result": null,
  "undone_seq": 1,
  "undone_method": "resolutions",
  "restored_pointers": ["/db/host"],
  "restored_count": 1,
  "created_at": "...", "updated_at": "..."
}
```

- **一次只撤销一条**：即当前最近一条**未被补偿**的有效裁决事件。撤销成功后该事件不会从审计链中删除，而是由新追加的 `resolution-undo` 事件在 `detail.undone_seq` 中记录被补偿事件的序号（并在 `detail.restored_pointers` / 顶层 `restored_pointers` 中列出恢复的指针）；**再次撤销会自动跳过已补偿事件及其撤销事件**，继续回退更早的批次。所有事件仍在同一条哈希链上，链始终可验证。
- **撤销后可重新裁决**：恢复的冲突可以用任意方式（显式取值 / 规则 / 复用 / 草案）重新解决，新事件正常追加；`revision` 只增不减，事件序号连续追加，不做物理删除。
- **已 `resolved` 的合并单不可撤销**：最终结果一旦生成即不可变，对其撤销返回 **`409`**；这也意味着“最后一批把冲突全部解决”的裁决无法撤销——结果已锁定。
- **没有可撤销的裁决**（从未成功裁决过，或所有有效裁决都已被撤销）→ **`409`**（`no effective adjudication to undo`），不写状态、不追加事件。
- **`revision` 过期 → `409`**（响应体携带 `current_revision`）；合并单不存在 → **`404`**。
- **失败全程无副作用**：任何 `404` / `409` 都在写入前返回，合并单状态、`revision`、文档、冲突与审计链均保持原样，不留撤销事件。若审计链本身校验不通过（事件被外部篡改或删除，`GET .../audit` 的 `chain_valid` 为 `false`），撤销拒绝执行并返回 **`500`**（重放所依赖的历史已不可信），同样整体回滚、不写状态、不留事件。
- 撤销本身不受 `result_schema` 门禁影响（它不产生最终结果，且合并单必然仍有开放冲突）。
- 撤销会推进 `revision`，因此创建时钉住旧 `revision` 的草案在撤销后提交将正常得到 **`409`**（草案内容保留，可新建草案继续编辑）。

### 8. 持久化解决草案 — `/merges/{merge_id}/drafts`

逐条提交 `resolutions` 要求每批都携带最新 revision，适合在线一次性解决。若取值需要跨请求逐步收集（多人协作、审批流、离线试算），可以为 `pending` 合并单创建**解决草案**：草案**固定创建时的 merge revision**（`base_revision`），并快照当时的工作文档与开放冲突；之后的编辑只写草案，**不改合并单**。

**生命周期**：`open`（可编辑、可预演、可提交）→ 提交成功后 `committed`（**失效**，再编辑/预演/提交一律 `409`，草案内容仍可 `GET` 查看）。

```bash
# 1) 创建草案（201）：返回 draft_id、base_revision、空 resolutions
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/drafts

# 2) 追加或覆盖取值（可分多批补齐；同一指针重复给出即覆盖，语义同 resolutions）
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/drafts/9f1e…/resolutions \
  -H 'Content-Type: application/json' \
  -d '{"resolutions": [{"pointer": "/db/host", "choice": "left"}]}'

# 3) 预演：基于创建时快照应用草案，返回剩余冲突；补全时附 patch 与 result_sha256
curl -s http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/drafts/9f1e…/preview

# 4) 提交：单事务内重校验后一次性应用全部取值，草案随即失效
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/drafts/9f1e…/commit
```

预演响应（草案只覆盖了部分指针时，`remaining_conflicts` 列出其余开放冲突）：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "draft_id": "9f1e…",
  "base_revision": 1,
  "current_revision": 1,
  "document": {"replicas": 3, "db": {"host": "b", "port": 5433}},
  "remaining_conflicts": [],
  "would_resolve": true,
  "patch": ["…"],
  "result_sha256": "5f4a…"
}
```

- **编辑语义复用 resolutions**：每条取值二选一 `choice` / `value`（显式 `null` 算给出 `value`；两者同给或都不给 → `400`）；未知冲突指针 → `400`（`no open conflict at pointer …`）。编辑只更新草案行，合并单的 `revision`、`conflicts`、`document` 均不受影响。
- **预演无副作用**：基于草案创建时的快照计算，不写入、不改状态；`would_resolve` 为 `true` 时附确定性 `patch` 与 `result_sha256`（规则同 `resolution-preview`）。`current_revision` 与 `base_revision` 不一致时说明合并单已被其他请求推进，提交将返回 `409`。
- **提交是原子操作**：在**单个事务**内重读合并单并重新校验——① 草案仍为 `open`；② 合并单仍 `pending`；③ 当前 `revision` 等于草案的 `base_revision`；④ 草案覆盖的每个指针仍是开放冲突；⑤ 若草案将解决最后一个冲突，候选结果须通过 `result_schema` 门禁。全部通过才**一次性应用全部取值**（`revision` 恰好 +1）并将草案置为 `committed`；任一条件变化 → **`409`**（revision 过期时响应体携带 `current_revision`；schema 不通过 → 复用 `422` 错误列表），**合并单与草案内容均不部分写入**（草案保持 `open`、取值原样保留，可修正后重试或放弃）。
- **部分草案也可提交**：未覆盖的冲突原样保留，合并单继续 `pending`（与分批 resolutions 语义一致）；空草案（无任何取值）提交 → `400`。
- 合并单已 `resolved` → 创建草案与提交草案均 `409`；合并单或草案不存在 → `404`（草案不能跨合并单 URL 访问）。

### 9. 获取确定性补丁 — `GET /merges/{merge_id}/patch`

合并单 `resolved` 后，可获取从**原始 `base_doc` 到最终 `result`** 的确定性操作序列（[RFC 6902 JSON Patch](https://www.rfc-editor.org/rfc/rfc6902)，路径为 [RFC 6901 JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901)）：

- **对象**按键递归生成 `add` / `remove` / `replace`；新增/删除的整个子树收敛为单条操作。
- **数组整体 `replace`**（不逐下标生成操作）；标量与类型变化同样是单条 `replace`。
- 路径中的 `~` 转义为 `~0`、`/` 转义为 `~1`；根文档路径为 `""`。
- 操作按 **JSON Pointer 字典序稳定输出**，依次应用到 `base` 可精确重建 `result`。
- 响应同时给出 `base` 与 `result` 的 **SHA-256**（基于规范化 JSON：键排序、无空白）。

```bash
curl -s http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/patch
```

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "status": "resolved",
  "patch": [
    {"op": "replace", "path": "/db/host", "value": "b"},
    {"op": "replace", "path": "/db/port", "value": 5433},
    {"op": "replace", "path": "/image", "value": "app:v2"},
    {"op": "replace", "path": "/replicas", "value": 3}
  ],
  "base_sha256": "9f2c…（64 位十六进制）",
  "result_sha256": "4a7e…（64 位十六进制）"
}
```

含新增 / 删除 / 数组及特殊键名的例子：

```json
"patch": [
  {"op": "add", "path": "/brand", "value": {"new": {"sub": true}}},
  {"op": "remove", "path": "/gone"},
  {"op": "replace", "path": "/list", "value": [1, 2, 3, 4]},
  {"op": "replace", "path": "/weird~1a~0k", "value": [1, 2]}
]
```

- 合并单尚为 `pending`（`result` 未生成）→ **`409`**。
- 合并单不存在 → **`404`**。
- 无冲突、创建即 `resolved` 的合并单可立即获取补丁（无变化时 `patch` 为 `[]`）。

### 10. 裁决审计链 — `GET /merges/{merge_id}/audit`

每一次**成功改变合并单**的裁决请求——`resolutions`、`rule-resolutions`、`reuse-resolutions`、草案 `commit`，或一次成功的 `resolution-undo`——都会在**与合并单更新相同的 SQLite 事务**内追加一条审计事件：事件与状态变更同事务提交或回滚，因此失败请求（`400` / `409` / `422`）、预演接口、草案编辑，以及零命中的规则裁决 / 历史复用（`applied: false`）、无可撤销批次的撤销请求都**不会留下任何事件**。

每条事件记录：

| 字段 | 说明 |
| --- | --- |
| `seq` | 合并单内单调递增的事件序号（从 1 开始） |
| `method` | 裁决方式：`resolutions` / `rule-resolutions` / `reuse-resolutions` / `draft-commit` / `resolution-undo` |
| `revision_before` / `revision_after` | 事件前后的 merge revision（撤销同样 +1，revision 永不回退） |
| `pointers` | 实际生效（撤销时为**恢复**）的 JSON Pointer（按应用顺序） |
| `detail` | 方式相关的裁决明细（逐指针的 choice/value 或 side/rule_index、复用来源 `source_merge_id`、草案 `draft_id`；撤销事件给出被补偿事件序号 `undone_seq`、`undone_method` 与 `restored_pointers`） |
| `prev_hash` / `hash` | SHA-256 链：见下 |
| `created_at` | 事件时间（UTC ISO 8601） |
| `valid` | 本次链校验中该事件是否通过 |

**哈希链构造**（逐合并单一条链）：`hash = SHA-256(规范化JSON(事件内容))`，规范化 JSON 与文档摘要一致（键排序、无空白）；事件内容包含上一事件的 `hash`（`prev_hash`），每个合并单的首条事件以 64 个 `0` 作为创世 `prev_hash`。任何对历史事件的篡改或删除都会使链条在确定的位置断裂。

**链校验**：接口按 `seq` 顺序重放全部事件，逐条用库存字段重算哈希并检查序号连续性与 `prev_hash` 衔接，返回整体结论；链断裂时 `first_invalid_seq` 明确指出**首个异常事件的序号**（链完好时为 `null`）。

```bash
curl -s http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/audit
```

响应示例（先经 `resolutions` 解决 `/db/host`，再经 `rule-resolutions` 解决 `/db/port`）：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "event_count": 2,
  "events": [
    {
      "seq": 1,
      "method": "resolutions",
      "revision_before": 1,
      "revision_after": 2,
      "pointers": ["/db/host"],
      "detail": {"items": [{"pointer": "/db/host", "choice": "left"}]},
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "hash": "3d25…（64 位十六进制）",
      "created_at": "2026-09-20T03:36:35.598099+00:00",
      "valid": true
    },
    {
      "seq": 2,
      "method": "rule-resolutions",
      "revision_before": 2,
      "revision_after": 3,
      "pointers": ["/db/port"],
      "detail": {"items": [{"pointer": "/db/port", "side": "right", "rule_index": 0}]},
      "prev_hash": "3d25…",
      "hash": "7943…",
      "created_at": "2026-09-20T03:36:35.631001+00:00",
      "valid": true
    }
  ],
  "chain_valid": true,
  "first_invalid_seq": null
}
```

- 从未成功裁决过的合并单返回空链：`events: []`、`chain_valid: true`。
- **撤销不会删除或改写历史事件**：`resolution-undo` 是链上的普通追加事件，其 `prev_hash` 衔接上一事件，`hash` 按相同规则计算；被它补偿的裁决事件原样保留，因此“裁决 → 撤销 → 重新裁决 → 再撤销”的链条依然连续且整体可验证。
- 合并单不存在 → **`404`**。
- 既有接口的响应体不因审计模块增加任何字段，完全保持兼容。

### 11. 合并前影响分析 — `POST /merge-analysis`

创建合并单之前，可用与 `POST /merges` **完全相同的 `base` / `left` / `right`** 先做一次纯试算：服务按 JSON Pointer 联合遍历三份文档，返回每个**最小变更路径**的 change record 及其分类，并给出分类计数与 `would_conflict`。接口**不落任何库**（不创建合并单、不写审计、无 revision），天然幂等：**相同请求重复发送，响应逐字节一致**。

```bash
curl -s -X POST http://localhost:8000/merge-analysis \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"replicas": 1, "image": "app:v1", "db": {"host": "a", "port": 5432}},
    "left":  {"replicas": 3, "image": "app:v1", "db": {"host": "b", "port": 5432}},
    "right": {"replicas": 1, "image": "app:v2", "db": {"host": "c", "port": 5433}}
  }'
```

```json
{
  "changes": [
    {"pointer": "/db/host", "type": "conflict",
     "base": "a", "left": "b", "right": "c", "missing": []},
    {"pointer": "/db/port", "type": "right_only",
     "base": 5432, "left": 5432, "right": 5433, "missing": []},
    {"pointer": "/image", "type": "right_only",
     "base": "app:v1", "left": "app:v1", "right": "app:v2", "missing": []},
    {"pointer": "/replicas", "type": "left_only",
     "base": 1, "left": 3, "right": 1, "missing": []}
  ],
  "counts": {"left_only": 1, "right_only": 2, "same_change": 0, "conflict": 1, "total": 4},
  "would_conflict": true
}
```

**记录分类（`type`）**：

| type | 含义 |
| --- | --- |
| `left_only` | 仅 left 相对 base 发生变化（将自动采用 left） |
| `right_only` | 仅 right 相对 base 发生变化（将自动采用 right） |
| `same_change` | 两侧做了**相同**修改（将自动合并为该值） |
| `conflict` | 两侧做了**不同**修改（与合并引擎判定完全一致） |

每条记录包含：

- `pointer`：RFC 6901 JSON Pointer（根文档为 `""`；`~` → `~0`、`/` → `~1`）；
- `base` / `left` / `right`：该路径上的三方取值（缺失侧序列化为 `null`）；
- `missing`：哪些侧在该路径**键不存在**（取值同冲突记录，`"base"` / `"left"` / `"right"` 的子集）；
- `type`：上述分类之一。

**最小根路径（避免子树重复计数）**：

- 整个子树被一侧**新增**或**删除**、或发生**类型替换**（对象 ↔ 标量/数组）时，只在该子树的根路径输出**一条**记录，遍历不会进入子树，子树下的叶子不再单独计数；
- 一侧删除父节点、另一侧修改其子树时，只在父路径报告**一条 `conflict`**（与 `POST /merges` 实际产生的冲突位置一致）；
- 两侧对**已存在的对象**做不同修改时才按键递归到叶子；两侧以不同内容新增同一对象键时，同样按键递归（不重合的键各自成为 `left_only` / `right_only`）。

**缺失与显式 `null` 严格区分**：

- 一侧把键显式设为 `null`、另一侧删除该键 → 该路径为 `conflict`，记录中显式 `null` 照常作为取值，只有删除侧出现在 `missing` 中；
- 两侧都显式设为 `null` → `same_change` 且 `missing` 为空；
- 一侧新增显式 `null`、另一侧未新增 → `left_only` / `right_only`，`missing` 同时包含 base 与未变更侧。

其余约定：

- `changes` 按 `pointer` 字典序**稳定排序**；无任何变化时为 `[]`，各计数均为 `0`。
- `counts` 给出四类各自数量与 `total`；`would_conflict` 为 `true` 当且仅当该三元组在 `POST /merges` 中会创建**待处理（pending）**合并单，因此可用于提交前预判是否需要人工裁决。
- 缺少 `base` / `left` / `right` 任一字段 → **`422`**；字段类型可为任意合法 JSON（标量、数组、对象、`null`）。
- 该接口不接受也不需要 `revision`、`idempotency_key` 或 `result_schema`；它不产生任何副作用，可安全反复调用。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install httpx pytest
uvicorn app.main:app --reload --port 8000   # 本地运行
python -m pytest tests/ -q                  # 运行测试（188 项）
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与错误映射（404/409/400/422）
  analysis.py   # 合并前影响分析：纯内存三方联合遍历，四类 change records + would_conflict（不落库）
  merge.py      # 纯 Python 三方合并引擎（JSON Pointer 递归）
  rules.py      # RFC 6901 风格路径模式（* 与 **）：规则编译、匹配、批量裁决
  reuse.py      # 历史裁决复用：指针 + base/left/right/missing 规范化匹配、来源结果取值/删除
  undo.py       # 撤销上一批裁决：初始快照 + 审计事件重放重建文档与开放冲突
  patch.py      # 确定性 RFC 6902 补丁生成 + 规范化 SHA-256
  validation.py # Draft 2020-12 结果门禁：元 schema 校验 + 最终结果校验（422 错误排序）
  audit.py      # 裁决审计：同事务事件追加 + 逐合并单 SHA-256 哈希链校验
  store.py      # 合并单状态机 + 解决草案 + 历史复用 + 撤销：revision 校验、冲突解决、不可变结果、schema 门禁
  db.py         # SQLite 连接与建表（启动时执行，含轻量列迁移、审计表 CHECK 扩展与单事务提交支持）
  models.py     # Pydantic 请求模型（含可选 result_schema、草案编辑、历史复用、撤销请求）
tests/          # 合并引擎/补丁单测 + API 端到端测试（含 schema 门禁、草案、历史复用、审计链与撤销重放测试）
Dockerfile
docker-compose.yml
requirements.txt
```
