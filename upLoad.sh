#!/bin/bash

git pull gitee main
git pull github main
python convert_to_utf8.py
git add --all -- ':!nul'
git commit -m "快捷上传最新可执行文件、代码"
git push gitee
git push github
