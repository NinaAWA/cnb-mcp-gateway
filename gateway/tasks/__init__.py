"""长任务持久化、进程组终止与实验复现元数据。"""
from .manager import TaskManager, get_task_manager

__all__ = ["TaskManager", "get_task_manager"]
