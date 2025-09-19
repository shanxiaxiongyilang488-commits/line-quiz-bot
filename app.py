import os
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)

# ---------- 基本設定 ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quiz-bot")

app = Flask(__name__)

LINE_TOKEN = os.environ["CHANNEL_ACCESS_TOKEN"]
LINE_SECRET = os.environ["CHANNEL_SECRET"]
line_bot_api = LineBotApi(LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# ---------- 定数 ----------
CMD_DONE  = "__DONE__"
CMD_CLEAR = "__CLEAR__"
CMD_SKIP  = "__SKIP__"

# ---------- ユーザー状態 ----------
STATE: Dict[str, Dict[str, Any]] = {}

# ---------- 質問読み込み ----------
def load_questions() -> List[Dict[str, Any]]:
    p = Path(__file__).with_name("questions_30.json")
    with p.open(encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("questions_30.json is empty or invalid")

    try:
        data.sort(key=lambda q: int(q.get("id", 0)))
    except Exception:
        pass
    return data

QUESTIONS = load_questions()

# ---------- ユーティリティ ----------
def q_kind(q: Dict[str, Any]) -> str:
    if q.get("type") == "input":
        return "input"
    if q.get("type") in ("single", "multi"):
        return q["type"]
    if q.get("options"):
        return "single"
    if q.get("choices"):
        return "single"
    return "input"

def q_title(q: Dict[str, Any]) -> str:
    return q.get("title") or q.get("question") or "質問"

def q_desc(q: Dict[str, Any]) -> Optional[str]:
    return q.get("desc")

def q_id(q: Dict[str, Any]) -> Any:
    return q.get("id")

def q_choices(q: Dict[str, Any]) -> List[str]:
    if "choices" in q and isinstance(q["choices"], list):
        return [str(c) for c in q["choices"]]
    if "options" in q and isinstance(q["options"], list):
        return [str(opt.get("text")) for opt in q["options"] if opt.get("text")]
    return []

def choice_to_value(q: Dict[str, Any], label: str) -> Any:
    if "options" in q and isinstance(q["options"], list):
        for opt in q["options"]:
            if opt.get("text") == label:
                return opt.get("tag") or opt.get("text")
    return label

def build_question_text(q: Dict[str, Any]) -> str:
    lines = [f"Q{q_id(q)}. {q_title(q)}"]
    if q_desc(q):
        lines.append(f"説明: {q_desc(q)}")
    return "\n".join(lines)

def make_quick_reply_for_single(choices: List[str]) -> QuickReply:
    items = [QuickReplyButton(action=MessageAction(label=c[:20], text=c)) for c in choices[:13]]
    if len(items) < 13:
        items.append(QuickReplyButton(action=MessageAction(label="⏭ スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)

def make_quick_reply_for_multi(choices: List[str], selected_count: int) -> QuickReply:
    items = [QuickReplyButton(action=MessageAction(label=c[:20], text=c)) for c in choices[:9]]
    if len(items) < 13:
        items.append(QuickReplyButton(action=MessageAction(label=f"✅ 完了 ({selected_count})", text=CMD_DONE)))
    if len(items) < 13:
        items.append(QuickReplyButton(action=MessageAction(label="↩ クリア", text=CMD_CLEAR)))
    if len(items) < 13:
        items.append(QuickReplyButton(action=MessageAction(label="⏭ スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)

def send_question(user_id: str, idx: int) -> None:
    if idx < 0 or idx >= len(QUESTIONS):
        line_bot_api.push_message(user_id, TextSendMessage(text="全問終了！「結果」と送ると回答を表示します。"))
        return

    q = QUESTIONS[idx]
    kind = q_kind(q)
    text = build_question_text(q)

    if kind == "input":
        STATE[user_id]["await_input"] = True
        line_bot_api.push_message(user_id, TextSendMessage(text=text + "\n\n自由入力で送ってください。"))
        return

    labels = q_choices(q)
    if not labels:
        STATE[user_id]["await_input"] = True
        line_bot_api.push_message(user_id, TextSendMessage(text=text + "\n\n自由入力で送ってください。"))
        return

    if kind == "single":
        qr = make_quick_reply_for_single(labels)
        line_bot_api.push_message(user_id, TextSendMessage(text=text, quick_reply=qr))
    else:
        STATE[user_id]["multi_selected"] = set()
        qr = make_quick_reply_for_multi(labels, 0)
        line_bot_api.push_message(user_id, TextSendMessage(
            text=text + "\n（複数選択可：押すたびにON/OFF。最後に「✅ 完了」で確定）",
            quick_reply=qr
        ))

def start_for(user_id: str) -> None:
    STATE[user_id] = {"pos": 0, "answers": {}, "multi_selected": set(), "await_input": False}
    send_question(user_id, 0)

def advance(user_id: str) -> None:
    STATE[user_id]["pos"] += 1
    pos = STATE[user_id]["pos"]
    if pos < len(QUESTIONS):
        send_question(user_id, pos)
    else:
        line_bot_api.push_message(user_id, TextSendMessage(text="全問終了！「結果」と送ると回答を表示します。"))

# ---------- ルーティング ----------
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info("BODY: %s", body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.exception("Invalid signature")
        abort(401)
    except Exception:
        logger.exception("Unhandled error in callback")
    return "OK", 200

# ---------- メッセージ受信 ----------
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_id = event.source.user_id or event.source.sender_id
    text = (event.message.text or "").strip()

    if user_id not in STATE:
        STATE[user_id] = {"pos": None, "answers": {}, "multi_selected": set(), "await_input": False}

    st = STATE[user_id]

    if text in ("開始", "start"):
        start_for(user_id)
        return

    if text == "結果":
        ans = st.get("answers", {})
        lines = []
        for q in QUESTIONS:
            qid = q_id(q)
            title = q_title(q)
            val = ans.get(qid)
            if val is None or val == "" or val == []:
                lines.append(f"Q{qid}. {title} → （未回答）")
            elif isinstance(val, list):
                lines.append(f"Q{qid}. {title} → {', '.join(map(str, val))}")
            else:
                lines.append(f"Q{qid}. {title} → {val}")
        pretty = "\n".join(lines)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=pretty))
        return

    if st.get("pos") is None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="クイズを始めるには「開始」と送ってください。"))
        return

    pos = st["pos"]
    if pos >= len(QUESTIONS):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="全問終了しました。「結果」と送ると回答を表示します。"))
        return

    q = QUESTIONS[pos]
    kind = q_kind(q)

    # ---- input ----
    if kind == "input" or st.get("await_input"):
        if text in (CMD_DONE, CMD_CLEAR, CMD_SKIP):
            if text == CMD_SKIP:
                st["answers"][q_id(q)] = ""
                st["await_input"] = False
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="スキップしました。"))
                advance(user_id)
                return
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="入力を送ってください。"))
            return

        st["answers"][q_id(q)] = text
        st["await_input"] = False
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="入力を受け付けました。"))
        advance(user_id)
        return

    # ---- single ----
    if kind == "single":
        labels = q_choices(q)
        if text == CMD_SKIP:
            st["answers"][q_id(q)] = None
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="スキップしました。"))
            advance(user_id)
            return

        if text in labels:
            value = choice_to_value(q, text)
            st["answers"][q_id(q)] = value
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"選択：{text}"))
            advance(user_id)
            return

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="選択肢をタップしてください。"))
        return

    # ---- multi ----
    if kind == "multi":
        labels = set(q_choices(q))

        if text == CMD_CLEAR:
            st["multi_selected"].clear()
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="選択をクリアしました。", quick_reply=make_quick_reply_for_multi(list(labels), 0))
            )
            return

        if text == CMD_SKIP:
            st["answers"][q_id(q)] = []
            st["multi_selected"].clear()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="スキップしました。"))
            advance(user_id)
            return

        if text == CMD_DONE:
            chosen = list(st["multi_selected"])
            min_required = q.get("min")
            max_allowed = q.get("max")

            if min_required and len(chosen) < min_required:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"{min_required}個以上選んでください。現在: {len(chosen)}")
                )
                return
            if max_allowed and len(chosen) > max_allowed:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"{max_allowed}個までにしてください。現在: {len(chosen)}")
                )
                return

            st["answers"][q_id(q)] = [choice_to_value(q, lab) for lab in chosen]
            st["multi_selected"].clear()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"確定：{', '.join(chosen) if chosen else '（なし）'}"))
            advance(user_id)
            return

        if text in labels:
            if text in st["multi_selected"]:
                st["multi_selected"].remove(text)
                msg = f"解除：{text}\n現在：{', '.join(st['multi_selected']) or '（なし）'}"
            else:
                st["multi_selected"].add(text)
                msg = f"選択：{text}\n現在：{', '.join(st['multi_selected'])}"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=msg, quick_reply=make_quick_reply_for_multi(list(labels), len(st["multi_selected"])))
            )
            return

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="選択してから「✅ 完了」を押してください。"))
        return

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="操作が分からない場合は「開始」と送ってください。"))

# ---------- ローカル実行 ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

