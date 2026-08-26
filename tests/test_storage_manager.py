"""
存储管理器测试脚本
测试后部暂存区状态管理功能
"""

import sys
import json
from storage_manager import init_storage_manager, get_storage_manager

def test_storage_manager():
    """测试存储管理器的各项功能"""
    
    print("="*70)
    print("存储管理器功能测试")
    print("="*70)
    
    # 1. 初始化（重置为空）
    print("\n[测试1] 初始化存储管理器（重置模式）")
    print("-"*70)
    init_storage_manager(reset=True)
    storage_mgr = get_storage_manager()
    print("✓ 初始化完成")
    print(storage_mgr.display_storage_status())
    
    # 2. 测试更新槽位
    print("\n[测试2] 更新槽位")
    print("-"*70)
    
    test_bottles = [
        ("glass_bottle_1000", 0, "glass_bottle_1000_001"),
        ("glass_bottle_1000", 1, "glass_bottle_1000_002"),
        ("glass_bottle_500", 0, "glass_bottle_500_001"),
        ("glass_bottle_250", 0, "glass_bottle_250_001"),
    ]
    
    for bottle_type, slot_index, bottle_id in test_bottles:
        result = storage_mgr.update_slot(bottle_type, slot_index, bottle_id)
        if result:
            print(f"✓ 放置成功: {bottle_id} -> {bottle_type}[{slot_index}]")
        else:
            print(f"✗ 放置失败: {bottle_id}")
    
    print("\n当前状态:")
    print(storage_mgr.display_storage_status())
    
    # 3. 测试查找空槽位
    print("\n[测试3] 查找空槽位")
    print("-"*70)
    for bottle_type in ["glass_bottle_1000", "glass_bottle_500", "glass_bottle_250"]:
        empty_index = storage_mgr.get_empty_slot_index(bottle_type)
        if empty_index is not None:
            print(f"✓ {bottle_type} 的第一个空槽位: [{empty_index}]")
        else:
            print(f"✗ {bottle_type} 暂存区已满")
    
    # 4. 测试检查是否已满
    print("\n[测试4] 检查暂存区是否已满")
    print("-"*70)
    print(f"所有暂存区是否已满: {storage_mgr.is_full()}")
    print(f"glass_bottle_1000 是否已满: {storage_mgr.is_type_full('glass_bottle_1000')}")
    print(f"glass_bottle_500 是否已满: {storage_mgr.is_type_full('glass_bottle_500')}")
    print(f"glass_bottle_250 是否已满: {storage_mgr.is_type_full('glass_bottle_250')}")
    
    # 5. 填满 glass_bottle_1000
    print("\n[测试5] 填满 glass_bottle_1000 暂存区")
    print("-"*70)
    storage_mgr.update_slot("glass_bottle_1000", 2, "glass_bottle_1000_003")
    storage_mgr.update_slot("glass_bottle_1000", 3, "glass_bottle_1000_004")
    print(f"glass_bottle_1000 是否已满: {storage_mgr.is_type_full('glass_bottle_1000')}")
    
    # 6. 测试清空槽位
    print("\n[测试6] 清空槽位")
    print("-"*70)
    storage_mgr.clear_slot("glass_bottle_1000", 0)
    print("✓ 已清空 glass_bottle_1000[0]")
    print(f"glass_bottle_1000 是否已满: {storage_mgr.is_type_full('glass_bottle_1000')}")
    
    print("\n当前状态:")
    print(storage_mgr.display_storage_status())
    
    # 7. 测试状态保存
    print("\n[测试7] 状态保存")
    print("-"*70)
    result = storage_mgr.save_storage()
    if result:
        print("✓ 状态已保存到 storage_state.json")
        
        # 读取文件验证
        with open("storage_state.json", 'r', encoding='utf-8') as f:
            saved_data = json.load(f)
        print("\n已保存的数据:")
        print(json.dumps(saved_data, indent=2, ensure_ascii=False))
    else:
        print("✗ 状态保存失败")
    
    # 8. 测试重新加载
    print("\n[测试8] 重新加载状态")
    print("-"*70)
    
    # 重新初始化（不重置）
    init_storage_manager(reset=False)
    storage_mgr = get_storage_manager()
    print("✓ 已重新加载状态")
    print(storage_mgr.display_storage_status())
    
    # 9. 测试填满所有暂存区
    print("\n[测试9] 填满所有暂存区")
    print("-"*70)
    
    # 填满 glass_bottle_1000（还差一个）
    storage_mgr.update_slot("glass_bottle_1000", 0, "glass_bottle_1000_005")
    
    # 填满 glass_bottle_500
    for i in range(1, 4):
        storage_mgr.update_slot("glass_bottle_500", i, f"glass_bottle_500_{i+1:03d}")
    
    # 填满 glass_bottle_250
    for i in range(4):
        storage_mgr.update_slot("glass_bottle_250", i, f"glass_bottle_250_{i+1:03d}")
    
    print(storage_mgr.display_storage_status())
    print(f"\n所有暂存区是否已满: {storage_mgr.is_full()}")
    
    # 10. 测试重置
    print("\n[测试10] 重置暂存区")
    print("-"*70)
    choice = input("是否重置暂存区？(y/n): ").strip().lower()
    if choice == 'y':
        storage_mgr.reset_storage()
        storage_mgr.save_storage()
        print("✓ 暂存区已重置")
        print(storage_mgr.display_storage_status())
    else:
        print("已跳过重置")
    
    print("\n"+"="*70)
    print("测试完成！")
    print("="*70)


if __name__ == "__main__":
    try:
        test_storage_manager()
    except KeyboardInterrupt:
        print("\n\n测试被中断")
    except Exception as e:
        print(f"\n测试出错: {e}")
        import traceback
        traceback.print_exc()

