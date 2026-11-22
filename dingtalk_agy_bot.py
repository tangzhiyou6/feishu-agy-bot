#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "dingtalk-stream>=0.24.0",
#     "requests>=2.31.0",
# ]
# ///

import json
import os
import subprocess
import sys
import threading
import time
import uuid
import requests
import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage

# ================= 配置区 =================
CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID", "")          # 应用 AppKey / Client ID
CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET", "")  # 应用 AppSecret / Client Secret
ROBOT_CODE = os.getenv("DINGTALK_ROBOT_CODE", CLIENT_ID) # 机器人 Code（通常与 Client ID 一致）
ALLOWED_STAFF_ID = os.getenv("DINGTALK_ALLOWED_STAFF_ID", "")  # 允许交互的 senderStaffId
# 可选：钉钉 AI 卡片模板 ID（若留空，则自动降级为普通 Markdown 最终一次性回复）
CARD_TEMPLATE_ID = os.getenv("DINGTALK_CARD_TEMPLATE_ID", "")

WORK_DIR = os.getenv("AGY_WORK_DIR", os.path.expanduser("~"))
SAVE_PATH = os.getenv("IMG_SAVE_PATH", "/tmp/latest.png")
FILE_SAVE_DIR = "/tmp/dingtalk_files"
os.makedirs(FILE_SAVE_DIR, exist_ok=True)

if not CLIENT_ID or not CLIENT_SECRET:
    print("[错误] 缺少 DINGTALK_CLIENT_ID 或 DINGTALK_CLIENT_SECRET 环境变量", file=sys.stderr, flush=True)
    sys.exit(1)

current_conversation_id = None
agy_lock = threading.Lock()


# ================= Token 管理 =================
class TokenManager:
    """线程安全的钉钉 AccessToken 缓存管理"""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = ""
        self._expire_at = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expire_at - 120:
                return self._token
            resp = requests.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": self.client_id, "appSecret": self.client_secret},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["accessToken"]
            self._expire_at = time.monotonic() + int(data.get("expireIn", 7200))
            return self._token


token_mgr = TokenManager(CLIENT_ID, CLIENT_SECRET)


def dt_headers() -> dict:
    return {
        "x-acs-dingtalk-access-token": token_mgr.get_token(),
        "Content-Type": "application/json",
    }


# ================= 消息与卡片发送 =================
def reply_text_webhook(session_webhook: str, text: str) -> None:
    """通过 sessionWebhook 快速回复纯文本"""
    requests.post(
        session_webhook,
        json={"msgtype": "text", "text": {"content": text}},
        timeout=10,
    )


def reply_markdown_webhook(session_webhook: str, title: str, content: str) -> None:
    """通过 sessionWebhook 回复 Markdown（无卡片模板时的兜底方案）"""
    requests.post(
        session_webhook,
        json={
            "msgtype": "markdown",
            "markdown": {"title": title, "text": content[:4000] or "（无输出）"},
        },
        timeout=10,
    )


def create_and_deliver_ai_card(incoming_msg: ChatbotMessage, init_text: str) -> str | None:
    """创建并投放钉钉 AI 流式卡片，返回 outTrackId"""
    out_track_id = f"agy_{uuid.uuid4().hex}"
    is_group = incoming_msg.conversation_type == "2"

    open_space_id = (
        f"dtv1.card//IM_GROUP.{incoming_msg.conversation_id}"
        if is_group
        else f"dtv1.card//IM_ROBOT.{incoming_msg.sender_staff_id}"
    )

    payload = {
        "cardTemplateId": CARD_TEMPLATE_ID,
        "outTrackId": out_track_id,
        "cardData": {"cardParamMap": {"content": init_text}},
        "imGroupOpenSpaceModel": {"supportForward": True},
        "imRobotOpenSpaceModel": {"supportForward": True},
        "openSpaceId": open_space_id,
        "userIdType": 1,
    }
    if is_group:
        payload["imGroupOpenDeliverModel"] = {"robotCode": ROBOT_CODE}
    else:
        payload["imRobotOpenDeliverModel"] = {"spaceType": "IM_ROBOT"}

    resp = requests.post(
        "https://api.dingtalk.com/v1.0/card/instances/createAndDeliver",
        headers=dt_headers(),
        json=payload,
        timeout=10,
    )
    if resp.status_code == 200:
        return out_track_id
    print(f"[错误] 创建 AI 卡片失败: {resp.status_code} {resp.text}", flush=True)
    return None


def stream_update_ai_card(out_track_id: str, content: str, is_finalize: bool = False) -> None:
    """流式更新钉钉 AI 卡片内容"""
    display_text = (content[:3900] + "\n\n▌") if not is_finalize else (content[:3950] or "（无输出）")
    payload = {
        "outTrackId": out_track_id,
        "guid": uuid.uuid4().hex,
        "key": "content",  # 需与钉钉卡片模板中的 Markdown 变量名保持一致
        "content": display_text,
        "isFull": True,
        "isFinalize": is_finalize,
        "isError": False,
    }
    resp = requests.put(
        "https://api.dingtalk.com/v1.0/card/streaming",
        headers=dt_headers(),
        json=payload,
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"[错误] 更新 AI 卡片失败: {resp.status_code} {resp.text}", flush=True)


# ================= 文件下载与回传 =================
def download_robot_file(download_code: str, save_path: str) -> tuple[bool, str]:
    """通过 downloadCode 下载用户发给机器人的图片或文件"""
    try:
        resp = requests.post(
            "https://api.dingtalk.com/v1.0/robot/messageFiles/download",
            headers=dt_headers(),
            json={"downloadCode": download_code, "robotCode": ROBOT_CODE},
            timeout=15,
        )
        if resp.status_code != 200:
            return False, f"获取下载链接失败: {resp.text}"
        download_url = resp.json().get("downloadUrl")
        if not download_url:
            return False, "返回数据中缺少 downloadUrl"

        file_resp = requests.get(download_url, timeout=60)
        file_resp.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(file_resp.content)
        return True, save_path
    except Exception as e:
        return False, str(e)


def send_file_to_dingtalk(incoming_msg: ChatbotMessage, file_path: str) -> None:
    """将服务器本地文件上传到钉钉并回传给用户（支持单聊/群聊）"""
    webhook = incoming_msg.session_webhook
    if not os.path.isfile(file_path):
        reply_text_webhook(webhook, f"找不到文件：{file_path}")
        return

    file_name = os.path.basename(file_path)
    file_ext = os.path.splitext(file_name)[1].lstrip(".") or "bin"

    try:
        # 1. 上传媒体文件获取 media_id
        token = token_mgr.get_token()
        with open(file_path, "rb") as f:
            upload_resp = requests.post(
                f"https://oapi.dingtalk.com/media/upload?access_token={token}&type=file",
                files={"media": (file_name, f)},
                timeout=60,
            )
        upload_data = upload_resp.json()
        media_id = upload_data.get("media_id")
        if not media_id:
            reply_text_webhook(webhook, f"文件上传钉钉失败：{upload_data}")
            return

        # 2. 按单聊 / 群聊调用对应发送接口
        msg_param = json.dumps(
            {"mediaId": media_id, "fileName": file_name, "fileType": file_ext},
            ensure_ascii=False,
        )
        if incoming_msg.conversation_type == "2":
            # 群聊
            send_resp = requests.post(
                "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                headers=dt_headers(),
                json={
                    "robotCode": ROBOT_CODE,
                    "openConversationId": incoming_msg.conversation_id,
                    "msgKey": "sampleFile",
                    "msgParam": msg_param,
                },
                timeout=15,
            )
        else:
            # 单聊
            send_resp = requests.post(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                headers=dt_headers(),
                json={
                    "robotCode": ROBOT_CODE,
                    "userIds": [incoming_msg.sender_staff_id],
                    "msgKey": "sampleFile",
                    "msgParam": msg_param,
                },
                timeout=15,
            )

        if send_resp.status_code != 200:
            reply_text_webhook(webhook, f"发送文件失败：{send_resp.text}")
    except Exception as e:
        reply_text_webhook(webhook, f"回传文件异常：{e}")


# ================= agy 流式执行 =================
def run_agy_stream_and_reply(incoming_msg: ChatbotMessage, prompt: str) -> None:
    global current_conversation_id

    use_card = bool(CARD_TEMPLATE_ID)
    out_track_id = None

    if use_card:
        out_track_id = create_and_deliver_ai_card(incoming_msg, "正在思考...")
        if not out_track_id:
            use_card = False  # 建卡失败时自动降级为普通 Markdown
    if not use_card:
        reply_text_webhook(incoming_msg.session_webhook, "正在思考...")

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

                # 每 0.8 秒节流刷新一次 AI 卡片
                now = time.monotonic()
                if use_card and out_track_id and buffer and (now - last_update) >= 0.8:
                    stream_update_ai_card(out_track_id, buffer, is_finalize=False)
                    last_update = now

            proc.wait(timeout=180)
            stderr_out = proc.stderr.read().strip() if proc.stderr else ""
            final_text = final_response or buffer or stderr_out or "（无输出）"

        except subprocess.TimeoutExpired:
            proc.kill()
            final_text = (buffer + "\n\n[执行超时已终止]").strip()
        except Exception as e:
            final_text = f"调用 agy 失败: {e}"

    if use_card and out_track_id:
        stream_update_ai_card(out_track_id, final_text, is_finalize=True)
    else:
        reply_markdown_webhook(incoming_msg.session_webhook, "Antigravity 回复", final_text)


# ================= 钉钉消息事件处理 =================
class AgyBotHandler(ChatbotHandler):
    async def process(self, callback: dingtalk_stream.CallbackMessage):
        global current_conversation_id

        incoming_msg = ChatbotMessage.from_dict(callback.data)
        sender_id = incoming_msg.sender_staff_id
        webhook = incoming_msg.session_webhook

        # 1. 白名单校验
        if not ALLOWED_STAFF_ID:
            print(
                f"[提示] 收到来自 staff_id={sender_id} 的消息，请填入 DINGTALK_ALLOWED_STAFF_ID",
                flush=True,
            )
            reply_text_webhook(webhook, f"未配置白名单，你的 staff_id 是：{sender_id}")
            return AckMessage.STATUS_OK, "OK"
        if sender_id != ALLOWED_STAFF_ID:
            return AckMessage.STATUS_OK, "OK"

        msg_type = incoming_msg.message_type
        raw_data = callback.data

        # 2. 处理图片消息
        if msg_type == "picture":
            download_code = raw_data.get("content", {}).get("downloadCode")
            if download_code:
                ok, info = download_robot_file(download_code, SAVE_PATH)
                reply_text_webhook(
                    webhook,
                    f"图片已保存到 {info}" if ok else f"图片下载失败：{info}",
                )

        # 3. 处理文件消息
        elif msg_type == "file":
            content = raw_data.get("content", {})
            download_code = content.get("downloadCode")
            file_name = content.get("fileName") or f"file_{int(time.time())}"
            if download_code:
                save_path = os.path.join(FILE_SAVE_DIR, file_name)
                ok, info = download_robot_file(download_code, save_path)
                reply_text_webhook(
                    webhook,
                    f"文件已保存到：{info}\n你可以直接发消息让 agy 转换它。"
                    if ok
                    else f"文件下载失败：{info}",
                )

        # 4. 处理文本消息
        elif msg_type == "text":
            text = (incoming_msg.text.content or "").strip()
            if not text:
                return AckMessage.STATUS_OK, "OK"

            if text.lower() in ("/new", "/reset"):
                current_conversation_id = None
                reply_text_webhook(webhook, "已开启新对话，之前的上下文已清空。")
                return AckMessage.STATUS_OK, "OK"

            if text.startswith("/send "):
                target_path = text[6:].strip()
                threading.Thread(
                    target=send_file_to_dingtalk,
                    args=(incoming_msg, target_path),
                    daemon=True,
                ).start()
                return AckMessage.STATUS_OK, "OK"

            threading.Thread(
                target=run_agy_stream_and_reply,
                args=(incoming_msg, text),
                daemon=True,
            ).start()

        return AckMessage.STATUS_OK, "OK"


if __name__ == "__main__":
    print("正在启动钉钉 Antigravity 流式助手 (uv)...", flush=True)
    credential = dingtalk_stream.Credential(CLIENT_ID, CLIENT_SECRET)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(ChatbotMessage.TOPIC, AgyBotHandler())
    client.start_forever()