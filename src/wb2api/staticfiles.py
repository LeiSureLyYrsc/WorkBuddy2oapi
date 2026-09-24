"""静态资源解析与前端构建兜底模块。

提供前端构建产物（web/dist）目录查找、index.html 读取
以及未构建时的提示页面支持。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["NOT_BUILT_HTML", "find_dist_dir", "read_index"]

# 前端未构建时的暗色主题提示页面，与 Go 版本保持一致视觉体验
NOT_BUILT_HTML: str = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkBuddy GUI · 前端未构建</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1117;color:#e6e8ee;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px;box-sizing:border-box}
.card{max-width:640px;width:100%;background:#171a23;border:1px solid #262b38;border-radius:12px;padding:28px;box-sizing:border-box}
h1{font-size:19px;margin:0 0 12px;color:#ffffff}code{background:#0b0d13;padding:2px 6px;border-radius:5px;color:#7ee787}
pre{background:#0b0d13;padding:12px;border-radius:8px;overflow:auto;color:#7ee787;font-size:13px;line-height:1.5}
p{line-height:1.7;color:#a9b0c0;font-size:14px}
</style></head><body><div class="card">
<h1>前端产物尚未构建</h1>
<p>后端已正常运行，但服务目录内尚未检测到前端构建产物。请在项目根目录执行：</p>
<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>构建完成后刷新本页面即可。API 接口此时已可用。</p>
</div></body></html>"""


def find_dist_dir() -> Path | None:
    """定位前端构建产物 web/dist 目录。

    依次按优先级检查：
    1. 环境变量 WB2API_WEB_DIST
    2. 当前工作目录下的 web/dist (Path.cwd() / "web" / "dist")
    3. 仓库根目录下的 web/dist (Path(__file__).resolve().parents[2] / "web" / "dist")

    返回首个存在且包含 index.html 的有效目录；若未找到则返回 None。
    """
    candidates: list[Path] = []

    # 1. 检查环境变量
    env_dist = os.environ.get("WB2API_WEB_DIST")
    if env_dist and env_dist.strip():
        candidates.append(Path(env_dist.strip()).resolve())

    # 2. 检查当前工作目录
    candidates.append((Path.cwd() / "web" / "dist").resolve())

    # 3. 检查仓库根目录（src/wb2api/staticfiles.py 向上两级为仓库根目录）
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append((repo_root / "web" / "dist").resolve())

    for candidate in candidates:
        try:
            if candidate.is_dir() and (candidate / "index.html").is_file():
                return candidate
        except OSError:
            continue

    return None


def read_index(dist: Path) -> bytes | None:
    """读取指定 dist 目录下的 index.html 内容。

    若文件不存在或读取失败则返回 None。
    """
    try:
        index_file = dist / "index.html"
        if index_file.is_file():
            return index_file.read_bytes()
    except OSError:
        return None
    return None
