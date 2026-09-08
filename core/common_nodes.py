"""
跨项目通用的业务节点定义。

三层节点的区别（编辑器面板按这个分栏显示）：

    builtin  —— 流程引擎内置，引擎自带处理器，任何项目都一样。
                见 core/flow_engine.py::BUILTIN_NODE_TYPE_SCHEMAS
                （condition / set_variable / delay / parallel / ...）

    common   —— 本文件。**界面和参数跨项目统一**，但处理器由各项目自己实现，
                因为底层对接的对象不同（比如同样是"记录状态步骤"，多机器人项目
                写的是 ParallelTaskStateMachine，单机器人项目写的是 TaskStateMachine）。

    project  —— 项目专有，各项目 node_handlers.py 里的 NODE_TYPE_SCHEMAS。
                同一个概念在不同项目参数可能完全不同（KAIAO 的导航要选走廊中间点
                策略，WRC 的导航不需要），所以刻意不强行统一，只在面板上分栏隔开。

放到 common 层的前提是"参数表能统一"。如果两个项目对同一概念的参数确实不同，
应该老老实实各自留在 project 层，硬凑成同名节点反而更容易用错——
本文件出现之前，update_step 就是两个项目同名不同参，看着是一个块，
换个项目少一个字段。

反面例子就在手边：KAIAO 的导航要选走廊中间点策略、要挂离架后的姿态校正，
这些参数在别的项目没有对应概念，所以它老实留在 project 层叫 kaiao_navigate，
没有硬塞进这里的 navigate。

**通用节点是"项目可选实现"，不是"所有项目都必须有"。**
项目在 adapter 的 ``common_nodes`` 里声明自己支持哪几个，编辑器只显示声明过的。
不这么做的话，面板上会出现本项目没有处理器的块，拖上去要等跑起来才报"未知节点类型"。
"""

from typing import Any, Dict

#: options_source 的取值由 network/flow_api_server.py 在返回 schema 时按项目实时注入：
#:   "steps"  <- adapter["step_options"]    项目的任务步骤枚举
#:   "robots" <- adapter["robot_options"]   项目配置里的机器人列表
#:   "poses"  <- robot_config.json 的导航点位
#: 项目没提供对应数据时，前端把该字段降级成自由文本输入，不会卡住编辑。
COMMON_NODE_TYPE_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "navigate": {
        "label": "导航到点位",
        "category": "机器人动作",
        "fields": [
            {"name": "pose", "type": "select", "label": "目标点位", "required": True,
             "options_source": "poses",
             "extra_options": ["{{p3_n}}", "{{p3_k}}", "{{robot_id}}"],
             "hint": "选配置里的点位，或动态槽位 {{p3_n}} / {{p3_k}}（find_slot 写入的变量）"},
            {"name": "robot_id", "type": "select", "label": "机器人",
             "options_source": "robots",
             "extra_options": ["{{robot_id}}"],
             "hint": "指定 robot_a / robot_b，或选 {{robot_id}} 跟随命令入参；留空也走命令里的 robot_id"},
            {"name": "skip_if_at_pose", "type": "checkbox",
             "label": "已在该点位时跳过导航", "default": True},
            {"name": "timeout", "type": "number", "label": "超时秒数", "default": 180},
        ],
        "outputs": ["success", "failure"],
    },
    "send_operation": {
        "label": "发送操作动作",
        "category": "机器人动作",
        "fields": [
            {"name": "call_type", "type": "select", "label": "调用方式", "required": True,
             "options": ["action", "service"], "default": "action"},
            {"name": "service", "type": "select", "label": "Service 通道",
             "options_source": "services",
             "hint": "仅 call_type=service 时生效"},
            {"name": "task", "type": "text", "label": "任务名(task)", "required": True},
            {"name": "area", "type": "text", "label": "区域(area)",
             "hint": "发给机器人的 area，如 point_1 / point_4；不是导航点位名 P1。动态槽位可填 {{p3_n}}"},
            {"name": "extra_params", "type": "json", "label": "附加参数(extra_params)"},
            {"name": "robot_id", "type": "select", "label": "机器人",
             "options_source": "robots",
             "extra_options": ["{{robot_id}}"],
             "hint": "指定 robot_a / robot_b，或选 {{robot_id}} 跟随命令入参；留空也走命令里的 robot_id"},
            {"name": "timeout", "type": "number", "label": "超时秒数", "default": 1200},
        ],
        "outputs": ["success", "failure"],
    },
    "update_step": {
        "label": "记录状态步骤",
        "category": "状态记录",
        "fields": [
            {"name": "step", "type": "select", "label": "步骤名称", "required": True,
             "options_source": "steps", "allow_free_text": True,
             "hint": "写入任务状态机，供 GET_TASK_STATE 查询回显"},
            {"name": "message", "type": "text", "label": "描述信息"},
            {"name": "robot_id", "type": "select", "label": "机器人",
             "options_source": "robots",
             "extra_options": ["{{robot_id}}"],
             "hint": "多机器人项目才需要指定；也可选 {{robot_id}} 跟随命令入参，留空即可"},
        ],
        "outputs": ["default"],
    },
}
