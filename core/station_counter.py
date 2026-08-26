"""
暂存区瓶子计数器模块
用于追踪以下三个区域的瓶子数量：
1. 分液台待分液区 (waiting_split_area)
2. 250ml分液完成暂存区 (split_done_250ml_area)
3. 500ml分液完成暂存区 (split_done_500ml_area)

注意：暂存区与机器人无关，是独立的物理位置
"""

import threading
import json
import os
from typing import Dict, Any, Optional
from infrastructure.error_logger import get_error_logger
from infrastructure.constants import StationArea

logger = get_error_logger()

# 状态文件路径
STATION_STATE_FILE = "station_state.json"


class StationCounter:
    """暂存区瓶子计数器"""
    
    # 区域名称常量（从 constants.py 引用）
    WAITING_SPLIT_AREA = StationArea.WAITING_SPLIT_AREA
    SPLIT_DONE_250ML_AREA = StationArea.SPLIT_DONE_250ML_AREA
    SPLIT_DONE_500ML_AREA = StationArea.SPLIT_DONE_500ML_AREA
    
    def __init__(self):
        """初始化计数器"""
        self.lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._load_state()
    
    def _get_default_counter(self) -> Dict[str, int]:
        """获取默认计数器结构"""
        return {
            self.WAITING_SPLIT_AREA: 0,
            self.SPLIT_DONE_250ML_AREA: 0,
            self.SPLIT_DONE_500ML_AREA: 0
        }
    
    def _ensure_initialized(self):
        """确保计数器已初始化"""
        if not self._counters:
            self._counters = self._get_default_counter()
    
    def _load_state(self):
        """从文件加载状态"""
        try:
            if os.path.exists(STATION_STATE_FILE):
                with open(STATION_STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 兼容旧格式（按机器人分组）和新格式（直接的计数器）
                    if isinstance(data, dict):
                        # 检查是否是旧格式（包含 robot_id 作为键）
                        first_value = next(iter(data.values()), None) if data else None
                        if isinstance(first_value, dict) and self.WAITING_SPLIT_AREA in first_value:
                            # 旧格式：合并所有机器人的数据
                            merged = self._get_default_counter()
                            for robot_data in data.values():
                                if isinstance(robot_data, dict):
                                    for area, count in robot_data.items():
                                        if area in merged:
                                            merged[area] += count
                            self._counters = merged
                            logger.info("暂存区计数器", f"已从旧格式迁移状态: {self._counters}")
                        elif self.WAITING_SPLIT_AREA in data:
                            # 新格式
                            self._counters = data
                            logger.info("暂存区计数器", f"已从文件加载状态: {self._counters}")
                        else:
                            self._counters = self._get_default_counter()
                    else:
                        self._counters = self._get_default_counter()
            else:
                self._counters = self._get_default_counter()
                logger.info("暂存区计数器", "状态文件不存在，使用默认空状态")
        except Exception as e:
            logger.error("暂存区计数器", f"加载状态文件失败: {e}")
            self._counters = self._get_default_counter()
    
    def _save_state(self):
        """保存状态到文件"""
        try:
            with open(STATION_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._counters, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("暂存区计数器", f"保存状态文件失败: {e}")
    
    def increment(self, area: str, count: int = 1) -> int:
        """
        增加指定区域的计数
        
        参数:
            area: 区域名称
            count: 增加数量（默认1）
        
        返回:
            增加后的数量
        """
        with self.lock:
            self._ensure_initialized()
            if area in self._counters:
                self._counters[area] += count
                new_value = self._counters[area]
                self._save_state()
                logger.info("暂存区计数器", f"{area} +{count} = {new_value}")
                return new_value
            elif area == None:
                return _0
            else:
                logger.warning("暂存区计数器", f"未知区域: {area}")
                return 0
    
    def decrement(self, area: str, count: int = 1) -> int:
        """
        减少指定区域的计数（不会低于0）
        
        参数:
            area: 区域名称
            count: 减少数量（默认1）
        
        返回:
            减少后的数量
        """
        with self.lock:
            self._ensure_initialized()
            if area in self._counters:
                self._counters[area] = max(0, self._counters[area] - count)
                new_value = self._counters[area]
                self._save_state()
                logger.info("暂存区计数器", f"{area} -{count} = {new_value}")
                return new_value
            elif area == None:
                return 0
            else:
                logger.warning("暂存区计数器", f"未知区域: {area}")
                return 0
    
    def set_count(self, area: str, count: int) -> int:
        """
        设置指定区域的计数
        
        参数:
            area: 区域名称
            count: 目标数量
        
        返回:
            设置后的数量
        """
        with self.lock:
            self._ensure_initialized()
            if area in self._counters:
                self._counters[area] = max(0, count)
                new_value = self._counters[area]
                self._save_state()
                logger.info("暂存区计数器", f"{area} = {new_value}")
                return new_value
            else:
                logger.warning("暂存区计数器", f"未知区域: {area}")
                return 0
    
    def get_count(self, area: str) -> int:
        """
        获取指定区域的计数
        
        参数:
            area: 区域名称
        
        返回:
            当前数量
        """
        with self.lock:
            self._ensure_initialized()
            return self._counters.get(area, 0)
    
    def get_all_counts(self) -> Dict[str, Any]:
        """
        获取所有区域的计数
        
        返回:
            计数字典
        """
        with self.lock:
            self._ensure_initialized()
            return {
                "counters": self._counters.copy(),
                "total": sum(self._counters.values())
            }
    
    def reset(self, area: str = None):
        """
        重置计数器
        
        参数:
            area: 区域名称，如果为None则重置所有区域
        """
        with self.lock:
            if area is None:
                # 重置所有
                self._counters = self._get_default_counter()
                logger.info("暂存区计数器", "已重置所有计数器")
            else:
                # 重置指定区域
                self._ensure_initialized()
                if area in self._counters:
                    self._counters[area] = 0
                    logger.info("暂存区计数器", f"已重置 {area} 计数器")
            
            self._save_state()
    
    def get_status_display(self) -> str:
        """
        获取格式化的状态显示字符串
        
        返回:
            格式化的状态字符串
        """
        with self.lock:
            self._ensure_initialized()
            
            lines = ["=" * 50]
            lines.append("暂存区瓶子数量统计")
            lines.append("=" * 50)
            
            area_names = {
                self.WAITING_SPLIT_AREA: "分液台待分液区",
                self.SPLIT_DONE_250ML_AREA: "250ml分液完成暂存区",
                self.SPLIT_DONE_500ML_AREA: "500ml分液完成暂存区"
            }
            
            for area, name in area_names.items():
                count = self._counters.get(area, 0)
                lines.append(f"  {name}: {count} 瓶")
            
            lines.append(f"  总计: {sum(self._counters.values())} 瓶")
            lines.append("=" * 50)
            return "\n".join(lines)


# 单例模式
_station_counter: Optional[StationCounter] = None


def get_station_counter() -> StationCounter:
    """获取暂存区计数器单例"""
    global _station_counter
    if _station_counter is None:
        _station_counter = StationCounter()
    return _station_counter


def reset_station_counter():
    """重置暂存区计数器单例"""
    global _station_counter
    _station_counter = None
