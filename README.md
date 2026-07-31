# pnetlab-mcp-server

[![中文](https://img.shields.io/badge/README-中文-blue.svg)](README.md) [![English](https://img.shields.io/badge/README-English-lightgrey.svg)](README.en.md)

一个 [MCP](https://modelcontextprotocol.io) 服务器,让 Claude(以及其他 LLM
Agent)可以通过自然语言**程序化控制 PNETLab v6** 网络实验环境 —— 创建拓扑、
添加节点、连线、推送配置、启动节点并读取状态。

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
查看账号加入 Agent 的实验会话 —— 于是两者共享同一个实时拓扑。Agent 打开实验
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

| 工具 | 作用 |
|---|---|
| `list_templates` | 列出已安装的节点模板(其中的 key 即 add_node 用的模板 id) |
| `list_network_types` | 列出网络类型(bridge、pnet0..9、ovs) |
| `open_lab(path)` | 按文件名打开实验,如 `2pc_1sw.unl`(无前导斜杠);自动加入查看账号 |
| `close_lab()` | 离开当前实验会话(同时让查看账号退出) |
| `join_viewer()` | (重新)把查看账号加入 Agent 的会话,以便在浏览器里查看 |
| `get_lab()` | 信息 + 完整拓扑(节点/网络/连接)+ 节点状态 |
| `get_node_status()` | 每个节点的运行状态(0=已停止、1=启动中、2=运行中) |
| `add_node(type, template, name, ...)` | 添加节点;返回新节点 id + 控制台端口 |
| `connect_nodes(src_id, src_if, dest_id, dest_if)` | 两个节点接口之间的点对点链路 |
| `start_node(node_id?)` | 启动一个节点,不传 id 则启动全部 |
| `stop_node(node_id?)` | 停止一个节点,不传 id 则停止全部 |
| `delete_node(node_id)` | 删除节点(需先停止) |
| `push_config(node_id, config)` | 推送启动配置(下次启动时生效) |
| `node_console(node_id)` | 获取节点的 telnet/SSH host:port,用于 CLI 访问 |

## 重要注意事项(v6 专属)

1. **每个账号一个会话。** v6 每个账号只能有一个活跃实验会话。Agent 必须用
   专用账号(例如 `mcp`),不能用你浏览时用的账号。配置 `PNETLAB_VIEWER_*`,
   `open_lab` 会自动把你的浏览账号加入 Agent 的会话 —— 两者共享同一个实时拓扑
   (见上文"配置")。
2. **所有 `/api/labs/session/*` 调用都用 JSON body。** 表单编码的 body 会被
   静默丢弃,表现为 `40000 "missing required fields"`。
3. **`open_lab` 的 path 没有前导斜杠** —— 是 `"2pc_1sw.unl"`,不是
   `"/2pc_1sw.unl"`。
4. `open_lab` 会在服务器上创建/复用一个沙盒文件(`labs<name>.unl`);沙盒在
   首次打开时为空。`close_lab` 释放会话绑定但保留沙盒文件(重新打开没问题)。
5. 删除节点前必须先 `stop_node`。
6. 不带 id 的 `start_node()` / `stop_node()` 会遍历实验的节点 id(API 的
   null-id "全部" 路径不可靠)。

## 架构

```
LLM agent  ──MCP/stdio──►  pnetlab-mcp-server  ──HTTP/JSON──►  PNETLab v6
                              (本仓库)                            /store/public/auth/login/login  (登录)
                                                                  /api/labs/session/*             (实验操作)
                                                                  /api/list/templates|networks    (列表)
```

`client.py` 是经过验证的 v6 API 客户端;`server.py` 把它封装为 MCP 工具。
