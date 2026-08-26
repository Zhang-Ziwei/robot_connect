"""
存储管理器模块
负责后部暂存区状态的持久化管理
支持多机器人各自独立的暂存区
"""

import json
import os
from typing import Dict, List, Optional, Any, Union
from infrastructure.constants import DEFAULT_BACK_TEMP_STORAGE, STORAGE_STATE_FILE, BottleState, ROBOT_CONFIGS, get_robot_storage_config
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()

# 默认机器人ID
DEFAULT_ROBOT_ID = "robot_a"

class StorageManager:
    """
    存储状态管理器
    支持多机器人各自独立的暂存区管理
    
    存储结构:
    {
        "robot_a": {
            "glass_bottle_500": [0, {...}, 0, ...],
            "glass_bottle_250": [0, 0, ...],
            ...
        },
        "robot_b": {
            ...
        }
    }
    """
    
    def __init__(self, state_file: str = STORAGE_STATE_FILE):
        self.state_file = state_file
        self._storage = None  # 多机器人存储: {robot_id: {bottle_type: [slots]}}
    
    def load_storage(self) -> Dict[str, Dict[str, List]]:
        """从文件加载存储状态"""
        if not os.path.exists(self.state_file):
            logger.info("存储管理器", f"状态文件不存在，使用默认配置")
            self._storage = self._get_default_storage()
            return self._storage
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
            
            # 检测是否是旧格式（单机器人）
            if self._is_old_format(loaded_data):
                logger.info("存储管理器", "检测到旧格式数据，自动迁移到多机器人格式")
                self._storage = self._migrate_old_format(loaded_data)
                self.save_storage()  # 保存迁移后的数据
            else:
                self._storage = loaded_data
            
            logger.info("存储管理器", f"成功加载存储状态: {self.state_file}")
            return self._storage
        except Exception as e:
            logger.error("存储管理器", f"加载存储状态失败: {e}, 使用默认配置")
            self._storage = self._get_default_storage()
            return self._storage
    
    def _is_old_format(self, data: Dict) -> bool:
        """检测是否是旧格式（单机器人）"""
        # 旧格式的key是瓶子类型，新格式的key是robot_id
        if not data:
            return False
        first_key = list(data.keys())[0]
        # 如果第一个key是瓶子类型（包含bottle），则是旧格式
        return "bottle" in first_key.lower() or "glass" in first_key.lower()
    
    def _migrate_old_format(self, old_data: Dict) -> Dict[str, Dict[str, List]]:
        """将旧格式数据迁移到新的多机器人格式"""
        return {DEFAULT_ROBOT_ID: old_data}
    
    def save_storage(self, storage: Dict[str, Dict[str, List]] = None) -> bool:
        """保存存储状态到文件"""
        if storage is not None:
            self._storage = storage
        
        if self._storage is None:
            logger.error("存储管理器", "没有存储状态可保存")
            return False
        
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self._storage, f, indent=4, ensure_ascii=False)
            logger.info("存储管理器", f"存储状态已保存到: {self.state_file}")
            return True
        except Exception as e:
            logger.error("存储管理器", f"保存存储状态失败: {e}")
            return False
    
    def reset_storage(self, robot_id: str = None) -> Dict[str, Dict[str, List]]:
        """
        重置存储状态为默认值
        
        Args:
            robot_id: 指定机器人ID，None表示重置所有机器人
        """
        if robot_id:
            if self._storage is None:
                self._storage = self._get_default_storage()
            self._storage[robot_id] = self._get_default_robot_storage(robot_id)
            logger.info("存储管理器", f"机器人 {robot_id} 存储状态已重置")
        else:
            self._storage = self._get_default_storage()
            logger.info("存储管理器", "所有存储状态已重置为默认值")
        return self._storage
    
    def get_storage(self, robot_id: str = None) -> Union[Dict[str, Dict[str, List]], Dict[str, List]]:
        """
        获取存储状态
        
        Args:
            robot_id: 指定机器人ID，None表示获取所有机器人的存储
        
        Returns:
            如果指定robot_id，返回该机器人的存储 {bottle_type: [slots]}
            否则返回所有存储 {robot_id: {bottle_type: [slots]}}
        """
        if self._storage is None:
            self.load_storage()
        
        if robot_id:
            return self._get_robot_storage(robot_id)
        return self._storage
    
    def _get_robot_storage(self, robot_id: str) -> Dict[str, List]:
        """获取指定机器人的存储，如果不存在则创建"""
        if self._storage is None:
            self.load_storage()
        
        if robot_id not in self._storage:
            self._storage[robot_id] = self._get_default_robot_storage(robot_id)
            logger.info("存储管理器", f"为机器人 {robot_id} 创建新的存储空间")
        
        return self._storage[robot_id]
    
    def _get_default_storage(self) -> Dict[str, Dict[str, List]]:
        """获取默认存储配置（包含所有已配置的机器人）"""
        default_storage = {}
        
        # 为每个配置的机器人创建默认存储（使用各自的储位配置）
        for robot_id in ROBOT_CONFIGS.keys():
            default_storage[robot_id] = get_robot_storage_config(robot_id)
        
        # 确保至少有默认机器人
        if DEFAULT_ROBOT_ID not in default_storage:
            default_storage[DEFAULT_ROBOT_ID] = get_robot_storage_config(DEFAULT_ROBOT_ID)
        
        return default_storage
    
    def _get_default_robot_storage(self, robot_id: str = None) -> Dict[str, List]:
        """获取单个机器人的默认存储配置"""
        return get_robot_storage_config(robot_id or DEFAULT_ROBOT_ID)
    
    def display_storage_status(self, robot_id: str = None) -> str:
        """
        显示存储状态的格式化字符串
        
        Args:
            robot_id: 指定机器人ID，None表示显示所有机器人
        """
        if self._storage is None:
            self.load_storage()
        
        status_lines = []
        
        if robot_id:
            robots_to_display = {robot_id: self._get_robot_storage(robot_id)}
        else:
            robots_to_display = self._storage
        
        for rid, robot_storage in robots_to_display.items():
            status_lines.append(f"\n{'='*60}")
            status_lines.append(f"机器人: {rid}")
            status_lines.append("="*60)
        
            for bottle_type, slots in robot_storage.items():
                occupied = sum(1 for slot in slots if not self.is_slot_empty(slot))
                split_count = 0
                for i, slot in enumerate(slots):
                    if not self.is_slot_empty(slot):
                        info = self.get_bottle_info(rid, bottle_type, i)
                        if info and info.get("bottle_state") == BottleState.SPLIT_DONE:
                            split_count += 1
                total = len(slots)
                status_lines.append(f"{bottle_type}:")
                status_lines.append(f"  占用: {occupied}/{total}, 已分液: {split_count}")
                # 简化显示
                slot_display = []
                for i, slot in enumerate(slots):
                    if self.is_slot_empty(slot):
                        slot_display.append("空")
                    else:
                        bid = self.get_bottle_id(slot)
                        info = self.get_bottle_info(rid, bottle_type, i)
                        state = info.get("bottle_state", BottleState.NOT_SPLIT) if info else BottleState.NOT_SPLIT
                        state_mark = "✓" if state == BottleState.SPLIT_DONE else "○"
                        slot_display.append(f"{bid}({state_mark})")
                status_lines.append(f"  槽位: {slot_display}")
        
        status_lines.append("="*60)
        return "\n".join(status_lines)
    
    def update_slot(self, robot_id: str, bottle_type: str, slot_index: int, 
                    bottle_id: Union[str, int]) -> bool:
        """
        更新指定槽位（仅管理槽位占用，不处理瓶子状态）
        
        注意：放置瓶子后，需要调用 set_bottle_state() 来设置瓶子状态
        
        Args:
            robot_id: 机器人ID
            bottle_type: 瓶子类型
            slot_index: 槽位索引
            bottle_id: 瓶子ID，0表示清空槽位
        
        Returns:
            bool: 操作是否成功
        """
        robot_storage = self._get_robot_storage(robot_id)
        
        if bottle_type not in robot_storage:
            logger.error("存储管理器", f"无效的瓶子类型: {bottle_type}")
            return False
        
        if slot_index < 0 or slot_index >= len(robot_storage[bottle_type]):
            logger.error("存储管理器", f"无效的槽位索引: {slot_index}")
            return False
        
        # 如果是清空槽位
        if bottle_id == 0:
            robot_storage[bottle_type][slot_index] = 0
            logger.info("存储管理器", f"清空槽位: {robot_id}/{bottle_type}[{slot_index}]")
        else:
            # 仅存储瓶子ID，状态由 set_bottle_state 单独设置
            robot_storage[bottle_type][slot_index] = {
                "bottle_id": bottle_id
            }
            logger.info("存储管理器", f"放置瓶子: {robot_id}/{bottle_type}[{slot_index}] = {bottle_id}")
        
        self.save_storage()
        return True
    
    def clear_slot(self, robot_id: str, bottle_type: str, slot_index: int) -> bool:
        """清空指定槽位"""
        return self.update_slot(robot_id, bottle_type, slot_index, 0)
    
    def set_bottle_state(self, robot_id: str, bottle_type: str, slot_index: int, 
                         bottle_state: str = None, **extra_params) -> bool:
        """
        设置瓶子状态和其他参数
        
        该方法用于在 update_slot() 放置瓶子后，设置瓶子的状态和其他属性
        
        Args:
            robot_id: 机器人ID
            bottle_type: 瓶子类型
            slot_index: 槽位索引
            bottle_state: 瓶子状态（如 BottleState.NOT_SPLIT, BottleState.SPLIT_DONE）
            **extra_params: 其他瓶子参数，如 scan_time, qr_code 等
        
        Returns:
            bool: 操作是否成功
            
        示例:
            # 设置状态
            set_bottle_state(robot_id, "glass_bottle_500", 0, BottleState.NOT_SPLIT)
            
            # 设置状态和额外参数
            set_bottle_state(robot_id, "glass_bottle_500", 0, 
                           bottle_state=BottleState.SPLIT_DONE,
                           scan_time="2026-01-21 10:00:00",
                           qr_code="ABC123")
        """
        robot_storage = self._get_robot_storage(robot_id)
        
        if bottle_type not in robot_storage:
            logger.error("存储管理器", f"无效的瓶子类型: {bottle_type}")
            return False
        
        if slot_index < 0 or slot_index >= len(robot_storage[bottle_type]):
            logger.error("存储管理器", f"无效的槽位索引: {slot_index}")
            return False
        
        slot = robot_storage[bottle_type][slot_index]
        if slot == 0:
            logger.error("存储管理器", f"槽位为空，无法设置状态")
            return False
        
        # 兼容旧格式（纯字符串）
        if isinstance(slot, str):
            robot_storage[bottle_type][slot_index] = {
                "bottle_id": slot
            }
            slot = robot_storage[bottle_type][slot_index]
        
        # 设置瓶子状态
        if bottle_state is not None:
            slot["bottle_state"] = bottle_state
        
        # 设置额外参数
        for key, value in extra_params.items():
            slot[key] = value
        
        self.save_storage()
        
        # 构建日志信息
        params_str = f"bottle_state={bottle_state}" if bottle_state else ""
        if extra_params:
            extra_str = ", ".join(f"{k}={v}" for k, v in extra_params.items())
            params_str = f"{params_str}, {extra_str}" if params_str else extra_str
        logger.info("存储管理器", f"设置 {robot_id}/{bottle_type}[{slot_index}]: {params_str}")
        return True
    
    def get_bottle_info(self, robot_id: str, bottle_type: str, slot_index: int) -> Optional[Dict]:
        """
        获取指定槽位的瓶子信息
        
        Args:
            robot_id: 机器人ID
            bottle_type: 瓶子类型
            slot_index: 槽位索引
        
        Returns:
            瓶子信息字典，包含 bottle_id, bottle_state, bottle_type, slot_index 和其他参数
            如果槽位为空，返回 None
            
        示例返回:
            {
                "bottle_id": "ABC123",
                "bottle_state": "NOT_SPLIT",
                "bottle_type": "glass_bottle_500",
                "slot_index": 0,
                "scan_time": "2026-01-21 10:00:00",
                "qr_code": "QR123"
            }
        """
        robot_storage = self._get_robot_storage(robot_id)
        
        if bottle_type not in robot_storage:
            return None
        
        if slot_index < 0 or slot_index >= len(robot_storage[bottle_type]):
            return None
        
        slot = robot_storage[bottle_type][slot_index]
        if slot == 0:
            return None
        
        # 兼容旧格式（纯字符串）
        if isinstance(slot, str):
            return {
                "bottle_id": slot,
                "bottle_state": BottleState.NOT_SPLIT,
                "bottle_type": bottle_type,
                "slot_index": slot_index
            }
        
        # 兼容旧的 split_done 格式
        if isinstance(slot, dict) and "split_done" in slot and "bottle_state" not in slot:
            state = BottleState.SPLIT_DONE if slot.get("split_done") else BottleState.NOT_SPLIT
            result = slot.copy()
            result["bottle_state"] = state
            result["bottle_type"] = bottle_type
            result["slot_index"] = slot_index
            del result["split_done"]
            return result
        
        # 新格式：返回完整信息，如果没有 bottle_state 则设置默认值
        if isinstance(slot, dict):
            result = slot.copy()
            if "bottle_state" not in result:
                result["bottle_state"] = BottleState.NOT_SPLIT
            # 始终添加 bottle_type 和 slot_index
            result["bottle_type"] = bottle_type
            result["slot_index"] = slot_index
            return result
        
        return None
    
    def is_slot_empty(self, slot: Any) -> bool:
        """
        检查槽位是否为空
        
        Args:
            slot: 槽位数据
        
        Returns:
            True: 槽位为空 (slot 为 0, None, 空字符串, 或空字典)
            False: 槽位有瓶子
        """
        if slot is None:
            return True
        if slot == 0:
            return True
        if isinstance(slot, str) and slot == "":
            return True
        if isinstance(slot, dict) and not slot:
            return True
        return False
    
    def get_bottle_id(self, slot: Any) -> Optional[str]:
        """从槽位数据中获取bottle_id"""
        if slot == 0:
            return None
        if isinstance(slot, str):
            return slot
        if isinstance(slot, dict):
            return slot.get("bottle_id")
        return None
    
    def get_empty_slot_index(self, robot_id: str, bottle_type: str) -> Optional[int]:
        """
        获取指定类型的第一个空槽位索引
        
        Args:
            robot_id: 机器人ID
            bottle_type: 瓶子类型
        """
        robot_storage = self._get_robot_storage(robot_id)
        
        if bottle_type not in robot_storage:
            return None
        
        for i, slot in enumerate(robot_storage[bottle_type]):
            if self.is_slot_empty(slot):
                return i
        return None
    
    def get_unsplit_slot_index(self, robot_id: str, bottle_type: str) -> Optional[int]:
        """
        获取指定类型的第一个未分液瓶子的槽位索引
        
        Args:
            robot_id: 机器人ID
            bottle_type: 瓶子类型
        """
        robot_storage = self._get_robot_storage(robot_id)
        
        if bottle_type not in robot_storage:
            print("bottle_type not in robot_storage:")
            return None
        
        for i, slot in enumerate(robot_storage[bottle_type]):
            if not self.is_slot_empty(slot):
                # 兼容旧格式（纯字符串）
                if isinstance(slot, str):
                    return i
                if isinstance(slot, dict):
                    # 兼容旧的 split_done 格式
                    if "split_done" in slot and "bottle_state" not in slot:
                        if not slot.get("split_done", False):
                            return i
                    # 新的 bottle_state 格式
                    elif slot.get("bottle_state", BottleState.NOT_SPLIT) == BottleState.NOT_SPLIT:
                        return i
        return None
    
    def is_full(self, robot_id: str = None) -> bool:
        """
        检查暂存区是否已满
        
        Args:
            robot_id: 指定机器人ID，None表示检查所有机器人
        """
        if self._storage is None:
            self.load_storage()
        
        if robot_id:
            robot_storage = self._get_robot_storage(robot_id)
            for bottle_type, slots in robot_storage.items():
                for slot in slots:
                    if self.is_slot_empty(slot):
                        return False
            return True
        else:
            # 检查所有机器人
            for rid, robot_storage in self._storage.items():
                for bottle_type, slots in robot_storage.items():
                    for slot in slots:
                        if self.is_slot_empty(slot):
                            return False
            return True
    
    def is_type_full(self, robot_id: str, bottle_type: str) -> bool:
        """
        检查指定类型的暂存区是否已满
        
        Args:
            robot_id: 机器人ID
            bottle_type: 瓶子类型
        """
        robot_storage = self._get_robot_storage(robot_id)
        
        if bottle_type not in robot_storage:
            return True
        
        for slot in robot_storage[bottle_type]:
            if self.is_slot_empty(slot):
                return False
        return True
    
    def get_all_robot_ids(self) -> List[str]:
        """获取所有机器人ID列表"""
        if self._storage is None:
            self.load_storage()
        return list(self._storage.keys())


# 全局存储管理器实例
_storage_manager = None

def init_storage_manager(reset: bool = False) -> StorageManager:
    """
    初始化存储管理器
    
    Args:
        reset: 是否重置为默认值
    
    Returns:
        StorageManager实例
    """
    global _storage_manager
    _storage_manager = StorageManager()
    
    if reset:
        _storage_manager.reset_storage()
        _storage_manager.save_storage()
        logger.info("存储管理器", "存储状态已重置并保存")
    else:
        _storage_manager.load_storage()
        logger.info("存储管理器", "存储状态已加载")
    
    return _storage_manager

def get_storage_manager() -> StorageManager:
    """获取存储管理器实例"""
    global _storage_manager
    if _storage_manager is None:
        raise RuntimeError("存储管理器未初始化，请先调用init_storage_manager")
    return _storage_manager

def get_back_temp_storage(robot_id: str = DEFAULT_ROBOT_ID) -> Dict[str, List]:
    """获取后部暂存区状态（便捷函数）"""
    return get_storage_manager().get_storage(robot_id)

def save_back_temp_storage(storage: Dict[str, Dict[str, List]] = None):
    """保存后部暂存区状态（便捷函数）"""
    get_storage_manager().save_storage(storage)

def check_storage_is_full(back_temp_storage: Dict[str, List]) -> bool:
    """
    检查暂存区是否已满（兼容函数）
    
    Args:
        back_temp_storage: 单个机器人的暂存区数据
    """
    for bottle_type, slots in back_temp_storage.items():
        for slot in slots:
            if slot == 0:
                return False
    return True
