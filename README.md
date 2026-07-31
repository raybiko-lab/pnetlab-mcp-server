# pnetlab-mcp-server

[![中文](https://img.shields.io/badge/README-中文-blue.svg)](README.md) [![English](https://img.shields.io/badge/README-English-lightgrey.svg)](README.en.md)

一个 [MCP](https://modelcontextprotocol.io) 服务器,让 Claude(以及其他 LLM
Agent)可以通过自然语言**程序化控制 PNETLab v6** 网络实验环境 -- 创建拓扑、
添加节点、连线、推送配置、启动节点并读取状态、驱动设备控制台、注入链路故障。

它复刻了 [`axiom-works-ai/eveng-mcp-server`](https://github.com/axiom-works-ai/eveng-mcp-server)
的工具集,但对接的是 **PNETLab v6** 的会话级 API,而不是 v6 已经移除的经典
EVE-NG API。

## 为什么需要它

PNETLab v6(6.0.0+)是一次 Laravel 重写,保留了旧 EVE-NG 引擎(仍在 `/api/`
下),但是:

- **删除**了经典登录(`/api/auth/login`)、`/api/status`、`/api/labs/`、
  `/api/folders/`、`/api/users/`。
- **把实验操作迁移到**会话级 API:`/api/labs/session/*`。
- **替换**了登录方式,改为 Laravel 端点:`POST /store/public/auth/login/login`。

所以 `eveng-mcp-server`(走经典 API)在 v6 上**无法工作**。本服务器是针对
PNETLab 6.0.0-100 逆向工程并实际验证过的。

## 安装

```bash
pip install -e .
```

安装后提供 `pnetlab-mcp-server` 命令。

## 配置

设置环境变量(服务器在第一次调用工具时才会懒加载登录):

| 变量 | 示例 | 用途 |
|---|---|---|
| `PNETLAB_HOST` | `http://192.168.231.128` | PNETLab v6 地址 |
| `PNETLAB_USERNAME` | `mcp` | Agent 使用的工作账号(请用**专用**账号) |
| `PNETLAB_PASSWORD` | `pnet` | 工作账号密码 |
| `PNETLAB_VIEWER_USERNAME` | `admin` | (可选)在浏览器里查看的账号 |
| `PNETLAB_VIEWER_PASSWORD` | `pnet` | 查看账号密码 |

**为什么要专用工作账号?** v6 每个用户账号只能有一个活跃的实验会话。如果
Agent 和你的浏览器都用 `admin`,`open_lab` 会报 `20039 "sandbox already
exists"`。给 Agent 一个独立账号(例如 `mcp`,admin 角色),你的浏览器就自由了。

**在浏览器里实时查看。** 当设置了 `PNETLAB_VIEWER_*` 时,`open_lab` 会自动把
查看账号加入 Agent 的实验会话 -- 于是两者共享同一个实时拓扑。Agent 打开实验
后,只要用查看账号登录 PNETLab 网页 UI 并打开该实验(或访问
`/legacy/topology`):你就能看到 Agent 的拓扑,刷新即可看到它的改动。
`join_viewer` 用于在你先打开了浏览器时重新加入;`close_lab` 也会让查看账号退出。

### Claude Code(`.claude.json`)

```json
{
  "mcpServers": {
    "pnetlab": {
      "command": "pnetlab-mcp-server",
      "env": {
        "PNETLAB_HOST": "http://192.168.231.128",
        "PNETLAB_USERNAME": "mcp",
        "PNETLAB_PASSWORD": "pnet",
        "PNETLAB_VIEWER_USERNAME": "admin",
        "PNETLAB_VIEWER_PASSWORD": "pnet"
      }
    }
  }
}
```

添加后重启 Claude Code。

## 工具

### 列表与模板
| 工具 | 作用 |
|---|---|
| `list_templates()` | 列出已安装节点模板,结构化返回 `{template, name, installed}`(`installed=false` 表示镜像缺失) |
| `list_images(template)` | 列出某模板可用磁盘镜像(如 `mikrotik-7.23.2`)+ 默认镜像。**建 QEMU 节点前必查** |
| `get_template(template)` | 取模板完整可编辑选项(镜像、qemu 版本、各字段默认值) |
| `list_network_types()` | 列出网络类型(bridge、pnet0..9、ovs) |

### 实验会话
| 工具 | 作用 |
|---|---|
| `open_lab(path)` | 按文件名打开实验,如 `2pc_1sw.unl`(无前导斜杠);自动加入查看账号 |
| `close_lab()` | 离开当前实验会话(同时让查看账号退出) |
| `join_viewer()` | (重新)把查看账号加入 Agent 的会话,以便在浏览器里查看 |
| `get_lab(compact?)` | 信息 + 完整拓扑 + 节点状态。`compact=true` 去噪(省略空 style、第二控制台等),大拓扑推荐 |
| `get_node_status()` | 每个节点的运行状态(0=已停止、1=启动中、2=运行中) |

### 节点
| 工具 | 作用 |
|---|---|
| `add_node(type, template, name, image?, ram?, ...)` | 添加节点。**默认自动套用模板自带默认值**(image/ram/cpu/qemu_*/console/config_script),与 GUI 建的节点一致;只需 `add_node("qemu","mikrotik","R1")` 即可建出可启动节点。显式传参覆盖模板;`template_defaults=false` 关闭 |
| `update_node(node_id, image?, ram?, ...)` | 修改已有节点字段(镜像、内存、qemu_* 等)。改 image/ram 下次启动生效 |
| `connect_nodes(src_id, src_if, dest_id, dest_if)` | 两接口点对点链路。精简返回 `{network_id, src, dst}`(用 network_id 操作链路) |
| `start_node(node_id?, check?)` | 启动节点(不传 id 启动全部)。`check=true` 启动后轮询状态,崩溃则返回诊断 |
| `stop_node(node_id?)` | 停止节点(不传 id 停止全部) |
| `delete_node(node_id)` | 删除节点(需先停止) |
| `push_config(node_id, config)` | 推送启动配置(下次启动生效) |
| `node_console(node_id)` | 取控制台 host:port。实际下发命令请用下面的控制台工具 |

### 链路与故障注入
| 工具 | 作用 |
|---|---|
| `delete_link(network_id)` | 删除链路(断开两端接口) |
| `set_link_state(network_id, up)` | 链路 Up/Down(接口 suspend,等同拔线;仅对运行中节点生效)。状态可从 `get_lab` 的 `suspend` 字段读 |
| `set_link_quality(network_id, loss?, delay?, jitter?, bandwidth?)` | 注入丢包/延迟/抖动/限速(双向;仅对运行中节点生效) |

### 控制台交互(免手写 telnet)
| 工具 | 作用 |
|---|---|
| `run_command(node_id, command, timeout?, wait_for?, username?, password?)` | 高层:自动登录 + 发命令 + 读到提示符返回输出。内置 IAC 协商、ANSI/回显清理。会话复用 |
| `console_send(node_id, text, newline?)` | 低层:发送原始文本(首次自动登录)。配合 `console_read` 做交互 |
| `console_read(node_id, timeout?)` | 读取控制台待输出 |
| `console_close(node_id?)` | 关闭一个/全部控制台会话 |

## 重要注意事项(v6 专属)

1. **每个账号一个会话。** v6 每个账号只能有一个活跃实验会话。Agent 必须用
   专用账号(例如 `mcp`),不能用你浏览时用的账号。配置 `PNETLAB_VIEWER_*`,
   `open_lab` 会自动把你的浏览账号加入 Agent 的会话 -- 两者共享同一个实时拓扑
   (见上文"配置")。
2. **所有 `/api/labs/session/*` 调用都用 JSON body。** 表单编码的 body 会被
   静默丢弃,表现为 `40000 "missing required fields"`。
3. **`open_lab` 的 path 没有前导斜杠** -- 是 `"2pc_1sw.unl"`,不是
   `"/2pc_1sw.unl"`。
4. **`add_node` 默认自动套用模板。** 建节点时会自动拉取模板自带默认值
   (image/ram/cpu/qemu_arch/qemu_nic/qemu_options/qemu_version/console/config_script)
   并填入,与 GUI 建的节点完全一致 -- 所以 `add_node("qemu","mikrotik","R1")`
   就能直接建出可启动、可连控制台的节点,通常无需先 `list_images`。显式传入的
   字段覆盖模板默认;`template_defaults=false` 可关闭。想换镜像时再用
   `list_images` 查可用项,传 `image=...` 覆盖。
5. **`console` 默认 `telnet`。** PNETLab 通过控制台端口是否在监听来判断节点
   "运行中"(状态 2)。`console` 为空时不会起 `qemu_wrapper_telnet` 转发器,
   端口不监听,于是状态恒为 0、控制台也连不上 -- 看起来像"启动即崩",实际
   节点在跑。`add_node` 已默认 `console="telnet"`;如需 ssh/winbox/http 等
   显式传入即可。
6. `open_lab` 会在服务器上创建/复用一个沙盒文件(`labs<name>.unl`);沙盒在
   首次打开时为空。`close_lab` 释放会话绑定但保留沙盒文件(重新打开没问题)。
7. 删除节点前必须先 `stop_node`。
8. 不带 id 的 `start_node()` / `stop_node()` 会遍历实验的节点 id(API 的
   null-id "全部" 路径不可靠)。
9. **链路质量/状态在接口层。** v6 的 network 不带 quality 字段;丢包/延迟/
   suspend 都是接口级。本服务器的链路工具会自动找到链路两端的接口并下发。
   仅对运行中节点生效(数据存库 + 实时下发)。
10. **控制台会话复用。** 一个 PNETLab 控制台端口同时只服务一个 telnet 客户端,
    `run_command`/`console_send`/`console_read` 按节点复用会话;用完调
    `console_close`。RouterOS 默认 `admin`/空密码,可在 `run_command` 里传
    `username`/`password` 覆盖。

## 架构

```
LLM agent  ──MCP/stdio──►  pnetlab-mcp-server  ──HTTP/JSON──►  PNETLab v6
                              (本仓库)                            /store/public/auth/login/login  (登录)
                                                                  /api/labs/session/*             (实验操作)
                                                                  /api/list/templates|networks    (列表)
                                                                  /api/labs/session/interfaces/*  (链路质量/状态)
                                                                  telnet <host>:<console port>    (控制台)
```

`client.py` 是经过验证的 v6 API 客户端(含自实现 telnet 控制台,telnetlib 在
Python 3.13+ 已移除);`server.py` 把它封装为 MCP 工具。
