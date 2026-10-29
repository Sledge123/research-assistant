"""在项目容器内使用私有 umask 启动指定源码角色，不展开 shell。"""
import os
import sys


os.umask(0o077)
if len(sys.argv) < 2:
    raise SystemExit("缺少源码启动命令。")
os.execvp(sys.argv[1], sys.argv[1:])
