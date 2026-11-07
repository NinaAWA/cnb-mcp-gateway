"""访问策略：默认完整管理员权限，危险防护仅作为可配置辅助层。"""
from .policy import AccessPolicy, get_policy

__all__ = ["AccessPolicy", "get_policy"]
