"""通用流式传输原语：论文下载与文件传输共用。"""
from .core import (GenericSourcePolicy, PaperSourcePolicy, TransferResult,
                   fetch_to_file, verify_pdf_file)

__all__ = ["GenericSourcePolicy", "PaperSourcePolicy", "TransferResult",
           "fetch_to_file", "verify_pdf_file"]
