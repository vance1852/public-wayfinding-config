# 联合导览终端的分区配置交接

本目录提供现场联调用的合成合作方记录。记录中的机构、地点、旅客和标识均为虚构。每份记录可单独使用。

## 合作方报文

终端归属和可显示的语言、图形能力由 `terminals` 提供。每条已存在的有向路线由先经过的点到最后经过的点排列，`site` 是设置位置，共址设备可分管不同方向。时刻使用同一活动日的分钟数，时段左闭右开。每条路线的无障碍属性和某一终端可显示的图形是不同字段。`operators` 给出本次联调里负责相关内容的机构。`stop_entry_at` 指停引导新进入者的时点，路线票载的终止时刻同时约束在途指引。
设备使用 `PUT /screens/{id}/configuration` 接收一套配置，接受可回复 202。`received`、`installed`、`displaying` 的状态回报中，`revision` 标识所报告内容，`usable_until` 是该内容获准展示的右端点。终端失联时用相同 `screen` 标识报告线路事件。现场若确实停用了终端，会通过独立回报报告。业务资料中的改动发布不是设备安装回执。

## 本地联调

使用 Python 3.11 的标准库。现场联调环境固定为 Python 3.11.2。启动本地资料端后保持该进程运行，默认只监听回环地址的 8090 端口。

```sh
python partner.py fixtures/two-directions-and-closure.json
```

资料端按路径回放记录中的响应。接口 `GET /fixture/facts` 返回起始事实，`GET /fixture/events` 返回按到达时间排列的原始事件。同一到达时刻的事件按记录中的顺序出现。`GET /fixture/journal` 返回收到的合作方请求，含未匹配及已收件但主动断开连接的请求。重启资料端会从该份记录开头开始。

`exchanges` 每条只使用一次，按方法、路径和列出的正文键值及请求头匹配最早尚未使用的记录。路径中的花括号匹配一个标识段。`delay_ms` 是合作方回包延迟，`drop` 表示合作方收到请求后直接关闭连接。未匹配的请求返回 404。记录字段可随新的联调资料调整。

在同一目录用 `python -m unittest -v` 核对资料载入和合作方接口，包括已记录的状态码、回包、断连及接收记录。

## 本地配置分发服务（`dispatch/`）

在合作方资料之上提供一套可本机运行的配置分发控制端与屏端接入，只用 Python 3.11 标准库。三家维护方仍各管各的屏和内容，控制端只负责核对路线相容性并下发可用内容。

### 模块

| 文件 | 职责 |
| --- | --- |
| `dispatch/engine.py` | 纯判定引擎：票券/封闭/终端状态、相容性、期望配置渲染，无网络 IO |
| `dispatch/service.py` | 控制端 HTTP 服务：发布/撤回/封闭/回报录入、下发、现场查询 |
| `dispatch/screen.py` | 屏端模拟器：收配置、本地按时效与能力显示、暂停/恢复联网、到期停引导 |
| `dispatch/driver.py` | 联调回放：从合作方资料端拉事实与事件，经记录交换下发并汇总 |
| `dispatch/demo.py` | 一键端到端演示：控制端+全部屏端，覆盖断网、重连、到期检查点 |
| `dispatch/runserver.py` | 独立启动控制端 |

### 现场安全规则（引擎实现要点）

* **入口相容性**：入口只有在该方向连续后续指路点都有终端能给出相容指引时才引导入内；任一环节只能停止引导时，入口屏改显停止引导。
* **离线承诺**：终端离线时，离线前已承诺且未到期的有效时段仍计入相容性；超过最后收到的 `usable_until` 必须本地退到停止引导。
* **重连纪元**：重连后旧安装/显示回执一律冻结为历史证据，不能恢复已失效路线；路线恢复只依据重连后重新下发的配置与新回报。
* **老屏兜底**：能力不足的老屏在路线仍可通行时保留适用的旧指引（译文按语言子集呈现）；表达不了有效绕行（如缺图形）或路段封闭时显示停止引导。
* **在途与入口分离**：路线级 `stop_entry_at` 只停新进入者，中间指路点继续给在途旅客走完/退出提示，入口回撤不清在途内容。
* **临时封闭**：越过日常时段并压住预发内容；在线终端立即撤下经过封闭段的引导；封闭时无法证明已撤下的离线入口进入 `/signs` 人工标牌清单。
* **单向隔离**：改动只作用于指定方向，反方向与无关区域继续服务。

### 控制端 API（默认 `http://127.0.0.1:8080`）

| 方法与路径 | 用途 |
| --- | --- |
| `POST /events` | 批量录入事件（publish/withdraw/closure/device_report/offline/reconnect/field_disabled/query） |
| `POST /publish`、`/withdraw`、`/closures`、`/reports` | 单条录入 |
| `POST /clock` | 推进虚拟时钟（分钟），触发到期/解封重算 |
| `GET /directions/{route}` | 何时可开始引导、阻塞点、欠哪些终端确认、在途提示 |
| `GET /screens` | 每屏的收到/可用/正在显示/失联后最后可证实状态（含冻结快照） |
| `GET /signs` | 封闭时无法证明撤下、必须人工补挂标牌的入口及解除情况 |
| `GET /overview` | 票券、封闭段、全部决策历史、下发日志的总览 |

### 运行方式

```sh
# 1. 启动合作方资料端（保持运行）
python partner.py fixtures/two-directions-and-closure.json

# 2. 一键端到端演示（自带控制端与全部屏端，无需先起 partner）
python -m dispatch.demo fixtures/two-directions-and-closure.json
python -m dispatch.demo fixtures/pass-through-after-entry-ends.json

# 3. 对接合作方资料端回放，并保留控制端供查询
python -m dispatch.driver fixtures/two-directions-and-closure.json \
    --partner http://127.0.0.1:8090 --serve 8080 --output /tmp/replay.json

# 4. 仅启动控制端（屏端地址可选，缺省只登记下发意图）
python -m dispatch.runserver fixtures/two-directions-and-closure.json --port 8080
```

屏端模拟器（每块屏独立端口）的现场操作接口：`PUT /screens/{id}/configuration` 收配置，`POST /local/offline|online|disable` 控制联网与停用，`POST /local/clock` 推进本地时钟，`GET /local/display` 查看此刻正在给旅客的指示。

### 测试

```sh
python -m unittest -v          # 含合作方资料端与 dispatch 全部 32 个用例
```

