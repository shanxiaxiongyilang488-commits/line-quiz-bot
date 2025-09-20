import os
import json
from pathlib import Path
from typing import Dict, Any, List

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)

# ===== 基本設定 =====
app = Flask(__name__)
CHANNEL_ACCESS_TOKEN = os.environ["CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["CHANNEL_SECRET"]

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ===== 定数 =====
CMD_RESET = "__RESET__"
CMD_START = "__START__"
CMD_SKIP = "__SKIP__"
CMD_DONE = "__DONE__"
CMD_CLEAR = "__CLEAR__"

# ===== 状態管理 =====
STATE: Dict[str, Any] = {}

# ===== 質問データを読み込む =====
QUESTIONS_PATH = Path(__file__).parent / "questions_30.json"
with open(QUESTIONS_PATH, encoding="utf-8") as f:
    QUESTIONS: List[Dict[str, Any]] = json.load(f)

QMAX = len(QUESTIONS)


def new_state():
    return {
        "pos": 1,
        "answers": {}
    }


def q_id(q):
    return str(q.get("id", ""))


def q_choices(q):
    return q.get("choices", [])


def current_question(st):
    pos = st.get("pos", 1)
    if 1 <= pos <= QMAX:
        return QUESTIONS[pos - 1]
    return None


def advance(st):
    st["pos"] = st.get("pos", 1) + 1


def make_quick_reply(choices: List[str]) -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label=c, text=c))
        for c in choices
    ])


def make_quick_reply_for_multi(labels: List[str], selected_count: int) -> QuickReply:
    items = []
    for c in labels:
        items.append(QuickReplyButton(action=MessageAction(label=c, text=c)))
    # 追加で「クリア」「完了」「スキップ」
    items.append(QuickReplyButton(action=MessageAction(label="⇄ クリア", text=CMD_CLEAR)))
    items.append(QuickReplyButton(action=MessageAction(label="✅ 完了", text=CMD_DONE)))
    items.append(QuickReplyButton(action=MessageAction(label="⏭ スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)


# ===== Webhook =====
@app.route("/callback", methods=["POST"])
def callback():
    print(">>> Callback called!")  # デバッグ
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


# ===== メインハンドラ =====
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id or getattr(event.source, "sender_id", None)
    if not user_id:
        return

    text = (event.message.text or "").strip()

    # --- リセット ---
    if text in (CMD_RESET, "reset", "/reset", "リセット"):
        STATE.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="OK: リセット"))
        return

    # --- 開始 ---
    if text in (CMD_START, "start", "/start", "開始"):
        STATE[user_id] = new_state()
        send_question(user_id, event.reply_token)
        return

    # 初回メッセージでSTATEが無ければ新規作成
    if user_id not in STATE:
        STATE[user_id] = new_state()

    st = STATE[user_id]
    q = current_question(st)

    if not q:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="全ての質問が終了しました。"))
        return

    kind = q.get("kind", "text")

    # ---- multi ----
    if kind == "multi":
        labels = set(q_choices(q))
        st.setdefault("multi_selected", set())
        t = (text or "").strip()

        if t == CMD_CLEAR:
            st["multi_selected"].clear()
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="選択をクリアしました。"),
                quick_reply=make_quick_reply_for_multi(list(labels), len(st["multi_selected"]))
            )
            return

        if t == CMD_SKIP:
            st["answers"][q_id(q)] = []
            st["multi_selected"].clear()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="スキップしました。"))
            advance(st)
            send_question(user_id, event.reply_token)
            return

        if t == CMD_DONE:
            chosen = list(st["multi_selected"])
            min_required = int(q.get("min", 1))
            max_allowed = int(q.get("max", len(labels)))

            if len(chosen) < min_required:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"あと{min_required - len(chosen)}つ選んでください。"),
                )
                return
            if len(chosen) > max_allowed:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"最大{max_allowed}件までです。不要なものを外してください。"),
                )
                return

            st["answers"][q_id(q)] = chosen
            st["multi_selected"].clear()
            advance(st)
            send_question(user_id, event.reply_token)
            return

        if t in labels:
            if t in st["multi_selected"]:
                st["multi_selected"].remove(t)
                msg = f"解除：{t}\n現在：{', '.join(st['multi_selected']) or '（なし）'}"
            else:
                st["multi_selected"].add(t)
                msg = f"選択：{t}\n現在：{', '.join(st['multi_selected'])}"

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=msg),
                quick_reply=make_quick_reply_for_multi(list(labels), len(st["multi_selected"]))
            )
            return

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="候補をタップして選んでください。終わったら「✅ 完了」です。"),
            quick_reply=make_quick_reply_for_multi(list(labels), len(st["multi_selected"]))
        )
        return

    # ---- text ----
    if kind == "text":
        st["answers"][q_id(q)] = text
        advance(st)
        send_question(user_id, event.reply_token)
        return


def send_question(user_id, reply_token):
    st = STATE[user_id]
    q = current_question(st)
    if not q:
        done = f"回答ありがとうございました！（全{QMAX}問終了）"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=done))
        STATE.pop(user_id, None)
        return

    kind = q.get("kind", "text")
    title = f"Q{st['pos']}. {q.get('text', '')}"

    if kind == "multi":
        choices = q_choices(q)
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=title, quick_reply=make_quick_reply_for_multi(choices, 0))
        )
    elif kind == "text":
        line_bot_api.reply_message(reply_token, TextSendMessage(text=title))


# ===== ヘルスチェック =====
@app.get("/healthz")
def healthz():
    return "OK"

