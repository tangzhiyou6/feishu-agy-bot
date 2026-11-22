#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "lark-oapi>=1.4.0",
# ]
# ///

import json
import os
import subprocess
import sys
import threading
import time
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

APP_ID = os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
ALLOWED_OPEN_ID = os.getenv("FEISHU_ALLOWED_OPEN_ID", "")
WORK_DIR = os.getenv("AGY_WORK_DIR", os.path.expanduser("~"))
SAVE_PATH = os.getenv("IMG_SAVE_PATH", "/tmp/latest.png")
FILE_SAVE_DIR = "/tmp/feishu_files"
os.makedirs(FILE_SAVE_DIR, exist_ok=True)


if not APP_ID or not APP_SECRET:
    print("[错误] 缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET 环境变量", file=sys.stderr, flush=True)
    sys.exit(1)

api_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
current_conversation_id = None
agy_lock = threading.Lock()


def build_card(content: str, streaming: bool = False) -> str:
    """构造支持 Markdown 的飞书消息卡片"""
    display_text = (content[:3900] + "\n\n▌") if streaming else (content[:3950] or "（无输出）")
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "markdown",
                "content": display_text,
            }
        ],
    }
    return json.dumps(card, ensure_ascii=False)


def reply_text(message_id: str, text: str) -> None:
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .msg_type("text")
            .build()
        )
        .build()
    )
    api_client.im.v1.message.reply(req)


def reply_card(message_id: str, content: str, streaming: bool = True) -> str | None:
    """回复一张卡片并返回新卡片的 message_id"""
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(build_card(content, streaming=streaming))
            .msg_type("interactive")
            .build()
        )
        .build()
    )
    resp = api_client.im.v1.message.reply(req)
    if resp.success() and resp.data:
        return resp.data.message_id
    print(f"[错误] 发送卡片失败: code={resp.code}, msg={resp.msg}", flush=True)
    return None


def update_card(card_msg_id: str, content: str, streaming: bool = True) -> None:
    """更新已有卡片的内容"""
    req = (
        PatchMessageRequest.builder()
        .message_id(card_msg_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(build_card(content, streaming=streaming))
            .build()
        )
        .build()
    )
    resp = api_client.im.v1.message.patch(req)
    if not resp.success():
        print(f"[错误] 更新卡片失败: code={resp.code}, msg={resp.msg}", flush=True)


def run_agy_stream_and_reply(message_id: str, prompt: str) -> None:
    global current_conversation_id

    card_msg_id = reply_card(message_id, "正在思考...", streaming=True)
    if not card_msg_id:
        return

    with agy_lock:
        cmd = ["agy", "--output-format", "stream-json"]
        if current_conversation_id:
            cmd.extend(["--conversation", current_conversation_id])
        cmd.extend(["-p", prompt])

        buffer = ""
        final_response = ""
        last_update = time.monotonic()

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=WORK_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                # 过滤行首可能混入的终端控制字符（如 ^[[>1;95;0c）
                json_start = line.find("{")
                if json_start == -1:
                    continue
                try:
                    event = json.loads(line[json_start:])
                except json.JSONDecodeError:
                    continue

                ev_type = event.get("event")

                if ev_type == "init":
                    if event.get("conversation_id"):
                        current_conversation_id = event["conversation_id"]
                elif ev_type == "step_update":
                    step = event.get("step_update") or {}
                    if step.get("conversation_id"):
                        current_conversation_id = step["conversation_id"]
                    delta = step.get("text_delta", "")
                    if delta:
                        buffer += delta
                elif ev_type == "result":
                    res = event.get("result") or {}
                    if res.get("conversation_id"):
                        current_conversation_id = res["conversation_id"]
                    final_response = res.get("response", "")

                # 每 0.8 秒节流刷新一次飞书卡片
                now = time.monotonic()
                if buffer and (now - last_update) >= 0.8:
                    update_card(card_msg_id, buffer, streaming=True)
                    last_update = now

            proc.wait(timeout=180)
            stderr_out = proc.stderr.read().strip() if proc.stderr else ""
            final_text = final_response or buffer or stderr_out or "（无输出）"

        except subprocess.TimeoutExpired:
            proc.kill()
            final_text = (buffer + "\n\n[执行超时已终止]").strip()
        except Exception as e:
            final_text = f"调用 agy 失败: {e}"

    update_card(card_msg_id, final_text, streaming=False)


def handle_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    global current_conversation_id
    sender_id = data.event.sender.sender_id.open_id
    msg = data.event.message

    if not ALLOWED_OPEN_ID:
        print(f"[提示] 收到来自 open_id={sender_id} 的消息，请填入 .env 的 FEISHU_ALLOWED_OPEN_ID", flush=True)
        reply_text(msg.message_id, f"未配置白名单，你的 open_id 是：{sender_id}")
        return
    if sender_id != ALLOWED_OPEN_ID:
        return

    if msg.message_type == "image":
        image_key = json.loads(msg.content).get("image_key")
        if not image_key:
            return
        req = (
            GetMessageResourceRequest.builder()
            .message_id(msg.message_id)
            .file_key(image_key)
            .type("image")
            .build()
        )
        resp = api_client.im.v1.message_resource.get(req)
        if resp.success():
            with open(SAVE_PATH, "wb") as f:
                f.write(resp.file.read())
            reply_text(msg.message_id, f"图片已保存到 {SAVE_PATH}")
        else:
            reply_text(msg.message_id, f"图片下载失败: {resp.msg}")
    
    elif msg.message_type == "file":
        content = json.loads(msg.content)
        file_key = content.get("file_key")
        file_name = content.get("file_name") or f"file_{int(time.time())}"
        if not file_key:
            return

        req = (
            GetMessageResourceRequest.builder()
            .message_id(msg.message_id)
            .file_key(file_key)
            .type("file")
            .build()
        )
        resp = api_client.im.v1.message_resource.get(req)
        if resp.success():
            save_path = os.path.join(FILE_SAVE_DIR, file_name)
            with open(save_path, "wb") as f:
                f.write(resp.file.read())
            reply_text(msg.message_id, f"文件已保存到：{save_path}\n你可以直接发消息让 agy 转换它。")
        else:
            reply_text(msg.message_id, f"文件下载失败：{resp.msg}")

    elif msg.message_type == "text":
        text = json.loads(msg.content).get("text", "").strip()
        if not text:
            return
        if text.lower() in ("/new", "/reset"):
            current_conversation_id = None
            reply_text(msg.message_id, "已开启新对话，之前的上下文已清空。")
            return
        if text.startswith("/send "):
            target_path = text[6:].strip()
            reply_file(msg.message_id, target_path)
            return

        threading.Thread(
            target=run_agy_stream_and_reply, args=(msg.message_id, text), daemon=True
        ).start()


def reply_file(message_id: str, file_path: str) -> None:
    """将服务器上的文件上传到飞书并回复给用户"""
    if not os.path.isfile(file_path):
        reply_text(message_id, f"找不到文件：{file_path}")
        return

    file_name = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            upload_req = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type("stream")  # stream 适用于任意格式文件
                    .file_name(file_name)
                    .file(f)
                    .build()
                )
                .build()
            )
            upload_resp = api_client.im.v1.file.create(upload_req)

        if not upload_resp.success() or not upload_resp.data:
            reply_text(message_id, f"文件上传飞书失败：{upload_resp.msg}")
            return

        file_key = upload_resp.data.file_key
        send_req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps({"file_key": file_key}))
                .msg_type("file")
                .build()
            )
            .build()
        )
        send_resp = api_client.im.v1.message.reply(send_req)
        if not send_resp.success():
            reply_text(message_id, f"发送文件失败：{send_resp.msg}")
    except Exception as e:
        reply_text(message_id, f"回传文件异常：{e}")



event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(handle_message)
    .build()
)

if __name__ == "__main__":
    print("正在启动飞书 Antigravity 流式助手 (uv)...", flush=True)
    ws_client = lark.ws.Client(
        APP_ID, APP_SECRET, event_handler=event_handler, log_level=lark.LogLevel.INFO
    )
    ws_client.start()
