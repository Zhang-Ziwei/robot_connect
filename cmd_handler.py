"""
兼容性转发模块 — 请勿在新代码中使用此文件。

cmd_handler 已迁移至 programs/TJSH/cmd_handler.py。
此文件仅保留以避免旧脚本因路径变更而崩溃，后续可安全删除。
"""
from programs.TJSH.cmd_handler import *  # noqa: F401, F403
from programs.TJSH.cmd_handler import init_cmd_handler, get_cmd_handler, CmdHandler
