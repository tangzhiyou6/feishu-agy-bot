3. 执行部署
赋予执行权限并运行部署脚本：
```Bash
    chmod +x deploy.sh && ./deploy.sh
```
填入你的飞书 FEISHU_APP_ID、FEISHU_APP_SECRET 和 FEISHU_ALLOWED_OPEN_ID：
```Bash
    nano /opt/feishu-agy-bot/.env
```
启动服务并验证运行状态：
```Bash
    sudo systemctl restart feishu-agy-bot
    journalctl -u feishu-agy-bot -n 50 -f
```
验证： 日志中出现 connected 字样，且在飞书发图或发文字后能收到机器人回复。


运行与使用
前台测试运行（顺便去飞书后台保存“使用长连接接收事件”配置）：
```Bash
python3 feishu_img_saver.py
```
验证： 终端输出 connected 字样，在飞书里搜索并给你的机器人发一张截图，终端打印 [成功] 已保存最新图片到 /tmp/latest.png。
挂到后台长期运行：
```Bash
nohup python3 feishu_img_saver.py > /tmp/feishu_bot.log 2>&1 &
```
在 agy 中使用：
以后手机或电脑截图直接发给这个飞书机器人，然后在 SSH 的 agy 终端里直接输入：
```Plaintext
@/tmp/latest.png 帮我看看这个报错
```

>1.发文件： 直接把文档、表格、音视频或图片文件拖进飞书发给机器人，机器人会回复路径（例如 /tmp/feishu_files/report.docx）。

>2.让 agy 转换： 发文字指令，例如：
用 libreoffice 把 /tmp/feishu_files/report.docx 转成 PDF，保存到 /tmp/feishu_files/report.pdf

>3.取回文件： 看到 agy 提示转换完成后，发送：
/send /tmp/feishu_files/report.pdf
机器人就会把转换好的 PDF 直接发到飞书聊天窗口里。




## 钉钉的运行

加上 --env-file .env 参数运行即可：
```Bash
uv run --env-file .env dingtalk_agy_bot.py
```

或者先在当前终端导入 .env 再运行：
```Bash
set -a && source .env && set +a && uv run dingtalk_agy_bot.py
```
>4.运行钉钉程序
uv run --env-file .env dingtalk_agy_bot.py