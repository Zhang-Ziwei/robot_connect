"""
任务优化算法模块
优化机器人导航路径，减少导航次数，最大化效率
"""

from typing import List, Dict, Tuple
from collections import defaultdict
from core.bottle_manager import get_bottle_manager, BottleInfo
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()


class TaskOptimizer:
    """任务优化器"""
    
    def __init__(self):
        self.bottle_manager = get_bottle_manager()
        self.back_platform_capacity = 4  # 后部平台容量（4个瓶子）
    
    def optimize_pickup_task(self, bottle_ids: List[str]) -> Tuple[Dict, List[str]]:
        """
        优化拾取任务
        
        参数:
            bottle_ids: 需要拾取的瓶子ID列表
        
        返回:
            (task_list, failed_bottles)
            task_list: {navigation_pose: [bottle_id, ...]}
            failed_bottles: 无法拾取的瓶子ID列表（超过容量限制）
        """
        logger.info("任务优化器", f"开始优化PICK_UP任务，瓶子数量: {len(bottle_ids)}")
        
        task_list = defaultdict(list)
        failed_bottles = []
        total_count = 0
        
        # 按导航点位分组
        nav_groups = defaultdict(list)
        for bottle_id in bottle_ids:
            bottle = self.bottle_manager.get_bottle(bottle_id)
            if not bottle:
                logger.warning("任务优化器", f"瓶子不存在: {bottle_id}")
                failed_bottles.append(bottle_id)
                continue
            
            nav_groups[bottle.navigation_pose].append(bottle)
        
        # 为每个导航点位安排任务
        for nav_pose, bottles in nav_groups.items():
            # 检查后部平台容量
            for bottle in bottles:
                if total_count >= self.back_platform_capacity:
                    logger.warning("任务优化器", f"后部平台已满，无法拾取: {bottle.bottle_id}")
                    failed_bottles.append(bottle.bottle_id)
                    continue
                
                # 检查目标点位是否可用
                target_pose = self.bottle_manager.get_target_pose(bottle.target_pose)
                if not target_pose or not target_pose.can_add(1):
                    logger.warning("任务优化器", f"目标点位已满: {bottle.target_pose}")
                    failed_bottles.append(bottle.bottle_id)
                    continue
                
                task_list[nav_pose].append(bottle.bottle_id)
                total_count += 1
        
        logger.info("任务优化器", 
                   f"PICK_UP优化完成 - 成功: {total_count}, 失败: {len(failed_bottles)}")
        logger.info("任务优化器", f"导航点位数量: {len(task_list)}")
        
        return dict(task_list), failed_bottles
    
    def optimize_put_task(self, release_params: List[Dict]) -> Tuple[Dict, List[str]]:
        """
        优化放置任务
        
        参数:
            release_params: [{"bottle_id": xxx, "release_pose": xxx}, ...]
        
        返回:
            (task_list, failed_bottles)
            task_list: {navigation_pose: [(bottle_id, release_pose), ...]}
            failed_bottles: 无法放置的瓶子ID列表
        """
        logger.info("任务优化器", f"开始优化PUT_TO任务，数量: {len(release_params)}")
        
        task_list = defaultdict(list)
        failed_bottles = []
        
        # 根据release_pose对应的navigation_pose分组
        for param in release_params:
            bottle_id = param["bottle_id"]
            release_pose = param["release_pose"]
            
            bottle = self.bottle_manager.get_bottle(bottle_id)
            if not bottle:
                logger.warning("任务优化器", f"瓶子不存在: {bottle_id}")
                failed_bottles.append(bottle_id)
                continue
            
            # 获取release_pose对应的导航点位
            # 假设release_pose的命名规则：{nav_pose}_temp_{type}_{id}
            # 例如：worktable_temp_001 -> 导航点位是 worktable
            nav_pose = self._extract_navigation_pose(release_pose)
            
            # 检查目标点位容量
            target_pose_obj = self.bottle_manager.get_target_pose(release_pose)
            if target_pose_obj and target_pose_obj.is_full():
                logger.warning("任务优化器", f"目标点位已满: {release_pose}")
                failed_bottles.append(bottle_id)
                continue
            
            task_list[nav_pose].append((bottle_id, release_pose))
        
        logger.info("任务优化器", 
                   f"PUT_TO优化完成 - 成功: {sum(len(v) for v in task_list.values())}, 失败: {len(failed_bottles)}")
        
        return dict(task_list), failed_bottles
    
    def optimize_transfer_task(self, target_params: List[Dict], 
                               release_params: List[Dict]) -> Tuple[List[Dict], List[str]]:
        """
        优化转移任务（TAKE_BOTTLE_FROM_SP_TO_SP）
        
        参数:
            target_params: 拾取参数列表
            release_params: 放置参数列表
        
        返回:
            (task_list2, failed_bottles)
            task_list2: [{"pick": {nav: [bottles]}, "put": {nav: [(id, pose)]}}]
            failed_bottles: 失败的瓶子ID列表
        """
        logger.info("任务优化器", 
                   f"开始优化TRANSFER任务 - 拾取: {len(target_params)}, 放置: {len(release_params)}")
        
        task_list2 = []
        failed_bottles = []
        
        # 创建bottle_id到release_pose的映射
        release_map = {param["bottle_id"]: param["release_pose"] 
                      for param in release_params}
        
        # 按批次处理，每批次尽量填满后部平台
        remaining_bottles = [p["bottle_id"] for p in target_params]
        
        while remaining_bottles:
            # 本批次的拾取和放置
            batch_pick = {}
            batch_put = defaultdict(list)
            batch_bottles = []
            
            # 按导航点位分组拾取
            pick_groups = defaultdict(list)
            for bottle_id in remaining_bottles:
                bottle = self.bottle_manager.get_bottle(bottle_id)
                if bottle:
                    pick_groups[bottle.navigation_pose].append(bottle)
            
            # 尽量填满后部平台
            current_capacity = 0
            for nav_pose, bottles in pick_groups.items():
                if nav_pose not in batch_pick:
                    batch_pick[nav_pose] = []
                
                for bottle in bottles:
                    if current_capacity >= self.back_platform_capacity:
                        break
                    
                    batch_pick[nav_pose].append(bottle.bottle_id)
                    batch_bottles.append(bottle.bottle_id)
                    current_capacity += 1
                
                if current_capacity >= self.back_platform_capacity:
                    break
            
            # 为本批次的瓶子安排放置任务
            # 按release_pose的导航点位分组
            for bottle_id in batch_bottles:
                if bottle_id not in release_map:
                    logger.warning("任务优化器", f"瓶子没有对应的放置点位: {bottle_id}")
                    failed_bottles.append(bottle_id)
                    continue
                
                release_pose = release_map[bottle_id]
                nav_pose = self._extract_navigation_pose(release_pose)
                batch_put[nav_pose].append((bottle_id, release_pose))
            
            # 添加到总任务列表
            if batch_pick and batch_put:
                task_list2.append({
                    "pick": batch_pick,
                    "put": dict(batch_put)
                })
            
            # 从待处理列表中移除已处理的瓶子
            for bottle_id in batch_bottles:
                if bottle_id in remaining_bottles:
                    remaining_bottles.remove(bottle_id)
            
            # 防止无限循环
            if not batch_bottles:
                logger.error("任务优化器", "无法继续处理剩余瓶子")
                failed_bottles.extend(remaining_bottles)
                break
        
        logger.info("任务优化器", 
                   f"TRANSFER优化完成 - 批次数: {len(task_list2)}, 失败: {len(failed_bottles)}")
        
        return task_list2, failed_bottles
    
    def _extract_navigation_pose(self, pose_name: str) -> str:
        """
        从点位名称提取导航点位
        例如：shelf_temp_1000_001 -> shelf
             worktable_temp_001 -> worktable
        """
        # 简单的命名规则解析
        parts = pose_name.split('_')
        if len(parts) >= 1:
            return parts[0]
        return pose_name
    
    def validate_bottle_capacity(self, bottle_ids: List[str]) -> Tuple[bool, str]:
        """
        验证瓶子数量是否超过容量限制
        
        返回:
            (is_valid, message)
        """
        if len(bottle_ids) > self.back_platform_capacity:
            return False, f"瓶子数量超过后部平台容量限制({self.back_platform_capacity})"
        return True, "容量检查通过"


# 全局任务优化器实例
_task_optimizer = None

def get_task_optimizer():
    """获取任务优化器实例（单例）"""
    global _task_optimizer
    if _task_optimizer is None:
        _task_optimizer = TaskOptimizer()
    return _task_optimizer

