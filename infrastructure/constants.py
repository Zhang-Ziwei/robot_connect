from enum import IntEnum

# ── 项目模式默认值 ───────────────────────────────────────────────────────────
# 可选值：TJSH / ATC / WRC / AJL / WAIC / KAIAO / ALL
# 切换项目时修改此处即可；Docker 部署时可通过 /config/robot_config.json
# 中的 active_project 字段在运行期覆盖（优先级更高）。
DEFAULT_ACTIVE_PROJECT: str = "WAIC"

# ── PLC保持寄存器地址映射 ─────────────────────────────────────────────────────
class PLCHoldingRegisters:
    OPEN_LID_STATE = 0       # 4001: 0-未就绪,1-准备就绪,2-工作中,3-工作完成
    CLEAN_STATE = 1          # 4002: 0-未就绪,1-准备就绪,2-工作中,3-工作完成
    DETECT_STATE = 2         # 4003: 0-未就绪,1-准备就绪,2-放料位移完成,3-工作中,4-请求取走第一个样品,5-请求取走第二个样品
    CLOSE_LID_STATE = 3      # 4004: 0-未就绪,1-准备就绪,2-工作中,3-工作完成
    HOLDING_REG_COUNT = 4

# PLC线圈地址映射
class PLCCoils:
    OPEN_START = 0           # 1: 开盖启动（状态1置位→状态2复位）
    OPEN_FINISH = 1          # 2: 开盖完成后取瓶（状态3置位→状态1复位）
    CLOSE_START = 2          # 3: 关盖启动（状态1置位→状态2复位）
    CLOSE_FINISH = 3         # 4: 关盖完成后取瓶（状态3置位→状态1复位）
    DETECT_DISPENSE = 4      # 5: 检测放料位移（状态1置位→状态2复位）
    DETECT_START = 5         # 6: 检测启动（状态2置位→状态3复位）
    DETECT_PICK = 6          # 7: 检测取料位移（状态4置位→状态5复位）
    DETECT_FINISH = 7        # 8: 检测取料完成（状态5置位→状态1复位）
    CLEAN_START = 8          # 9: 清洗启动（状态1置位→状态2复位）
    CLEAN_FINISH = 9         # 10: 清洗取料完成（状态3置位→状态1复位）
    COIL_COUNT = 16

# 机器人类型
class RobotType:
    ROBOT_A = "robot_a"
    ROBOT_B = "robot_b"
    ROBOT_C = "robot_c"

# ROS 话题名称
class ROSTopic:
    # 导航状态（所有机器人通用）
    NAVIGATION_STATUS = "/navigation_status"
    # 上肢关节状态
    UPPER_LIMB_JOINT_STATES = "/zj_humanoid/upperlimb/joint_states"
    # 手部关节状态
    HAND_JOINT_STATES = "/zj_humanoid/hand/joint_states"
    # 左手手指压力传感器
    FINGER_PRESSURES_LEFT = "/zj_humanoid/hand/finger_pressures/left"
    # 右手手指压力传感器
    FINGER_PRESSURES_RIGHT = "/zj_humanoid/hand/finger_pressures/right"
    # 导航控制
    NAVIGATION_CONTROL = "/navigation_control"
    # 导航 Action Topics（rosbridge topic 方式，通过 订阅/发布这些 topic 来实现 Action 调用）
    NAVIGATION_ACTION_GOAL     = "/zj_humanoid/navigation/navigation/goal"
    NAVIGATION_ACTION_FEEDBACK = "/zj_humanoid/navigation/navigation/feedback"
    NAVIGATION_ACTION_RESULT   = "/zj_humanoid/navigation/navigation/result"
    NAVIGATION_ACTION_CANCEL   = "/zj_humanoid/navigation/navigation/cancel"
    NAVIGATION_ACTION_STATUS   = "/zj_humanoid/navigation/navigation/status"
    # 电池状态topic
    BATTERY_STATE = "/zj_humanoid/robot/battery_info"
    # 充电状态 Topic
    CHARGING_STATUS_TOPIC = "/zj_humanoid/chassis/charge_state"
    # 头部相机RGB图像JPG
    HEAD_CAMERA_RGB_IMAGE_JPG = "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed"
    # 头部相机深度图像JPG
    HEAD_CAMERA_DEPTH_IMAGE_JPG = "/zj_humanoid/sensor/realsense_head/depth/image_rect_raw/compressed"
    # 头部深度相机参数
    HEAD_CAMERA_DEPTH_PARAMS = "/zj_humanoid/sensor/realsense_head/depth/camera_info"
    # 机器人运动状态
    ROBOT_MOTION_STATE = "/zj_humanoid/navigation/odom_info" # /zj_humanoid/navigation/odom_info 导航组的参数，以这个为准，但是导航没开就不会有值，/zj_humanoid/chassis/odom_info 底盘的参数
    # 导航定位：订阅定位状态（module_common_msgs/ModuleStatus，status==2 表示成功）
    LOCATION_CODE = "/zj_humanoid/perception/location_code"
    # 机器人任务 Action Topics（/robot_task/*，标准 actionlib topic 协议）
    # Goal / Result / Feedback 对应 RobotAction.action:
    #   Goal:     navi_types/RobotTaskTypes robot_task_types, string task, string area, string extra_params
    #   Result:   bool success, string error_msg, string return_params
    #   Feedback: string status, string current_params
    ROBOT_TASK_ACTION_GOAL     = "/robot_task/goal"
    ROBOT_TASK_ACTION_FEEDBACK = "/robot_task/feedback"
    ROBOT_TASK_ACTION_RESULT   = "/robot_task/result"
    ROBOT_TASK_ACTION_CANCEL   = "/robot_task/cancel"
    ROBOT_TASK_ACTION_STATUS   = "/robot_task/status"

# ROS服务名称
class ROSService:
    # 机器人A主服务
    STRAWBERRY_SERVICE = "/get_strawberry_service"
    # 机器人B主服务
    CHEM_PROJECT_SERVICE = "/chem_project_service"
    # 导航地图配置
    NAVIGATION_MAP_CONFIG = "/zj_humanoid/navigation/set_map"
    # 获取当前导航地图信息
    NAVIGATION_GET_MAP_INFO = "/zj_humanoid/navigation/get_cur_map_info"
    # 导航定位：调用重定位服务（naviai_localization_msgs/Lio）
    RELOC = "/zj_humanoid/perception/reloc"

# ROS话题消息类型
class ROSTopicMessageType:
    # 导航状态
    NAVIGATION_STATUS = "navi_types/NavigationStatus"
    # 上肢关节状态
    UPPER_LIMB_JOINT_STATES = "sensor_msgs/JointState"
    # 手部关节状态
    HAND_JOINT_STATES = "sensor_msgs/JointState"
    # 左手手指压力传感器
    FINGER_PRESSURES_LEFT = "hand/PressureSensor"
    # 右手手指压力传感器
    FINGER_PRESSURES_RIGHT = "hand/PressureSensor"
    # 导航控制
    NAVIGATION_CONTROL = "std_msgs/String"
    # 导航 Action Topics（topic 方式）
    NAVIGATION_ACTION_GOAL     = "navigation/NavigationActionGoal"
    NAVIGATION_ACTION_FEEDBACK = "navigation/NavigationActionFeedback"
    NAVIGATION_ACTION_RESULT   = "navigation/NavigationActionResult"
    NAVIGATION_ACTION_STATUS   = "actionlib_msgs/GoalStatusArray"
    NAVIGATION_ACTION_CANCEL   = "actionlib_msgs/GoalID"
    # 标准电池消息类型
    BATTERY_STATE = "sensor_msgs/BatteryState"
    # 导航定位：定位状态消息类型（status==2 表示成功）
    LOCATION_CODE = "module_common_msgs/ModuleStatus"
    # 充电消息类型
    CHARGING_STATUS = "chassis_msgs/PowerStatusStamped"
    # 头部相机RGB图像（压缩格式）
    HEAD_CAMERA_RGB_IMAGE_JPG = "sensor_msgs/CompressedImage"
    # 头部相机深度图像（压缩格式）
    HEAD_CAMERA_DEPTH_IMAGE_JPG = "sensor_msgs/CompressedImage"
    # 头部深度相机参数
    HEAD_CAMERA_DEPTH_PARAMS = "sensor_msgs/CameraInfo"
    # 机器人运动状态
    ROBOT_MOTION_STATE = "nav_msgs/Odometry"
    # 机器人任务 Action Topics 消息类型（RobotAction.action → navi_types 包）
    ROBOT_TASK_ACTION_GOAL     = "navi_types/RobotActionActionGoal"
    ROBOT_TASK_ACTION_FEEDBACK = "navi_types/RobotActionActionFeedback"
    ROBOT_TASK_ACTION_RESULT   = "navi_types/RobotActionActionResult"
    ROBOT_TASK_ACTION_STATUS   = "actionlib_msgs/GoalStatusArray"
    ROBOT_TASK_ACTION_CANCEL   = "actionlib_msgs/GoalID"

# ROS服务消息类型
class ROSServiceMessageType:
    # 导航地图配置
    NAVIGATION_MAP_CONFIG = "map_server_msgs/SetMap"
    # 获取当前导航地图信息
    NAVIGATION_GET_MAP_INFO = "map_server_msgs/GetCurMapInfo"


# ==================== actionlib GoalStatus 状态集合 ====================
# 对应 actionlib_msgs/GoalStatus.status (uint8)
# 0=PENDING, 1=ACTIVE, 2=PREEMPTED, 3=SUCCEEDED, 4=ABORTED,
# 5=REJECTED, 6=PREEMPTING, 7=RECALLING, 8=RECALLED, 9=LOST

# "机器人仍在执行"状态集合 —— 用于检测导航是否在运行中
# PENDING/ACTIVE/PREEMPTING/RECALLING 都表示 Goal 尚未进入终态
ACTIONLIB_GOAL_STATUS_ACTIVE = {0, 1, 6, 7}

# ==================== 导航 Action 外部设备状态定义 ====================
# 这些数值对应机器人发回的 uint8 value 字段（feedback / result）
# 若机器人固件升级修改了编号，只需在此处调整，业务代码无需改动。
class NavigationState(IntEnum):
    """
    导航状态（与机器人 NavigationState.msg 的 uint8 value 对齐）
    """
    NONE      = 0   # Idle / 未启动
    ACTIVE    = 1   # Active
    RUNNING   = 2   # Running
    ARRIVED   = 3   # Arrived
    CANCELING = 4   # Canceling
    CANCELLED = 5   # Cancelled
    SUCCEEDED = 6   # Succeeded
    FAILED    = 7   # Failed
    ERROR     = 8   # Error
    ABORTED   = 9   # Aborted


class NavigationTaskType(IntEnum):
    """
    导航任务类型（对应 Navigation.action 的 TaskType.value）
    """
    ROUTINE   = 0   # 常规导航
    CHARGE    = 1   # 充电导航
    EMERGENCY = 2   # 紧急导航


# NavigationState 成功/失败分类 —— 用于业务逻辑判定
# 如果机器人固件修改了语义，这里集中调整即可
# 注：cancelled 属于业务层主动行为（我们发的 cancel），与失败同等看待，归入 FAILED
NAV_STATE_SUCCEEDED = {NavigationState.ARRIVED, NavigationState.SUCCEEDED}
NAV_STATE_FAILED    = {
    NavigationState.FAILED,
    NavigationState.ERROR,
    NavigationState.ABORTED,
    NavigationState.CANCELING,
    NavigationState.CANCELLED,
}


# 机器人B ROS服务名称（固定不变的特殊服务）
class ROSServiceRobotB:
    HALFBODY_CHEMICAL_SERVICE = "/get_halfbodychemical_service"

# 机器人C ROS服务名称（本地部署示例）
class ROSServiceRobotC:
    MAIN_SERVICE = "/robot_c_main_service"

# 机器人类型与主ROS服务的映射关系
# - robot_a 使用 STRAWBERRY_SERVICE
# - robot_b 使用 CHEM_PROJECT_SERVICE
# - 导航状态 NAVIGATION_STATUS 所有机器人通用
ROBOT_MAIN_SERVICE_MAP = {
    RobotType.ROBOT_A: ROSService.CHEM_PROJECT_SERVICE,
    RobotType.ROBOT_B: ROSService.CHEM_PROJECT_SERVICE,
    "robot_a": ROSService.CHEM_PROJECT_SERVICE,
    "robot_b": ROSService.CHEM_PROJECT_SERVICE,
}

def get_main_ros_service(robot_id: str) -> str:
    """
    根据机器人ID获取对应的主ROS服务名称
    
    参数:
        robot_id: 机器人ID (如 "robot_a", "robot_b")
    
    返回:
        对应的ROS服务名称
        
    示例:
        get_main_ros_service("robot_a") -> "/get_strawberry_service"
        get_main_ros_service("robot_b") -> "/chem_project_service"
    """
    return ROBOT_MAIN_SERVICE_MAP.get(robot_id, ROSService.STRAWBERRY_SERVICE)
    
# 机器人配置
# 
# 配置优先级：
# 1. 外部配置文件 /config/robot_config.json (Docker挂载目录，最高优先级)
# 2. 外部配置文件 ./robot_config.json (当前目录)
# 3. 以下默认配置 DEFAULT_ROBOT_CONFIGS (最低优先级)
#
# 使用方法：
#   from constants import get_robot_configs
#   configs = get_robot_configs()  # 自动加载外部配置或使用默认配置
#
# 默认配置（当没有外部配置文件时使用）
DEFAULT_ROBOT_CONFIGS = {
    "robot_b": {
        "host": "0.0.0.0",
        "port": "9090",
        "robot_type": RobotType.ROBOT_B
    }
}

def get_robot_configs():
    """
    获取机器人配置（优先使用外部配置文件）
    
    返回:
        机器人配置字典
        - 如果配置文件存在且有启用的机器人，返回启用的机器人配置
        - 如果配置文件存在但所有机器人都禁用，返回空字典 {}
        - 如果没有配置文件，返回默认配置
    """
    try:
        try:
            from infrastructure.config_loader import get_robot_configs as load_external_configs
        except ImportError:
            from config_loader import get_robot_configs as load_external_configs
        external_configs = load_external_configs()
        
        # None 表示没有找到配置文件，使用默认配置
        # {} 空字典表示配置文件存在但所有机器人都禁用了
        if external_configs is None:
            print("ℹ️  未找到机器人配置，使用默认配置")
            return DEFAULT_ROBOT_CONFIGS
        
        # 配置文件存在，返回启用的机器人（可能是空字典）
        if not external_configs:
            print("ℹ️  所有机器人都已禁用，不连接任何机器人")
            return {}
        
        # 将外部配置转换为内部格式（添加robot_type对象）
        result = {}
        for robot_id, config in external_configs.items():
            robot_type_str = config.get("robot_type", robot_id)
            # 映射robot_type字符串到RobotType类属性
            robot_type = getattr(RobotType, robot_type_str.upper(), robot_type_str)
            result[robot_id] = {
                "host": config.get("host"),
                "port": config.get("port"),
                "robot_type": robot_type,
                "navigation_map": config.get("navigation_map"),
            }
        return result
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️  加载机器人配置失败: {e}")
    
    return DEFAULT_ROBOT_CONFIGS

# 兼容旧代码：直接使用ROBOT_CONFIGS变量（静态默认配置）
ROBOT_CONFIGS = DEFAULT_ROBOT_CONFIGS

# 模块名称映射
MODULE_NAMES = [
    "Open Lid Module", 
    "Clean Module", 
    "Detect Module", 
    "Close Lid Module"
]

# Modbus配置
MODBUS_PORT = 502  # 使用非特权端口避免权限问题

# HTTP服务器配置
HTTP_SERVER_PORT = 8090  # HTTP服务器端口（注意：8081被Docker容器占用）

# WebSocket服务器配置
WEBSOCKET_SERVER_ENABLED = True  # 是否启用WebSocket服务器
WEBSOCKET_SERVER_PORT = 8091  # WebSocket服务器端口
WEBSOCKET_SERVER_HOST = "0.0.0.0"  # WebSocket服务器监听地址
WEBSOCKET_SSL_ENABLED = False  # 是否启用SSL（wss协议）
WEBSOCKET_SSL_CERT_FILE = None  # SSL证书文件路径
WEBSOCKET_SSL_KEY_FILE = None  # SSL密钥文件路径
WEBSOCKET_HEARTBEAT_INTERVAL = 30  # 心跳间隔（秒）
WEBSOCKET_MAX_CLIENTS = 100  # 最大客户端连接数
WEBSOCKET_DEFAULT_CLIENT_ID = "robot-connect-client"  # 默认客户端ID（固定ID模式）

# 机器人WebSocket连接重连配置（用于连接ROS Bridge）
ROBOT_WS_RECONNECT_MAX_ATTEMPTS = None  # 监听器最大重连次数，None表示无限重试
ROBOT_WS_RECONNECT_INTERVAL = 5  # 重连间隔（秒）
ROBOT_WS_PING_INTERVAL = 20  # WebSocket ping间隔（秒）
ROBOT_WS_PING_TIMEOUT = 30  # WebSocket ping超时（秒）

# 机器人B单独动作接口开关
# 设置为True时，可通过HTTP接口单独调用机器人B的各个动作
ENABLE_ROBOT_B_ACTIONS = True

# ==================== 导航地图自动下发配置 ====================
# 优先级：robot_config.json 的 navigation_map 段 > 以下默认值
# 配置示例：EMPTY_BOTTLE_AREA_SPLIT合并默认值）。
# 下面的 AUTO_SET_NAVIGATION_MAP_ON_CONNECT 等常量仅作为后备默认值。
AUTO_SET_NAVIGATION_MAP_ON_CONNECT = True
NAVIGATION_MAP_SET_WAIT_IDLE_TIMEOUT = 60
NAVIGATION_MAP_SET_SERVICE_TIMEOUT = 30
NAVIGATION_MAP_SKIP_IF_SAME = True

# ==================== 导航定位（reloc）默认配置 ====================
# 地图设置成功后是否自动触发重定位，默认关闭
AUTO_SET_LOCALIZATION_AFTER_MAP = False
# 重定位方法
LOCALIZATION_METHOD = "auto"
# 等待定位成功的轮询超时（秒）
LOCALIZATION_CHECK_TIMEOUT = 30
# 失败后重试间隔（秒）
LOCALIZATION_RETRY_INTERVAL = 10
# 最大重试次数（-1=无限）
LOCALIZATION_MAX_RETRIES = 5


def get_navigation_map_runtime_config() -> dict:
    """
    获取导航地图自动下发的最终运行配置（外部配置 > 默认值）。
    
    返回:
        {
            "auto_set_on_connect": bool,
            "wait_idle_timeout":   int,
            "service_timeout":     int,
            "skip_if_same":        bool,
        }
    """
    defaults = {
        "auto_set_on_connect": AUTO_SET_NAVIGATION_MAP_ON_CONNECT,
        "wait_idle_timeout":   NAVIGATION_MAP_SET_WAIT_IDLE_TIMEOUT,
        "service_timeout":     NAVIGATION_MAP_SET_SERVICE_TIMEOUT,
        "skip_if_same":        NAVIGATION_MAP_SKIP_IF_SAME,
    }
    try:
        try:
            from infrastructure.config_loader import get_navigation_map_config
        except ImportError:
            from config_loader import get_navigation_map_config
        external = get_navigation_map_config() or {}
    except Exception:
        external = {}
    
    # 外部配置覆盖默认值（键存在才覆盖）
    for k in defaults:
        if k in external and external[k] is not None:
            defaults[k] = external[k]
    return defaults

def get_navigation_localization_runtime_config() -> dict:
    """
    获取导航定位（reloc）的最终运行配置（外部配置 > 默认值）。

    返回:
        {
            "auto_set_after_map": bool,
            "method":    str,
            "map_path":  str,   # 留空则使用当前导航地图名
            "x_pos": float, "y_pos": float, "z_pos": float,
            "x_ori": float, "y_ori": float, "z_ori": float, "w_ori": float,
            "check_timeout":  int,
            "retry_interval": int,
            "max_retries":    int,
        }
    """
    defaults = {
        "auto_set_after_map": AUTO_SET_LOCALIZATION_AFTER_MAP,
        "method":             LOCALIZATION_METHOD,
        "map_path":           "",
        "x_pos": 0.0, "y_pos": 0.0, "z_pos": 0.0,
        "x_ori": 0.0, "y_ori": 0.0, "z_ori": 0.0, "w_ori": 0.0,
        "check_timeout":      LOCALIZATION_CHECK_TIMEOUT,
        "retry_interval":     LOCALIZATION_RETRY_INTERVAL,
        "max_retries":        LOCALIZATION_MAX_RETRIES,
    }
    try:
        try:
            from infrastructure.config_loader import get_navigation_localization_config
        except ImportError:
            from config_loader import get_navigation_localization_config
        external = get_navigation_localization_config() or {}
    except Exception:
        external = {}
    for k in defaults:
        if k in external and external[k] is not None:
            defaults[k] = external[k]
    return defaults


# 自动充电功能开关
ENABLE_AUTO_CHARGING = True
# 启动时等待电量信息功能开关
# 开启后，系统启动时会等待获取到所有机器人的电量信息后才允许执行任务
REQUIRE_BATTERY_INFO_ON_STARTUP = True
# 启动时等待电量信息的超时时间（秒），0表示无限等待
BATTERY_INFO_WAIT_TIMEOUT = 60
# 电池电量检测间隔（秒）
BATTERY_CHECK_INTERVAL = 600  # 10分钟
# 低电量阈值（低于此值触发充电）
BATTERY_LOW_THRESHOLD = 0.30  # 30%
# 电池状态 topic
BATTERY_TOPIC = "/zj_humanoid/robot/battery_info"
# 充电桩导航点位
CHARGING_STATION_POSE = "charge"
# 充电完成阈值（电量大于此值时返回home）
CHARGING_DONE_THRESHOLD = 0.99  # 99%
# 可接收任务阈值（充电时电量大于此值可接收新任务）
CHARGING_ACCEPT_TASK_THRESHOLD = 0.50  # 50%
# 充电完成后返回的home点位（按机器人类型分配）
CHARGING_DONE_HOME_POSES = {
    "robot_a": "home_transfer",  # Robot A -> 转运任务home点位
    "robot_b": "home_split",     # Robot B -> 分液任务home点位
}
CHARGING_DONE_HOME_POSE_DEFAULT = "home"  # 默认home点位（未配置的机器人使用）

# 充电状态验证配置
CHARGING_STATUS_CHECK_DELAY = 60  # 导航到充电桩后等待检查充电状态的延迟时间（秒）
CHARGING_STATUS_MAX_RETRIES = 3   # 充电状态验证失败后最大重试导航次数
# 充电状态值
CHARGING_STATUS_NOT_CHARGING = 0  # 未充电
CHARGING_STATUS_CHARGING = 1      # 充电中
CHARGING_STATUS_COMPLETE = 2      # 充电完成（当前不处理）

# 后部暂存区默认配置
# 槽位格式: 0 表示空, {"bottle_id": "xxx", "bottle_state": "..."} 表示有瓶子
# 每个机器人的储位配置可能不同

# 机器人A的储位配置
ROBOT_A_BACK_TEMP_STORAGE = {
    "glass_bottle_1000": [0, 0, 0, 0],
    "glass_bottle_500": [0, 0],
    "glass_bottle_250": [0, 0, 0, 0]
}

# 机器人B的储位配置
ROBOT_B_BACK_TEMP_STORAGE = {
    "glass_bottle_1000": [0],
    "glass_bottle_500": [0],
    "glass_bottle_250": [0]
}

# 机器人C的储位配置（示例）
ROBOT_C_BACK_TEMP_STORAGE = {
    "glass_bottle_1000": [0, 0, 0, 0],
    "glass_bottle_500": [0, 0, 0, 0, 0, 0],
    "glass_bottle_250": [0, 0, 0, 0]
}

# 机器人储位配置映射
ROBOT_STORAGE_CONFIGS = {
    RobotType.ROBOT_A: ROBOT_A_BACK_TEMP_STORAGE,
    RobotType.ROBOT_B: ROBOT_B_BACK_TEMP_STORAGE,
    RobotType.ROBOT_C: ROBOT_C_BACK_TEMP_STORAGE,
    "robot_a": ROBOT_A_BACK_TEMP_STORAGE,
    "robot_b": ROBOT_B_BACK_TEMP_STORAGE,
    "robot_c": ROBOT_C_BACK_TEMP_STORAGE,
}

# 默认储位配置（兼容旧代码）
DEFAULT_BACK_TEMP_STORAGE = ROBOT_A_BACK_TEMP_STORAGE

def get_robot_storage_config(robot_id: str) -> dict:
    """
    获取指定机器人的储位配置
    
    参数:
        robot_id: 机器人ID (如 "robot_a", "robot_b")
    
    返回:
        储位配置字典
    """
    import copy
    config = ROBOT_STORAGE_CONFIGS.get(robot_id, DEFAULT_BACK_TEMP_STORAGE)
    return copy.deepcopy(config)

# 瓶子状态
class BottleState:
    NOT_SPLIT = "未分液"
    SPLIT_DONE = "已分液"

# 暂存区区域名称常量
class StationArea:
    WAITING_SPLIT_AREA = "waiting_split/zj_humanoid/navigation/odom_info_area"           # 分液台待分液区
    SPLIT_DONE_250ML_AREA = "split_done_250ml_area"     # 250ml分液完成暂存区
    SPLIT_DONE_500ML_AREA = "split_done_500ml_area"     # 500ml分液完成暂存区
    EMPTY_BOTTLE_AREA = "empty_bottle_area"             # 空瓶区

# 存储状态文件路径
STORAGE_STATE_FILE = "storage_state.json"

# ============================================================
# HTTP API 错误码定义
# ============================================================
# 错误码格式说明：
# - 0: 成功
# - 1xxx: 请求错误 (Request Errors)
# - 2xxx: 机器人相关错误 (Robot Errors)
# - 3xxx: 任务相关错误 (Task Errors)
# - 4xxx: 系统错误 (System Errors)
# - 5xxx: 资源错误 (Resource Errors)

class ErrorCode:
    """HTTP API 错误码"""
    
    # 成功
    SUCCESS = 0
    
    # 1xxx: 请求错误
    INVALID_JSON = 1001              # JSON解析错误
    MISSING_PARAMS = 1002            # 缺少必要参数
    INVALID_PARAMS = 1003            # 参数格式错误
    UNKNOWN_CMD_TYPE = 1004          # 未知命令类型
    
    # 2xxx: 机器人相关错误
    ROBOT_NOT_FOUND = 2001           # 机器人不存在
    ROBOT_NOT_CONNECTED = 2002       # 机器人未连接
    ROBOT_BUSY = 2003                # 机器人正忙
    ROBOT_ACTION_DISABLED = 2004     # 机器人动作接口未启用
    ROBOT_ACTION_FAILED = 2005       # 机器人动作执行失败
    ROBOT_SERVICE_ERROR = 2006       # 机器人服务调用失败
    ROBOT_BATTERY_INFO_PENDING = 2007  # 电量信息未获取
    ROBOT_LOW_BATTERY = 2008         # 机器人低电量
    ROBOT_CHARGING_REJECT_TASK = 2009  # 机器人充电中，电量不足以接收任务
    ROBOT_CHARGING_FAILED = 2010     # 机器人充电失败（充电桩或导航问题）
    
    # 3xxx: 任务相关错误
    TASK_NOT_FOUND = 3001            # 任务不存在
    TASK_ID_MISMATCH = 3002          # 任务ID不匹配
    TASK_QUEUE_DISABLED = 3003       # 任务队列未启用
    TASK_TIMEOUT = 3004              # 任务超时
    TASK_CANCELLED = 3005            # 任务已取消
    TASK_FAILED = 3006               # 任务执行失败
    TASK_NOT_STARTED = 3007          # 流程尚未启动（需先发送 PROCESS_BEGINS）
    
    # 4xxx: 系统错误
    HANDLER_NOT_INIT = 4001          # 命令处理器未初始化
    SYSTEM_RESET_FAILED = 4002       # 系统重置失败
    CMD_EXECUTION_ERROR = 4003       # 命令执行异常
    INTERNAL_ERROR = 4004            # 内部错误
    SYSTEM_SLEEPING = 4005           # 系统休眠中，需要先发送 START_WORKING
    SYSTEM_ACTIVATING = 4006         # 系统激活中（连接机器人/下发地图），请稍后再试
    
    # 5xxx: 资源错误
    RESOURCE_INSUFFICIENT = 5001     # 资源不足（如瓶子不足）
    STORAGE_FULL = 5002              # 存储区已满
    RESOURCE_NOT_FOUND = 5003        # 资源未找到


# 错误码描述映射
ERROR_MESSAGES = {
    ErrorCode.SUCCESS: "成功",
    
    # 请求错误
    ErrorCode.INVALID_JSON: "JSON格式错误",
    ErrorCode.MISSING_PARAMS: "缺少必要参数",
    ErrorCode.INVALID_PARAMS: "参数格式错误",
    ErrorCode.UNKNOWN_CMD_TYPE: "未知命令类型",
    
    # 机器人错误
    ErrorCode.ROBOT_NOT_FOUND: "机器人不存在",
    ErrorCode.ROBOT_NOT_CONNECTED: "机器人未连接",
    ErrorCode.ROBOT_BUSY: "机器人正忙",
    ErrorCode.ROBOT_ACTION_DISABLED: "机器人动作接口未启用",
    ErrorCode.ROBOT_ACTION_FAILED: "机器人动作执行失败",
    ErrorCode.ROBOT_SERVICE_ERROR: "机器人服务调用失败",
    ErrorCode.ROBOT_BATTERY_INFO_PENDING: "机器人电量信息未获取，请等待",
    ErrorCode.ROBOT_LOW_BATTERY: "机器人电量低，正在充电",
    ErrorCode.ROBOT_CHARGING_REJECT_TASK: "机器人充电中，电量不足以接收新任务",
    ErrorCode.ROBOT_CHARGING_FAILED: "机器人充电失败",
    
    # 任务错误
    ErrorCode.TASK_NOT_FOUND: "任务不存在",
    ErrorCode.TASK_NOT_STARTED: "流程尚未启动，请先发送 PROCESS_BEGINS",
    ErrorCode.TASK_ID_MISMATCH: "任务ID不匹配",
    ErrorCode.TASK_QUEUE_DISABLED: "任务队列未启用",
    ErrorCode.TASK_TIMEOUT: "任务超时",
    ErrorCode.TASK_CANCELLED: "任务已取消",
    ErrorCode.TASK_FAILED: "任务执行失败",
    
    # 系统错误
    ErrorCode.HANDLER_NOT_INIT: "命令处理器未初始化",
    ErrorCode.SYSTEM_RESET_FAILED: "系统重置失败",
    ErrorCode.CMD_EXECUTION_ERROR: "命令执行异常",
    ErrorCode.INTERNAL_ERROR: "内部错误",
    ErrorCode.SYSTEM_SLEEPING: "系统休眠中，需要先发送 START_WORKING",
    ErrorCode.SYSTEM_ACTIVATING: "系统激活中，请稍后再试",
    
    # 资源错误
    ErrorCode.RESOURCE_INSUFFICIENT: "资源不足",
    ErrorCode.STORAGE_FULL: "存储区已满",
    ErrorCode.RESOURCE_NOT_FOUND: "资源未找到",
}

# 业务错误码到HTTP状态码的映射
# HTTP状态码说明：
# - 200 OK: 请求成功
# - 400 Bad Request: 请求参数错误
# - 404 Not Found: 资源未找到
# - 409 Conflict: 资源冲突（如机器人正忙）
# - 422 Unprocessable Entity: 请求格式正确但无法处理（如资源不足）
# - 500 Internal Server Error: 服务器内部错误
# - 503 Service Unavailable: 服务不可用
ERROR_CODE_TO_HTTP_STATUS = {
    ErrorCode.SUCCESS: 200,
    
    # 1xxx 请求错误 -> 400 Bad Request
    ErrorCode.INVALID_JSON: 400,
    ErrorCode.MISSING_PARAMS: 400,
    ErrorCode.INVALID_PARAMS: 400,
    ErrorCode.UNKNOWN_CMD_TYPE: 400,
    
    # 2xxx 机器人错误
    ErrorCode.ROBOT_NOT_FOUND: 404,           # 404 Not Found
    ErrorCode.ROBOT_NOT_CONNECTED: 503,       # 503 Service Unavailable
    ErrorCode.ROBOT_BUSY: 409,                # 409 Conflict
    ErrorCode.ROBOT_ACTION_DISABLED: 503,     # 503 Service Unavailable
    ErrorCode.ROBOT_ACTION_FAILED: 500,       # 500 Internal Server Error
    ErrorCode.ROBOT_SERVICE_ERROR: 502,       # 502 Bad Gateway
    ErrorCode.ROBOT_BATTERY_INFO_PENDING: 503,  # 503 Service Unavailable
    ErrorCode.ROBOT_LOW_BATTERY: 503,         # 503 Service Unavailable
    ErrorCode.ROBOT_CHARGING_REJECT_TASK: 503,  # 503 Service Unavailable
    ErrorCode.ROBOT_CHARGING_FAILED: 500,     # 500 Internal Server Error
    
    # 3xxx 任务错误
    ErrorCode.TASK_NOT_FOUND: 404,            # 404 Not Found
    ErrorCode.TASK_ID_MISMATCH: 404,          # 404 Not Found
    ErrorCode.TASK_QUEUE_DISABLED: 503,       # 503 Service Unavailable
    ErrorCode.TASK_TIMEOUT: 408,              # 408 Request Timeout
    ErrorCode.TASK_CANCELLED: 410,            # 410 Gone
    ErrorCode.TASK_FAILED: 500,               # 500 Internal Server Error
    ErrorCode.TASK_NOT_STARTED: 409,          # 409 Conflict（流程未激活）

    # 4xxx 系统错误 -> 500 Internal Server Error
    ErrorCode.HANDLER_NOT_INIT: 503,          # 503 Service Unavailable
    ErrorCode.SYSTEM_RESET_FAILED: 500,
    ErrorCode.CMD_EXECUTION_ERROR: 500,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SYSTEM_SLEEPING: 409,
    ErrorCode.SYSTEM_ACTIVATING: 503,
    
    # 5xxx 资源错误
    ErrorCode.RESOURCE_INSUFFICIENT: 422,     # 422 Unprocessable Entity
    ErrorCode.STORAGE_FULL: 422,              # 422 Unprocessable Entity
    ErrorCode.RESOURCE_NOT_FOUND: 404,        # 404 Not Found
}


def get_http_status(error_code: int) -> int:
    """
    根据业务错误码获取对应的HTTP状态码
    
    参数:
        error_code: 业务错误码
    
    返回:
        HTTP状态码
    """
    return ERROR_CODE_TO_HTTP_STATUS.get(error_code, 500)


def make_error_response(error_code: int, message: str = None, cmd_id: str = None, **extra_data) -> dict:
    """
    生成标准错误响应
    
    参数:
        error_code: 错误码
        message: 错误消息（可选，不提供则使用默认消息）
        cmd_id: 命令ID（可选）
        **extra_data: 额外数据字段
    
    返回:
        标准格式的响应字典（包含 _http_status 字段用于HTTP服务器设置状态码）
    """
    response = {
        "success": error_code == ErrorCode.SUCCESS,
        "code": error_code,
        "message": message or ERROR_MESSAGES.get(error_code, "未知错误"),
        "_http_status": get_http_status(error_code)  # 内部字段，用于HTTP服务器
    }
    if cmd_id:
        response["cmd_id"] = cmd_id
    response.update(extra_data)
    return response


def make_success_response(message: str = "操作成功", cmd_id: str = None, **extra_data) -> dict:
    """
    生成标准成功响应
    
    参数:
        message: 成功消息
        cmd_id: 命令ID（可选）
        **extra_data: 额外数据字段
    
    返回:
        标准格式的响应字典
    """
    response = {
        "success": True,
        "code": ErrorCode.SUCCESS,
        "message": message,
        "_http_status": 200
    }
    if cmd_id:
        response["cmd_id"] = cmd_id
    response.update(extra_data)
    return response
