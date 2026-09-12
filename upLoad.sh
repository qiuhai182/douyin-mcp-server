#!/bin/bash

# 只处理 .git/config 中真实存在的远端，避免某个远端缺失时整个上传流程中断
remotes=()
for r in gitee github; do
    if git remote get-url "$r" >/dev/null 2>&1; then
        remotes+=("$r")
    else
        echo "跳过未配置的远端: $r"
    fi
done

for r in "${remotes[@]}"; do
    git pull "$r" main
done

python convert_to_utf8.py
git add --all -- ':!nul'
git commit -m "快捷上传最新可执行文件、代码"

for r in "${remotes[@]}"; do
    git push "$r"
done
