我现在要新增功能，把算法做可复用改进。我增加了一系列action_type作为最基础的ros节点动作来组成更上层的动作：
action_type = {
    "waiting_navigation_status", // 上肢进入等待导航移动的安全姿势
    "navigation_to_pose", // 从当前位置导航到目标位置
    "grab_object", // 去预设的后部平台的固定点位抓物件
    "turn_waist", // 转腰到一定角度，转腰时候有时候需要让手臂规避障碍物
    "put_object", // 把手上抓的东西放到预设固定点位，并放手，有是需要缩回或者抬起
    "scan", // 双手扫码动作
    "cv_detect", // 视觉检测瓶子，输出临时点位
}
这些动作都是ros server，里面发送的参数如下：
request = {
    "op": "call_service",
    "service": "/navigation_status",
    "args": {
        "action": "waiting_navigation_status"
    }
}
request = {
    "op": "call_service",
    "service": "/navigation_status",
    "args": {
        "action": "navigation_to_pose",
        "navigation_pose": "shelf" // 导航点位：货架
    }
}
request = {
    "op": "call_service",
    "service": "/get_strawberry_service",
    "args": {
        "action": "turn_waist",
        "angle": "180", // 转到背面，转腰限位[-180, 180]
        "obstacle_avoidance": True // 规避障碍物参数，提示机器人转身过程中手臂可能会遇到障碍物
    }
}
request = {
    "op": "call_service",
    "service": "/get_strawberry_service",
    "args": {
        "action": "grab_object",
        "strawberry": {
            "type": "glass_bottle_1000",
            "target_pose": "shelf_temp_1000_001", // 机器人抓取固定点位：货架暂存区1
            "hand": "right" // right：左手抓，left：右手抓，both：双手抓
        }
    }
}
request = {
    "op": "call_service",
    "service": "/get_strawberry_service",
    "args": {
        "action": "turn_waist",
        "angle": "0" // 转到背面，转腰限位[-180, 180]
        "obstacle_avoidance": True // 规避障碍物参数，提示机器人转身过程中手臂可能会遇到障碍物
    }
}
request = {
    "op": "call_service",
    "service": "/get_strawberry_service",
    "args": {
        "action": "put_object",
        "strawberry": {
            "type": "glass_bottle_1000",
            "target_pose": "back_temp_1000_001", // 机器人抓取固定点位：机器人后背暂存区1000ml专用位1
            "hand": "right", // right：左手抓，left：右手抓，both：双手抓
            "safe_pose": "preset" // 执行完成后手臂移动到安全位置，下个动作不会撞到瓶子。preset：预设值，立正状态 / lift_up：抬起 / retract：缩回
        }
    }
}
request = {
    "op": "call_service",
    "service": "/get_strawberry_service",
    "args": {
        "action": "scan",
    }
}
request = {
    "op": "call_service",
    "service": "/get_strawberry_service",
    "args": {
        "action": "cv_detect",
    }
}
返回值：瓶盖临时固定点位，瓶子类型


这些action_type节点可以组成CMD_TYPE，这些是一些可以复用的机器人小技能：
CMD_TYPES = {
    "PICK_UP": "拿取东西到平台", # 把多个瓶子放到机器人后面平台上
    "PUT_TO": "放下东西到某个地方", # 把机器人后面平台上瓶子放到指定地方
    "TAKE_BOTTOL_FROM_SP_TO_SP": "把样品瓶从某处拿到某处", # 把某个暂存区（非机器人后部平台）的瓶子放到另一个暂存区
    "SCAN_QRCODE": "去样品瓶暂存区扫描瓶子二维码，并放入机器人后部平台上", # 扫完码会收到工厂调度系统发来的样品瓶类型和唯一ID
    "ENTER_ID": "录入样品瓶ID",
    "POUR_SEPARATE": "开始倾倒分液操作",
    "PIPETTE_SEPARATE": "开始移液枪分液操作",
    "BOTTLE_GET": "获取样品瓶信息" # 获取所有样品瓶，获取指定样品瓶，获取某个点位（暂存区）的样品瓶
}
其中CMD_TYPES会被被外部发送的HTTP JSON消息调用，并获得必要的参数，我们需要把收到JSON消息的参数转化输入到CMD_TYPES的function中，以下是具体的CMD_TYPES和HTTP JSON消息：
CMD_TYPES = "PICK_UP"：
HTTP JSON
{
    "header": {"seq": 203,"stamp": {"secs": 1698000020, "nsecs": 345678},"frame_id": "base_link"},
    "cmd_id": "PICK_UP_001",
    "cmd_type": "PICK_UP",
    "params": {
        "target_params": [
            {
                "bottle_id": "glass_bottle_1000_001" // 唯一物件编号
            }，
            {
                "bottle_id": "glass_bottle_1000_002"
            }
        ],
        "timeout": 10.0  // 操作超时时间（秒）
    },
    "extra": {}
}
注意每个"bottle_id"在我们的代码中有自己对应的一些参数，以下皆是：
"params_object": {
    "bottle_id": "glass_bottle_1000_001", // 扫码过的物件编号
    "object_type": "glass_bottle_1000",
    "hand": "left_hand", // left_hand 左手，right_hand 右手，both_hand 双手
    "target_pose": "position_001", // 物件位置(相对于机器人面前的固定点位)
    "navigation_pose": "sheft",  // 相对于机器人底盘的目标导航点位名（必须已存在）
    "timeout": 10.0  // 操作超时时间（秒）
}
注意每个"target_pose"在我们的代码中有自己对应的一些参数，以下皆是：
"params_target_pose": {
    "count": 0 // 目前瓶子数量，上限max_num=2
}

在收到HTTP消息后，我们的代码需要执行对应的处理程序，这也是需要补全的部分，在这里CMD_TYPES = "PICK_UP"由以下之前提到的action_type组合而成（伪代码）：
最佳化算法：上面的HTTP JSON CMD_TYPES = "PICK_UP"会输入很多的bottle_id，我们需要把这些bottle拿到指定位置。像"params_object"提到的一样，bottle_id会有其他参数，这些参数中，navigation_pose是导航的不同地点，机器人前往后，每个navigation_pose会有4个target_pose放置不同"object_type"的bottle，每个target_pose有2个放置位置，可以放2个bottle，现在有多个bottle_id的输入，每个bottle_id会有不同或相同的navigation_pose，target_pose，不同的navigation_pose会导致机器人前往不同的地方，要使机器人走最少的navigation_pose，拿到最多的bottle。输出一个list："task_list"={"navigation_pose": {"bottle_id", "bottle_id"}, "navigation_pose": {"bottle_id", "bottle_id"}}
for all navigation_pose in task_list: {
    robot.send_service_request("/navigation_status", "waiting_navigation_status")
    robot.send_service_request("/navigation_status", "navigation_to_pose", args={"navigation_pose" = "shelf"})
    for all bottle_id in task_list[navigation_pose]: {
        robot.send_service_request("/get_strawberry_service", "grab_object", args={"type": "glass_bottle_1000", "target_pose": "shelf_temp_1000_001", "hand": "right"})
        robot.send_service_request("/get_strawberry_service", "turn_waist", args={"angle": "180", "obstacle_avoidance": True})
        robot.send_service_request("/get_strawberry_service", "put_object", args={"type": "glass_bottle_1000", "target_pose": "shelf_temp_1000_001", "hand": "right", "safe_pose": "preset"})
        robot.send_service_request("/get_strawberry_service", "turn_waist", args={"angle": "180", "obstacle_avoidance": True})
    }
}
注意事项：输入的bottel_id可能会超过target_pose存储上限，如果超过会回传无法拿取的bottle_id。

CMD_TYPES = "PUT_TO"：
HTTP JSON
{
    "header": {"seq": 204,"stamp": {"secs": 1698000030, "nsecs": 567890},"frame_id": "base_link"},
    "cmd_id": "PUT_TO_001",
    "cmd_type": "PUT_TO",
    "params": {
        "release_params": [
            {
                "bottle_id": "glass_bottle_1000_001", // 扫码过的物件编号
                "release_pose": "worktable_temp_001" // 放置点位（绑定导航点位）：worktable_temp_001（工作台暂存区）
            }
            {
                "bottle_id": "glass_bottle_1000_002",
                "release_pose": "worktable_temp_001"
            }
        ],
        "timeout": 10.0
    },
    "extra": {}
}
在收到HTTP消息后，我们的代码需要执行对应的处理程序，这也是需要补全的部分，在这里CMD_TYPES = "PICK_UP"由以下之前提到的action_type组合而成（伪代码）：
for all navigation_pose in task_list: {
    robot.send_service_request("/navigation_status", "waiting_navigation_status")
    robot.send_service_request("/navigation_status", "navigation_to_pose", args={"navigation_pose" = "shelf"})
    for all bottle_id in task_list[navigation_pose]: {
        robot.send_service_request("/get_strawberry_service", "turn_waist", args={"angle": "180", "obstacle_avoidance": True})
        robot.send_service_request("/get_strawberry_service", "grab_object", args={"type": "glass_bottle_1000", "target_pose": "back_temp_1000_001", "hand": "right"})
        robot.send_service_request("/get_strawberry_service", "turn_waist", args={"angle": "180", "obstacle_avoidance": True})
        robot.send_service_request("/get_strawberry_service", "put_object", args={"type": "glass_bottle_1000", "target_pose": "shelf_temp_1000_001", "hand": "right", "safe_pose": "preset"})
    }
}

CMD_TYPES = "TAKE_BOTTOL_FROM_SP_TO_SP"：
HTTP JSON
{
    "header": {
        "seq": 205,"stamp": {"secs": 1698000040, "nsecs": 901234},"frame_id": "map"  // 跨点位操作基于全局地图坐标系
    },
    "cmd_id": "TAKEBOTTLE_TO_001",
    "cmd_type": "TAKE_BOTTOL_FROM_SP_TO_SP",
    "params": {
        "target_params": [ // 取瓶信息
            {
                "bottle_id": "glass_bottle_1000_001", // 扫码过的物件编号，样品瓶唯一标识（用于追踪）
            }，
            {
                "bottle_id": "glass_bottle_1000_002",
            }
        ],
        "release_params": [
            {
                "bottle_id": "glass_bottle_1000_001", // 扫码过的物件编号
                "release_pose": "worktable_temp_001", // 放置点位（绑定导航点位）：worktable_temp_001（工作台暂存区）
            }，
            {
                "bottle_id": "glass_bottle_1000_002",
                "release_pose": "worktable_temp_002",
            }
        ],
        "timeout": 60.0  // 整体超时（含导航+操作）
    },
    "extra": {}
}
在收到HTTP消息后，我们的代码需要执行对应的处理程序，这也是需要补全的部分，在这里CMD_TYPES = "PICK_UP"由以下之前提到的action_type组合而成（伪代码）：
相当于多个"PICK_UP"和"PUT_TO"的组合，需要使用新的"task_list"最佳化算法：输入分为"PICK_UP"的"target_params"和"PUT_TO"的"release_params"，拿起和放下以及暂存位的规则和原优化算法一样，由于需要多次执行，要尽量少做导航移动去navigation_pose，所以每次"PICK_UP"时要尽可能把机器人后平台暂存区放满，release_pose相同的瓶子尽可能一起放平台中。生成一个"task_list2[]"=[{"navigation_pose": {"bottle_id", "bottle_id"}, "navigation_pose": {"bottle_id", "bottle_id"}}, {"target_params": {"bottle_id", "bottle_id"}, "target_params": {"bottle_id", "bottle_id"}}, {"navigation_pose": {"bottle_id", "bottle_id"}, "navigation_pose": {"bottle_id", "bottle_id"}}, {"target_params": {"bottle_id", "bottle_id"}, "target_params": {"bottle_id", "bottle_id"}}]
for x in task_list2: {
    for all navigation_pose in x[0]: {
        robot.send_service_request("/navigation_status", "waiting_navigation_status")
        robot.send_service_request("/navigation_status", "navigation_to_pose", args={"navigation_pose" = "shelf"})
        for all bottle_id in x[0][navigation_pose]: {
            robot.send_service_request("/get_strawberry_service", "grab_object", args={"type": "glass_bottle_1000", "target_pose": "shelf_temp_1000_001", "hand": "right"})
            robot.send_service_request("/get_strawberry_service", "turn_waist", args={"angle": "180", "obstacle_avoidance": True})
            robot.send_service_request("/get_strawberry_service", "put_object", args={"type": "glass_bottle_1000", "target_pose": "shelf_temp_1000_001", "hand": "right", "safe_pose": "preset"})
            robot.send_service_request("/get_strawberry_service", "turn_waist", args={"angle": "180", "obstacle_avoidance": True})
        }
    }
    for all navigation_pose in x[1]: {
        robot.send_service_request("/navigation_status", "waiting_navigation_status")
        robot.send_service_request("/navigation_status", "navigation_to_pose", args={"navigation_pose" = "shelf"})
        for all bottle_id in x[1][navigation_pose]: {
            robot.send_service_request("/get_strawberry_service", "turn_waist", args={"angle": "180", "obstacle_avoidance": True})
            robot.send_service_request("/get_strawberry_service", "grab_object", args={"type": "glass_bottle_1000", "target_pose": "back_temp_1000_001", "hand": "right"})
            robot.send_service_request("/get_strawberry_service", "turn_waist", args={"angle": "180", "obstacle_avoidance": True})
            robot.send_service_request("/get_strawberry_service", "put_object", args={"type": "glass_bottle_1000", "target_pose": "shelf_temp_1000_001", "hand": "right", "safe_pose": "preset"})
        }
    }
}

CMD_TYPES = "SCAN_QRCODE"：
HTTP JSON
{
    "header": {"seq": 204,"stamp": {"secs": 1698000030, "nsecs": 567890},"frame_id": "base_link"},
    "cmd_id": "PUT_TO_001",
    "cmd_type": "PUT_TO",
    "params": {
        "release_params": [
            {
                "bottle_id": "glass_bottle_1000_001", // 扫码过的物件编号
                "release_pose": "worktable_temp_001" // 放置点位（绑定导航点位）：worktable_temp_001（工作台暂存区）
            }
            {
                "bottle_id": "glass_bottle_1000_002",
                "release_pose": "worktable_temp_001"
            }
        ],
        "timeout": 10.0
    },
    "extra": {}
}
CMD_TYPES = "ENTER_ID"：
HTTP JSON
{
    "header": {
        "seq": 206,"stamp": {"secs": 1698000050, "nsecs": 234567},"frame_id": "camera_link"  // 基于相机坐标系
    },
    "cmd_id": "SCAN_QRCODE_ENTER_ID_001",
    "cmd_type": "SCAN_QRCODE_ENTER_ID",
    "params": {
        "bottle_id": "glass_bottle_1000_001", // 扫码过的物件编号
        "type": "glass_bottle_1000",  // 目标类型：glass_bottle_1000（1L玻璃瓶）/glass_bottle_250（250ml玻璃瓶）
        "timeout": 5.0  // 扫描超时（秒）
    },
    "extra": {}
}
在收到HTTP消息后，我们的代码需要执行对应的处理程序，这也是需要补全的部分，在这里CMD_TYPES = "SCAN_QRCODE"由以下之前提到的action_type组合而成（伪代码）：
robot.send_service_request("/navigation_status", "waiting_navigation_status")
robot.send_service_request("/navigation_status", "navigation_to_pose", args={"navigation_pose" = "scan_table"})
robot.send_service_request("/get_strawberry_service", "grab_object", args={"type": "scan_gun", "target_pose": "scan_gun", "hand": "right"})
while robot.send_service_request("/get_strawberry_service", "cv_detect")回传"target_pose": "detect_temp_1000_001": {
    if 对应的target_pose没有满: {
        robot.send_service_request("/get_strawberry_service", "grab_object", args={"type": "glass_bottle_1000", "target_pose": "detect_temp_1000_001", "hand": "left"})
        robot.send_service_request("/get_strawberry_service", "scan")
        robot.send_service_request("/get_strawberry_service", "put_object", args={"type": "glass_bottle_1000", "target_pose": "shelf_temp_1000_001", "hand": "left", "safe_pose": "preset"})
    }
}

CMD_TYPES = "BOTTLE_GET"：
HTTP JSON
{
    "header": {
        "seq": 206,"stamp": {"secs": 1698000050, "nsecs": 234567},"frame_id": "camera_link"  // 基于相机坐标系
    },
    "cmd_id": "BOT_GET_001",
    "cmd_type": "BOTTLE_GET",
    "params": { // 没有参数输入则输出全部瓶子以及其详细信息
        "bottle_id": "glass_bottle_1000_001", // (可选)扫码过的物件编号，显示对应物件的相关参数
        "pose_name": "shelf_001" // （可选）物件点位和暂存区点位，显示在这个点位下的所有瓶子ID以及相关参数
    },
    "detail_params": True, // (可选) 默认为True，是否仅显示id或在显示详细信息
    "extra": {}
}
查看所有内容，并在原有的代码基础上编写出新代码。


1. 错误码要修改更新
2. 扫码抓瓶子可能会超过当前暂存区上限，需要有放回去，取消的功能
