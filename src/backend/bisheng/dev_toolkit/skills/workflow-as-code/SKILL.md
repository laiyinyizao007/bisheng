---
name: workflow-as-code
description: >-
  用一份 YAML（flow.yaml）声明毕昇 / BiSheng 平台上的工作流，并用 bisheng flow 命令编译、
  对比线上、推送新版本、运行调试。当用户用自然语言描述想要的工作流（「建一个工作流：用户输入…
  大模型…如果…就…」）、要求修改已有工作流的节点 / 提示词 / 分支、提到 flow.yaml、bisheng flow
  compile / diff / push / run / tool，或想把网页画布上的工作流变成可版本管理的代码时触发——
  即使没点名 YAML 也应触发。托管应用的打包部署用「部署纳管」，应用接平台能力用「平台能力接线」。
metadata:
  display-name: 工作流声明（flow.yaml + bisheng flow）
---

# 工作流声明

你的任务：把用户用自然语言描述的工作流，写成一份 `flow.yaml`，用 `bisheng flow` 编译通过、推送成平台上的
一个版本，再跑一次给用户看结果。**YAML 是唯一真相源**——不要去手拼平台的节点 JSON，也不要绕过 `compile`
直接调接口写工作流。

读的顺序就是目录的顺序：

1. [工作步骤](#1-工作步骤) —— 从一句话需求到跑出结果
2. [必须停下来问人的情形](#2-必须停下来问人的情形)
3. [声明文件结构](#3-声明文件结构)
4. [变量引用](#4-变量引用)
5. [条件分支](#5-条件分支)
6. [节点参数](#6-节点参数) —— 12 类节点逐一列出
7. [工具节点](#7-工具节点)
8. [常见报错与改法](#8-常见报错与改法)

样例在本技能目录的 `example/` 下，都能直接编译通过，提示词外置在 `example/prompts/`。**改造样例比从零写更稳。**

- `flow.yaml`：多轮研究助手——对话框输入 → 条件分支 → 大模型 → 输出 → 回到输入。
- `form-loop.yaml`：研报摘要——表单输入 → 空值判断（含只有空格）→ 提示后回到表单 / 两个大模型串联 → 输出 → 结束。

---

## 1. 工作步骤

前提：用户已经 `bisheng login <平台地址> --api-key-stdin` 登录过，密钥带 `workflow:read`、
`workflow:write`、`workflow:invoke` 三个权限。没登录时命令会以退出码 3 结束并提示，**让用户自己登录，
你不要索要或经手密钥**。

1. **把需求拆成节点和连线。** 先列出：入口（`start`）→ 谁提供输入（`input`）→ 处理（`llm` / `agent` /
   `code` / 检索 / 工具）→ 怎么分支（`condition`）→ 给用户看什么（`output`）→ 在哪结束（`end`）。
   多轮对话 = 从 `output` 连回 `input`。
2. **写 `flow.yaml`。** 只写需要改的参数，其余用模板默认值（例如 `temperature` 没有明确需求就不写）。
   长文本放进单独的文件，用 `!include prompts/xxx.md` 引入——任何字符串值的位置都能用（提示词、消息文本、
   代码），路径相对 flow.yaml 所在目录，不能跳出该目录；被引入的内容按原文使用，不再当 YAML 解析。
   `start` 节点的 id 不必叫 `start_1`，本文示例只是沿用这个名字。
3. **编译：`bisheng --json flow compile flow.yaml`**（只读，不写平台）。失败时退出码 6，最后一行
   `result` 事件的 `details.issues` 逐条列出问题，每条带 `node` / `param` / `reference` / `field` 等定位
   字段。**一次改完所有 issue 再编译**，直到成功。
4. **首次推送：`bisheng --json flow push flow.yaml --create`**。它在平台上新建工作流并把绑定写进
   `.bisheng/flow.json`（这个文件应进 git）。之后的命令都会自动找到这条工作流。
5. **改动后先对比：`bisheng --json flow diff flow.yaml`**。结果在 `data` 里：`data.identical` 为 `true`
   表示与线上当前版本一致（不计画布坐标）；否则看 `data.diff` 里这五组（另有画布注释的差异
   `notes_only_online` / `notes_only_local`，只供参考）：
   - `nodes_only_online` / `edges_only_online`：**线上有、本地没有**的节点 / 连线；
   - `nodes_only_local` / `edges_only_local`：本地新增、线上还没有的；
   - `nodes_changed`：两边都有但不同的节点，逐项给出 `params`（`key` / `online` / `local`）与 `fields`。

   向用户复述差异，再推送：`bisheng --json flow push flow.yaml --name <版本名>`。推送的三种结果：
   - 与线上一致：**不建版本**，`data.pushed` 为 `false`；
   - 有差异：新建一个版本并切为当前版本，旧版本原样保留（加 `--no-activate` 则只建不切）；
   - 线上有本地没有的内容：见第 2 章，**停下来**。
6. **运行：`bisheng --json flow run flow.yaml`**。停在等待输入的节点时，结果是
   `status: waiting_input`，带 `node_id`、`fields` 和续跑凭据 `resume`。没有终端时这样续跑：
   `bisheng --json flow run flow.yaml --resume <resume> --input "用户的话"`
   （表单输入用 `--input-json '{"字段key": "值"}'`）。跑完是 `status: finished`，输出在 `flow_event`
   事件里。运行中某个节点失败时，报错会点名是哪个节点、什么原因。
7. 向用户汇报：建了哪条工作流、新版本名、跑了什么输入、得到了什么输出。

所有命令都加 `--json`：stdout 是 NDJSON，最后一行恒为 `{"event": "result", "ok": …, "data" | "error": …}`；
人读的文字在 stderr。

## 2. 必须停下来问人的情形

以下情况**停下来，把情况讲清楚交给用户决定**，不要自己拍板：

- **`diff` 显示线上有本地没有的内容**：`data.diff.nodes_only_online` / `edges_only_online` 非空，或
  `nodes_changed` 里有任何一项——线上取值与本地不同的参数、只在线上有的参数，或被改过的节点字段（名称、描述、
  页签等）。这通常是有人在网页上手改过。此时 `push` 在终端里会逐条列出并
  要求确认；在无终端环境会失败并提示「非交互环境下无法确认……确认无误后带上 `--allow-drop-online-only`
  重新执行」。**这条提示是给人看的，你不得自行带上这个参数**——带上它，新版本就不再包含这些线上改动。
  先把差异给用户看，由用户决定是把线上改动合进 YAML，还是明确放弃。（线上当前版本恰好就是本机上次推送的
  内容时不会出现这一步，那是正常的。）
- **需要平台资源的 id**：模型（`model_id`）、知识库、问答库、报告模板。它们只存在于用户的平台上，
  **不要编造**。向用户要（模型 id 可在网页「模型管理」查到），或者告诉用户哪些参数要在网页上补选。
- **需要密钥或口令**：例如 `agent` 节点的 `sql_agent` 数据库口令、工具的鉴权信息。**绝不写进 YAML，也不写进
  `!include` 引入的文件**——被引入的内容会编译进工作流定义、随 push 上传到平台，文件本身还会进 git。
  让用户在网页上配置。
- **用到 `code` 节点**：它默认被平台关闭（开启后能编辑工作流的人都能执行 Python），由管理员在系统设置的
  `workflow.code_node_enabled` 打开。编译和推送不受影响，但运行会失败并提示该开关。先问用户平台是否已开启，
  未开启就换用不需要代码的写法，或请管理员决定。
- **上线工作流**：`bisheng flow` 只建版本，不上线。上线是用户在网页上做的动作，不要试图替用户完成。
  线上已上线的工作流不能直接改，推送会报「先下线」——把这个提示转给用户。

## 3. 声明文件结构

顶层字段（写错或多写会被编译拒绝）：

<!-- keys:top -->
| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 工作流名称；`push --create` 用它建工作流（平台内不可重名） |
| `description` | 否 | 工作流描述 |
| `nodes` | 是 | 节点列表 |
| `edges` | 否 | 连线列表；不写也能编译，但除 `end` 外的节点都必须有出边，校验会逐个点名 |

节点字段：

<!-- keys:node -->
| 字段 | 说明 |
|---|---|
| `id` | 节点 id，工作流内唯一；变量引用和连线都用它。**只用英文字母、数字、下划线**（最长 50 字符）——变量引用只认这些字符，含中文或 `-` 的 id 无法被引用。用有意义的短名，如 `ask`、`analyst` |
| `type` | 节点类型，见第 6 章的 12 类，外加 `tool`（第 7 章） |
| `name` | 画布上显示的名称（可中文）；不写时用模板默认名或 id |
| `description` | 节点描述 |
| `params` | 参数键 → 值；键名必须是该类节点模板里有的（第 6 章），未知键会报 `unknown_param` |
| `tab` | 节点的模式页签，只有 `input`（`dialog_input` / `form_input`）和 `llm` / `agent`（`single` / `batch`）有 |
| `tool` | 仅 `tool` 节点：引用的工具操作标识（第 7 章） |
| `cases` | 仅 `condition` 节点：分支列表（第 5 章） |
| `position` | 画布坐标 `{x, y}`；不写时自动横排。只影响显示，`diff` 不比较它 |

连线字段：

<!-- keys:edge -->
| 字段 | 说明 |
|---|---|
| `from` | 起点节点 id |
| `to` | 终点节点 id |
| `handle` | 起点的出口；只有 `condition` 节点需要写（= 某个 case 的 `id`），不写即默认出口 |

一个节点可以有多条出边（并行），也可以连回前面的节点（回环，用于多轮对话）。

## 4. 变量引用

在提示词、输出消息、工具参数等文本里，用 **`{{#节点id.变量名#}}`** 引用别的节点产出的值：

```yaml
user_prompt: "研究想法：{{#ask.user_input#}}\n上一轮结论：{{#analyst.output#}}"
```

- 必须是 `节点id.变量名` 两段，节点 id 只含字母、数字、下划线。`{{#query#}}`、`{{#histories#}}` 这种一段的写法在工作流里**不生效**。
- 引用的节点必须存在；编译会检查，写错报 `reference_unknown_node`。
- **绕过编译直接改线上时**，写错的引用不会报错，而是原样留在提示词里不被替换——这正是先 `compile` 的理由。
- 只有文本型参数会做替换（提示词、`output` 的消息、工具参数、表单标签）。`code` 节点的代码、`report`
  的模板里出现 `{{#…#}}` 只是普通文字。
- 每类节点能被引用的变量见第 6 章各节的「可引用变量」。

## 5. 条件分支

`condition` 节点用 `cases` 声明分支，**case 的 `id` 就是出边的 `handle`**；从上到下第一个为真的 case
胜出；全都不成立时走默认出口（出边不写 `handle`）。**没有条件的 case 永远为真**——要用它当兜底就放在最后，
通常直接用默认出口更清楚。

```yaml
  - id: gate
    type: condition
    cases:
      - id: stop
        logic: and                 # and / or，缺省 and
        conditions:
          - left: ask.user_input   # 左值：节点id.变量名（不加 {{# #}}）
            op: regex
            value: '^\s*结束\s*$'  # 右值：常量
      - id: long
        conditions:
          - left: ask.user_input
            op: contains
            ref: analyst.output    # 右值也可以引用变量；value 与 ref 二选一
edges:
  - {from: gate, to: end_1, handle: stop}
  - {from: gate, to: summarize, handle: long}
  - {from: gate, to: analyst}      # 默认出口
```

case 字段：

<!-- keys:case -->
| 字段 | 说明 |
|---|---|
| `id` | 分支 id，同一节点内唯一；出边的 `handle` 写它 |
| `logic` | 多个条件如何组合 |
| `conditions` | 条件列表；不写或为空即「无条件」分支 |

条件字段：

<!-- keys:condition -->
| 字段 | 说明 |
|---|---|
| `left` | 左值，`节点id.变量名` |
| `op` | 比较符，见下表 |
| `value` | 右值常量（数字写成 `3` 或 `"3"` 均可）；`is_empty` / `is_not_empty` 不需要 |
| `ref` | 右值引用另一个变量，`节点id.变量名`；与 `value` 二选一 |
| `id` | 条件 id；不写自动生成。从线上拉回的工作流已有 id，保留它可让 `diff` 不报差异 |
| `label` | 画布上显示的左值名；不写自动生成 |

组合逻辑：

<!-- values:logic -->
| 取值 | 含义 |
|---|---|
| `and` | 全部条件成立 |
| `or` | 任一条件成立 |

比较符（平台只认这些）：

<!-- values:op -->
| 比较符 | 含义 |
|---|---|
| `equals` | 等于 |
| `not_equals` | 不等于 |
| `greater_than` | 大于（数值） |
| `less_than` | 小于（数值） |
| `greater_than_or_equal` | 大于等于（数值） |
| `less_than_or_equal` | 小于等于（数值） |
| `contains` | 包含 |
| `not_contains` | 不包含 |
| `is_empty` | 为空 |
| `is_not_empty` | 不为空 |
| `starts_with` | 以…开头 |
| `ends_with` | 以…结尾 |
| `regex` | 正则匹配 |

比较语义（照引擎实现）：

- 左值、右值都按文本比较；`equals` / `contains` / `starts_with` 等区分大小写。
- `is_empty` 只在值为空字符串或不存在时成立——**只有空格的文本不算空**。要把空白也当空，再加一条
  `op: regex, value: '^\s*$'`，两条用 `logic: or`。
- `regex` 是 Python 正则的**部分匹配**（`re.search`）：`结束` 能匹配「我想结束了」。要整句匹配就写 `^…$`。
- `greater_than` 等四个数值比较会把两边转成数字；左值不是数字时**运行时报错**，不是判为假。比较大模型的
  输出前，先让它只输出数字，或用 `code` 节点清洗。

## 6. 节点参数

每节的参数表与平台节点模板逐键一致。「编译必查」= 编译要求该值非空，空着会报错；其余参数不写就用
默认值。**大多数参数保持默认即可**，表里标「通常不改」的只在用户明确要求时才动。

### `start`

工作流入口，每个工作流**恰好一个**。

<!-- params:start -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `guide_word` | 否 | `""` | 开场白，会话开始时展示 |
| `guide_question` | 否 | `[]` | 开场引导问题，字符串列表 |
| `user_info` | 否 | — | 平台注入的当前用户信息（只读变量，通常不改） |
| `current_time` | 否 | — | 平台注入的当前时间（只读变量，通常不改） |
| `chat_history` | 否 | `10` | 可引用的历史对话轮数 |
| `preset_question` | 否 | `[]` | 预置问题列表 |
| `custom_variables` | 否 | `[]` | 自定义全局变量（通常在网页上配） |

可引用变量：`{{#start_1.user_info#}}`、`{{#start_1.current_time#}}`、`{{#start_1.chat_history#}}`。
预置问题与自定义变量的引用名以网页画布的变量选择器为准。

### `input`

等待用户输入；运行到这里会暂停（`flow run` 返回 `waiting_input`）。`tab` 取值：

<!-- tabs:input -->
| `tab` | 含义 |
|---|---|
| `dialog_input` | 对话框输入：用户输入一段文字（可带文件）。**默认、最常用** |
| `form_input` | 表单输入：按 `form_input` 里声明的字段逐项填写 |

<!-- params:input -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `user_input` | 否 | — | 对话框模式下用户输入的文字（变量，不用写值） |
| `user_input_file` | 否 | `true` | 对话框是否允许上传文件 |
| `file_parse_mode` | 否 | `extract_text` | 上传文件的解析方式（通常不改） |
| `dialog_files_content` | 否 | — | 上传文件解析出的文字（变量） |
| `dialog_files_content_size` | 否 | `15000` | 文件文字的最大字符数 |
| `dialog_file_accept` | 否 | `all` | 允许的文件类型（通常不改） |
| `dialog_image_files` | 否 | — | 上传的图片（变量） |
| `dialog_file_paths` | 否 | — | 上传文件的路径（变量） |
| `recommended_questions_flag` | 否 | `false` | 是否为用户生成推荐问题 |
| `recommended_llm` | 否 | `""` | 生成推荐问题用的模型 id，写成数字（开启推荐问题时才需要） |
| `recommended_system_prompt` | 否 | 模板默认 | 生成推荐问题的提示词 |
| `recommended_history_num` | 否 | `2` | 生成推荐问题参考的历史轮数 |
| `form_input` | 否 | `[]` | 表单模式的字段列表，见下 |

表单字段（`tab: form_input` 时）每项写全这六个键（编译**不检查**表单字段内部的键，写错不会报错，只会在运行或
网页上表现异常）：

```yaml
  - id: form
    type: input
    tab: form_input
    params:
      form_input:
        - {key: variety, type: text, value: 品种, required: true, multiple: false, options: []}
        - {key: report, type: text, value: 研报正文, required: false, multiple: false, options: []}
```

- `key` 是变量名（字母、数字、下划线），引用写 `{{#form.variety#}}`，条件里写 `left: form.variety`；
  无终端续跑时 `--input-json '{"variety": "螺纹钢", "report": "…"}'` 的键也是它。
- **`value` 是字段的显示名**（不是默认值）。`type` 只用 `text`（文本，长短都行）或 `select`（下拉，选项写进
  `options`）；文件类字段结构复杂，请在网页上配。
- 要走「为空」分支的字段不要设 `required: true`，否则网页端会先要求用户填写。

可引用变量：对话框模式 `{{#ask.user_input#}}`、`{{#ask.dialog_files_content#}}`、
`{{#ask.dialog_image_files#}}`、`{{#ask.dialog_file_paths#}}`；表单模式 `{{#ask.<字段key>#}}`。

### `output`

向用户展示一条消息；也可以停下来让用户确认或选择。

<!-- params:output -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `message` | 否 | `{msg: "", files: []}` | 要展示的消息；`msg` 里可用变量引用 |
| `output_result` | 否 | `{type: "", value: ""}` | 交互方式：`type: ""` 仅展示；`input` 让用户改写后继续；`choose` 让用户选一项并按选项分支（选项结构复杂，请在网页上配） |

写法：`message: {msg: "结论：{{#analyst.output#}}", files: []}`。多行消息可以用 YAML 的 `|` 块写法。

`output_result.type` 为 `""` 时只展示、**不暂停**，紧接着走出边；所以「提示用户补充 → 连回 `input`」会在展示
提示后直接停在那个 `input` 上等用户。末尾的 `output` 可以直接连 `end`，消息会完整展示。

可引用变量：交互模式下用户的回应 `{{#reply.output_result#}}`。

### `llm`

调用一次大模型。`tab` 取值：

<!-- tabs:llm -->
| `tab` | 含义 |
|---|---|
| `single` | 单次：用提示词调用一次。**默认** |
| `batch` | 批量：对 `batch_variable` 里的每个变量各调用一次 |

<!-- params:llm -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `batch_variable` | 否 | `[]` | 批量模式：逐个处理的变量列表（`节点id.变量名`），条数须与 `output` 一致 |
| `model_id` | **是** | `""` | 模型 id，**写成数字**（如 `7`，与网页保存的形状一致，否则 `diff` 会报差异）。向用户要，**不要编造**；编译不检查该模型是否存在。样例里的 `0` 是占位，运行前必须换掉 |
| `temperature` | 否 | `1` | 采样温度，0～2 |
| `system_prompt` | 否 | `""` | 系统提示词，可用变量引用；长文本用 `!include` |
| `user_prompt` | 否 | `""` | 用户提示词，可用变量引用。**实际上总该写** |
| `image_prompt` | 否 | `[]` | 作为图片输入的变量列表 |
| `output_user` | 否 | `true` | 是否把模型输出直接展示给用户。后面另有 `output` 节点展示结果时设 `false`，否则同一段内容会出现两次 |
| `output` | 否 | `[]` | 批量模式：每个输出变量 `{key, label}`；单次模式不用写 |

可引用变量：单次模式 `{{#analyst.output#}}`；批量模式 `{{#analyst.<output[i].key>#}}`。

### `agent`

带工具 / 知识库 / 数据库的智能体。`tab` 取值：

<!-- tabs:agent -->
| `tab` | 含义 |
|---|---|
| `single` | 单次。**默认** |
| `batch` | 批量，同 `llm` |

<!-- params:agent -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `batch_variable` | 否 | `[]` | 批量模式的变量列表，同 `llm` |
| `model_id` | **是** | `""` | 模型 id，写成数字（须是平台「助手模型」里的模型）。向用户要 |
| `temperature` | 否 | `1` | 采样温度 |
| `system_prompt` | 否 | 模板默认 | 系统提示词 |
| `user_prompt` | 否 | `""` | 用户提示词 |
| `chat_history_flag` | 否 | `{flag: true, value: 50}` | 是否带历史对话及条数 |
| `image_prompt` | 否 | `""` | 图片输入变量 |
| `knowledge_id` | 否 | `{type: knowledge, value: []}` | 可检索的知识库，`value` 为 `[{key: 知识库id, label: 名称}]`；id 向用户要 |
| `user_auth` | 否 | `false` | 检索时是否按当前用户权限过滤 |
| `sql_agent` | 否 | `{open: false, …}` | 数据库查询配置，**含口令，绝不写进 YAML**，在网页上配 |
| `tool_list` | 否 | `[]` | 可调用的工具（建议在网页上选） |
| `output_user` | 否 | `true` | 是否把输出展示给用户 |
| `output` | 否 | `[]` | 批量模式的输出变量 |

可引用变量：同 `llm`，单次模式 `{{#agent_1.output#}}`。

### `qa_retriever`

在问答库里找最相近的问答对。

<!-- params:qa_retriever -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `user_question` | 否 | `""` | 用作检索的问题，`节点id.变量名`（不加 `{{# #}}`） |
| `qa_knowledge_id` | 否 | `[]` | 问答库列表 `[{key: 问答库id, label: 名称}]`；id 向用户要 |
| `score` | 否 | `0.8` | 相似度阈值，0.01～0.99 |
| `retrieved_result` | 否 | — | 检索到的答案（变量） |

可引用变量：`{{#qa_1.retrieved_result#}}`（没命中时为空，可接 `condition` 用 `is_empty` 分流）。

### `rag`

文档知识库问答：检索后由大模型作答。

<!-- params:rag -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `user_question` | 否 | `[]` | 问题变量列表 `[节点id.变量名, …]`，每个问题各答一次 |
| `knowledge` | 否 | `{type: knowledge, value: []}` | 知识库，`value` 为 `[{key: 知识库id, label: 名称}]`；id 向用户要 |
| `metadata_filter` | 否 | `{}` | 元数据过滤（在网页上配） |
| `advanced_retrieval_switch` | 否 | `{}` | 高级检索设置（在网页上配） |
| `retrieved_result` | 否 | — | 检索结果（提示词里用） |
| `system_prompt` | 否 | 模板默认 | 系统提示词 |
| `user_prompt` | 否 | 模板默认 | 用户提示词。**必须包含 `{{#<本节点id>.retrieved_result#}}`**（检索结果放进提示词的位置），否则每次运行都会报错 |
| `model_id` | **是** | `""` | 作答模型 id，写成数字。向用户要 |
| `temperature` | 否 | `1` | 采样温度 |
| `output_user` | 否 | `true` | 是否展示给用户 |
| `output_user_input` | 否 | `[]` | 每个问题的答案变量 `[{key, label}]`。**条数必须与 `user_question` 一致**——少写编译不报，运行时出错 |

可引用变量：`{{#rag_1.<output_user_input[i].key>#}}`。

### `knowledge_retriever`

只检索知识库，不作答（把检索结果交给后面的节点）。

<!-- params:knowledge_retriever -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `user_question` | 否 | `[]` | 问题变量列表 `[节点id.变量名, …]` |
| `knowledge` | 否 | `{type: knowledge, value: []}` | 知识库，同 `rag` |
| `metadata_filter` | 否 | `{}` | 元数据过滤（在网页上配） |
| `advanced_retrieval_switch` | 否 | `{}` | 高级检索设置（在网页上配） |
| `retrieved_result` | 否 | `[]` | 每个问题的结果变量 `[{key, label}]`。**条数必须与 `user_question` 一致**——少写编译不报，运行时出错 |

可引用变量：`{{#kr_1.<retrieved_result[i].key>#}}`。

### `report`

按 Word 模板生成报告。

<!-- params:report -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `report_info` | 否 | `{}` | 报告模板信息，指向平台上的模板文件。**只能在网页上配**——YAML 里保留占位，推送后让用户在网页上选模板 |

没有可引用变量。

### `code`

执行一段 Python。**平台默认关闭此节点**，用之前先看第 2 章。

<!-- params:code -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `code_input` | 否 | 两个示例入参 | 入参列表，每项 `{key: 参数名, type: input | ref, label: "", value: 常量或 节点id.变量名}` |
| `code` | 否 | 示例函数 | 代码，必须定义 `def main(<入参…>) -> dict:`，返回的字典键与 `code_output` 一致 |
| `code_output` | 否 | 两个示例出参 | 出参列表，每项 `{key: 键名, type: str}` |

```yaml
  - id: calc
    type: code
    params:
      code_input:
        - {key: text, type: ref, label: "", value: ask.user_input}
      code: |
        def main(text: str) -> dict:
            return {"length": str(len(text))}
      code_output:
        - {key: length, type: str}
```

可引用变量：`{{#calc.<code_output[i].key>#}}`。

### `condition`

条件分支，写法见第 5 章。

<!-- params:condition -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|
| `condition` | 否 | `[]` | 由 `cases` 编译生成，**不要直接写**；`cases` 与它同时出现会报 `cases_conflict` |

没有可引用变量。

### `end`

结束节点。可以有多个；它是唯一不需要出边的节点。没有参数，也没有可引用变量。

<!-- params:end -->
| 参数键 | 编译必查 | 默认 | 说明 |
|---|---|---|---|

## 7. 工具节点

`tool` 节点调用平台上注册的工具（一个 HTTP 接口）。三步：

1. **注册**（工具还不存在时）：准备一份描述接口的 OpenAPI 文件（YAML 或 JSON），每个操作带唯一的
   `operationId`，然后 `bisheng --json flow tool register openapi.yaml`。结果列出每个操作的标识。
   接口要鉴权时，鉴权信息让用户在网页上补，不要写进 OpenAPI 文件。
2. **查标识**：`bisheng --json flow tool list` 列出可用的工具类型及其操作。
3. **引用**：

```yaml
  - id: lookup
    type: tool
    tool: get_quote              # operationId；重名时写成 工具类型名/operationId
    params:
      symbol: "{{#ask.user_input#}}"   # 参数名 = OpenAPI 里的参数名
```

- 工具参数的值**只能是字符串**（可含变量引用）；空字符串的参数不会发给接口。
- 可引用变量：`{{#lookup.output#}}`（接口的返回）。
- 引用了不存在的工具报 `unknown_tool`，同名操作不止一个报 `ambiguous_tool`（改用 `类型名/operationId`）。

## 8. 常见报错与改法

`compile` 的 `details.issues` 里每条都有 `kind` 或 `code`，常见的：

| 报错 | 含义 | 改法 |
|---|---|---|
| `unknown_type` | 节点类型不存在 | 用第 6 章的 12 类或 `tool` |
| `unknown_param` | 参数键不在该类节点模板里 | 对照第 6 章该节的参数表 |
| `unknown_field` / `unknown_edge_field` / `unknown_top_level_field` | 字段名写错 | 对照第 3 章 |
| `reference_unknown_node` | `{{#x.y#}}` 里的节点 id 不存在 | 改成真实的节点 id |
| `condition_invalid` | 比较符或条件字段不合法 | 对照第 5 章的比较符表 |
| `missing_start` | 没有 `start` 节点 | 补一个 `type: start` 并连到第一个节点 |
| `node_unconnected` | 非 `end` 节点没有出边 | 补一条 `edges`，或确实该结束就连到 `end` |
| `edge_target_missing` / `edge_source_missing` | 连线两端的节点 id 不存在 | 改成真实的节点 id |
| `param_empty` | 必查参数为空（如 `model_id`） | 向用户要模型 id |
| `param_missing` / `param_shape` | 引擎要读的参数缺失或形状不对 | 对照第 6 章该参数的取值形状 |
| `edge_handle_unknown` | 条件节点出边的 `handle` 不是任何 case 的 id | 改成某个 case 的 `id`，或删掉 `handle` 走默认出口 |
| `batch_output_mismatch` | 批量模式下 `batch_variable` 与 `output` 条数不一致 | 两边补齐 |
| `reference_malformed` / `reference_without_node` | 变量引用写法不对（如缺 `节点id.`） | 改成 `{{#节点id.变量名#}}` |
| `tool_unresolved` | 工具节点缺 `tool_key` | 用 `tool:` 引用已注册的工具 |
| `unknown_tool` / `ambiguous_tool` | 工具引用不对 | `flow tool list` 查标识 |

`push` / `run` 的平台报错会带人读的提示（`error.next_step`），照着做；属于第 2 章情形的，停下来问用户。
