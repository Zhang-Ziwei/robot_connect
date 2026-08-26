"""
瓶子管理模块
管理所有样品瓶的信息、参数和状态
"""

from typing import Dict, List, Optional
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()


class BottleInfo:
    """瓶子信息类"""
    
    def __init__(self, bottle_id: str, object_type: str, hand: str, 
                 target_pose: str, navigation_pose: str, timeout: float = 10.0):
        self.bottle_id = bottle_id
        self.object_type = object_type
        self.hand = hand
        self.target_pose = target_pose
        self.navigation_pose = navigation_pose
        self.timeout = timeout
        self.scanned = False  # 是否已扫码
        self.location = None  # 当前位置
    
    def to_dict(self):
        """转换为字典"""
        return {
            "bottle_id": self.bottle_id,
            "object_type": self.object_type,
            "hand": self.hand,
            "target_pose": self.target_pose,
            "navigation_pose": self.navigation_pose,
            "timeout": self.timeout,
            "scanned": self.scanned,
            "location": self.location
        }


class TargetPose:
    """目标点位类"""
    
    def __init__(self, pose_name: str, max_num: int = 2):
        self.pose_name = pose_name
        self.max_num = max_num
        self.count = 0  # 当前瓶子数量
        self.bottles = []  # 当前点位的瓶子ID列表
    
    def is_full(self):
        """检查点位是否已满"""
        return self.count >= self.max_num
    
    def can_add(self, num: int = 1):
        """检查是否可以添加指定数量的瓶子"""
        return (self.count + num) <= self.max_num
    
    def add_bottle(self, bottle_id: str):
        """添加瓶子"""
        if self.is_full():
            return False
        self.bottles.append(bottle_id)
        self.count += 1
        return True
    
    def remove_bottle(self, bottle_id: str):
        """移除瓶子"""
        if bottle_id in self.bottles:
            self.bottles.remove(bottle_id)
            self.count -= 1
            return True
        return False
    
    def get_available_slots(self):
        """获取可用槽位数"""
        return self.max_num - self.count


class BottleManager:
    """瓶子管理器"""
    
    def __init__(self):
        self.bottles: Dict[str, BottleInfo] = {}  # bottle_id -> BottleInfo
        self.target_poses: Dict[str, TargetPose] = {}  # pose_name -> TargetPose
        self._init_default_bottles()
        self._init_default_poses()
    
    def _init_default_bottles(self):
        """初始化默认瓶子信息（示例数据）"""
        # 这里可以从配置文件加载默认瓶子信息
        default_bottles = [
            {
                "bottle_id": "glass_bottle_1000_001",
                "object_type": "glass_bottle_1000",
                "hand": "right",
                "target_pose": "shelf_temp_1000_001",
                "navigation_pose": "shelf"
            },
            {
                "bottle_id": "glass_bottle_1000_002",
                "object_type": "glass_bottle_1000",
                "hand": "right",
                "target_pose": "shelf_temp_1000_002",
                "navigation_pose": "shelf"
            },
        ]
        
        for bottle_data in default_bottles:
            bottle = BottleInfo(**bottle_data)
            self.bottles[bottle.bottle_id] = bottle
    
    def _init_default_poses(self):
        """初始化默认点位信息"""
        # 初始化各类点位
        pose_names = [
            # 货架暂存区
            "shelf_temp_1000_001", "shelf_temp_1000_002", 
            "shelf_temp_1000_003", "shelf_temp_1000_004",
            "shelf_temp_250_001", "shelf_temp_250_002",
            # 后部平台暂存区
            "back_temp_1000_001", "back_temp_1000_002",
            "back_temp_250_001", "back_temp_250_002",
            # 工作台暂存区
            "worktable_temp_001", "worktable_temp_002",
            # 扫描检测区
            "detect_temp_1000_001", "detect_temp_250_001"
        ]
        
        for pose_name in pose_names:
            self.target_poses[pose_name] = TargetPose(pose_name, max_num=2)
    
    def register_bottle(self, bottle_id: str, object_type: str, hand: str,
                       target_pose: str, navigation_pose: str, timeout: float = 10.0):
        """注册新瓶子"""
        bottle = BottleInfo(bottle_id, object_type, hand, target_pose, navigation_pose, timeout)
        self.bottles[bottle_id] = bottle
        logger.info("瓶子管理器", f"注册瓶子: {bottle_id}")
        return bottle
    
    def get_bottle(self, bottle_id: str) -> Optional[BottleInfo]:
        """获取瓶子信息"""
        return self.bottles.get(bottle_id)
    
    def get_bottles_by_pose(self, pose_name: str) -> List[BottleInfo]:
        """获取指定点位的所有瓶子"""
        return [b for b in self.bottles.values() if b.target_pose == pose_name]
    
    def get_bottles_by_navigation_pose(self, nav_pose: str) -> List[BottleInfo]:
        """获取指定导航点位的所有瓶子"""
        return [b for b in self.bottles.values() if b.navigation_pose == nav_pose]
    
    def get_all_bottles(self) -> Dict[str, BottleInfo]:
        """获取所有瓶子"""
        return self.bottles
    
    def get_target_pose(self, pose_name: str) -> Optional[TargetPose]:
        """获取目标点位"""
        if pose_name not in self.target_poses:
            # 自动创建新点位
            self.target_poses[pose_name] = TargetPose(pose_name)
        return self.target_poses.get(pose_name)
    
    def can_place_bottle(self, pose_name: str) -> bool:
        """检查点位是否可以放置瓶子"""
        pose = self.get_target_pose(pose_name)
        return pose and not pose.is_full()
    
    def place_bottle(self, bottle_id: str, pose_name: str) -> bool:
        """将瓶子放置到点位"""
        bottle = self.get_bottle(bottle_id)
        pose = self.get_target_pose(pose_name)
        
        if not bottle or not pose:
            logger.error("瓶子管理器", f"放置失败：瓶子或点位不存在 ({bottle_id}, {pose_name})")
            return False
        
        if pose.is_full():
            logger.warning("瓶子管理器", f"点位已满：{pose_name}")
            return False
        
        # 更新瓶子位置
        bottle.location = pose_name
        bottle.target_pose = pose_name
        pose.add_bottle(bottle_id)
        
        logger.info("瓶子管理器", f"瓶子 {bottle_id} 放置到 {pose_name}")
        return True
    
    def remove_bottle_from_pose(self, bottle_id: str, pose_name: str) -> bool:
        """从点位移除瓶子"""
        bottle = self.get_bottle(bottle_id)
        pose = self.get_target_pose(pose_name)
        
        if not bottle or not pose:
            return False
        
        if pose.remove_bottle(bottle_id):
            bottle.location = None
            logger.info("瓶子管理器", f"瓶子 {bottle_id} 从 {pose_name} 移除")
            return True
        
        return False
    
    def mark_scanned(self, bottle_id: str):
        """标记瓶子已扫码"""
        bottle = self.get_bottle(bottle_id)
        if bottle:
            bottle.scanned = True
            logger.info("瓶子管理器", f"瓶子 {bottle_id} 已扫码")
    
    def get_bottle_detail(self, bottle_id: str) -> Optional[Dict]:
        """获取瓶子详细信息"""
        bottle = self.get_bottle(bottle_id)
        return bottle.to_dict() if bottle else None
    
    def get_pose_info(self, pose_name: str) -> Optional[Dict]:
        """获取点位信息"""
        pose = self.get_target_pose(pose_name)
        if pose:
            return {
                "pose_name": pose.pose_name,
                "max_num": pose.max_num,
                "count": pose.count,
                "available_slots": pose.get_available_slots(),
                "bottles": pose.bottles
            }
        return None


# 全局瓶子管理器实例
_bottle_manager = None

def get_bottle_manager():
    """获取瓶子管理器实例（单例）"""
    global _bottle_manager
    if _bottle_manager is None:
        _bottle_manager = BottleManager()
    return _bottle_manager

