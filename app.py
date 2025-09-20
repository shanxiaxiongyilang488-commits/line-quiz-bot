# -*- coding: utf-8 -*-
import os
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

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

# ===== コマンド（日本語・英語どちらでも可） =====
CMD_DONE = "__DONE__"
CMD_CLEAR = "__CLEAR__"
CMD_SKIP = "__SKIP__"
CMD_FREE = "__FREE__"

CMD_RESET = ("reset", "/reset", "リセット")
CMD_START = ("start", "/start", "開始")

# ===== ユーザー状態 =====
# STATE[user_id] = {
#   "pos": 1,
#   "answers": {qid: value or [values] or str},
#   "multi_selected": set(),
# }
STATE: Dict[str, Dict[str, Any]] = {}

# ===== 質問データの読み込み =====
def load_questions() -> List[Dict[str, Any]]:
    p = Path(__file__).with_name("questions_30.json")
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("questions_30.json is empty or invalid")
    # id昇順にソート（念のため）
    try:
        data.sort(key=lambda q: int(q.get("id", 0)))
    except Exception:
        pass
    return data

QUESTIONS = load_questions()
QMAX = len(QUESTIONS)

# ===== 質問ユーティリティ =====
def q_by_pos(pos: int) -> Optional[Dict[str, Any]]:
    idx = pos - 1
    if 0 <= idx < len(QUESTIONS):
        return QUESTIONS[idx]
    return None

def q_kind(q: Dict[str, Any]) -> str:
    # "input" / "single" / "multi" を返す
    t = (q.get("type") or "").strip().lower()
    if t in ("input", "free", "text"):
        return "input"
    if t in ("multi", "multiple"):
        return "multi"
    return "single"

def q_id(q: Dict[str, Any]) -> str:
    return str(q.get("id"))

def q_title(q: Dict[str, Any]) -> str:
    # 表示タイトル
    return str(q.get("question") or f"Q{q_id(q)}")

def q_desc(q: Dict[str, Any]) -> str:
    return str(q.get("desc") or "")

def q_choices(q: Dict[str, Any]) -> List[str]:
    # ボタンに出すラベルの配列
    lst = []
    for opt in q.get("options", []) or []:
        if isinstance(opt, dict):
            text = str(opt.get("text") or "").strip()
            if text:
                lst.append(text)
        elif isinstance(opt, str):
            if opt.strip():
                lst.append(opt.strip())
    return lst

def choice_to_value(q: Dict[str, Any], label: str):
    """
    表示ラベル → 保存値（タグ or ラベル）へ変換
    """
    if isinstance(q.get("options"), list):
        for opt in q["options"]:
            if isinstance(opt, dict) and str(opt.get("text")) == label:
                return opt.get("tag") or opt.get("text")
    return label

def build_question_text(q: Dict[str, Any]) -> str:
    lines = [f"Q{q_id(q)}. {q_title(q)}"]
    if q_desc(q):
        lines.append(f"説明: {q_desc(q)}")
    return "\n".join(lines)

# ===== Quick Reply =====
def make_quick_reply_for_single(choices: List[str]) -> QuickReply:
    items = [QuickReplyButton(action=MessageAction(label=c[:20], text=c)) for c in choices[:13]]
    # 追加ボタン
    items.append(QuickReplyButton(action=MessageAction(label="➡ スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)

def make_quick_reply_for_multi(choices: List[str], selected_count: int) -> QuickReply:
    items = [QuickReplyButton(action=MessageAction(label=c[:20], text=c)) for c in choices[:9]]
    # コントロール
    items.append(QuickReplyButton(action=MessageAction(label=f"✅ 完了（{selected_count}）", text=CMD_DONE)))
    items.append(QuickReplyButton(action=MessageAction(label="↺ クリア", text=CMD_CLEAR)))
    items.append(QuickReplyButton(action=MessageAction(label="➡ スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)

# ===== 質問送信 =====
def send_question(user_id: str, reply_token: str):
    st = STATE[user_id]
    q = q_by_pos(st["pos"])
    if not q:
        done = f"回答ありがとうございました！（全{QMAX}問）\n※「{CMD_START[0]}」でやり直し、「{CMD_RESET[0]}」で状態クリア。"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=done))
        # ここで必ず STATE を削除
        STATE.pop(user_id, None)
        return

    kind = q_kind(q)
    title = build_question_text(q)

    if kind == "input":
        st["await_input"] = True
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=title + "\n\n自由回答を1行で送ってください。")
        )
        return

    labels = q_choices(q)
    if kind == "single":
        qr = make_quick_reply_for_single(labels)
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=title, quick_reply=qr)
        )
        return

    if kind == "multi":
        if "multi_selected" not in st:
            st["multi_selected"] = set()
        qr = make_quick_reply_for_multi(labels, len(st["multi_selected"]))
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=title + "\n（ON/OFF切替して「✅ 完了」で確定）", quick_reply=qr)
        )

def current_question(st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return q_by_pos(st["pos"])

def advance(st: Dict[str, Any]):
    st["pos"] += 1

def new_state() -> Dict[str, Any]:
    return {"pos": 1, "answers": {}, "multi_selected": set(), "await_input": False}

# ===== healthz / callback =====
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(401)
    except Exception:
        abort(500)
    return "OK", 200

# ===== メッセージ受信 =====
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_id = event.source.user_id or getattr(event.source, "sender_id", None)
    if not user_id:
        return

    text = (event.message.text or "").strip()
    text_lower = text.lower()

    # --- リセット ---
    if text in CMD_RESET or text_lower in ("reset", "/reset"):
        STATE.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="OK: リセット"))
        return

    # --- 開始 ---
    if text in CMD_START or text_lower in ("start", "/start"):
        STATE[user_id] = new_state()  # ★必ず pos=1 に初期化
        send_question(user_id, event.reply_token)
        return

    # 初回メッセージでまだSTATEが無ければ新規作成（Q1から）
    if user_id not in STATE:
        STATE[user_id] = new_state()
        send_question(user_id, event.reply_token)
        return

    # 以降：回答処理
    st = STATE[user_id]
    q = current_question(st)
    if not q:
        # 念のため
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="終了しています。『開始』で最初からどうぞ。"))
        STATE.pop(user_id, None)
        return

    kind = q_kind(q)
    labels = q_choices(q)

    # ---- input（自由入力） ----
    if st.get("await_input") and kind == "input":
        st["await_input"] = False
        st["answers"][q_id(q)] = text
        advance(st)
        send_question(user_id, event.reply_token)
        return

    # ---- single ----
    if kind == "single":
        if text == CMD_SKIP:
            st["answers"][q_id(q)] = None
            advance(st)
            send_question(user_id, event.reply_token)
            return

        if text not in labels:
            # 想定外入力はリマインド
            qr = make_quick_reply_for_single(labels)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="選択肢から選んでください。", quick_reply=qr)
            )
            return

        st["answers"][q_id(q)] = choice_to_value(q, text)
        advance(st)
        send_question(user_id, event.reply_token)
        return

    # ---- multi ----
    if kind == "multi":
        if text == CMD_CLEAR:
            st["multi_selected"].clear()
            qr = make_quick_reply_for_multi(labels, len(st["multi_selected"]))
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="選択をクリアしました。", quick_reply=qr)
            )
            return

        if text == CMD_SKIP:
            st["answers"][q_id(q)] = []
            st["multi_selected"].clear()
            advance(st)
            send_question(user_id, event.reply_token)
            return

        if text == CMD_DONE:
            chosen = list(st["multi_selected"])
            # 必要なら min/max を見る
            min_required = int(q.get("min", 0))
            max_allowed = int(q.get("max", 0)) if q.get("max") else None
            if min_required and len(chosen) < min_required:
                qr = make_quick_reply_for_multi(labels, len(st["multi_selected"]))
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"最低 {min_required} 個選んでください。", quick_reply=qr)
                )
                return
            if max_allowed and len(chosen) > max_allowed:
                qr = make_quick_reply_for_multi(labels, len(st["multi_selected"]))
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"最大 {max_allowed} 個までです。", quick_reply=qr)
                )
                return

            st["answers"][q_id(q)] = [choice_to_value(q, x) for x in chosen]
            st["multi_selected"].clear()
            advance(st)
            send_question(user_id, event.reply_token)
            return

        # ラベルのON/OFF切替
        if text in labels:
            if text in st["multi_selected"]:
                st["multi_selected"].remove(text)
                msg = f"解除: {text}\n現在: {', '.join(st['multi_selected']) or '（なし）'}"
            else:
                st["multi_selected"].add(text)
                msg = f"選択: {text}\n現在: {', '.join(st['multi_selected'])}"
            qr = make_quick_reply_for_multi(labels, len(st["multi_selected"]))
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=msg, quick_reply=qr)
            )
            return

        # 想定外入力はリマインド
        qr = make_quick_reply_for_multi(labels, len(st["multi_selected"]))
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="選択肢から選ぶか『✅ 完了』を押してください。", quick_reply=qr)
        )
        return

    # ---- その他（想定外） ----
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="『開始』でスタート、または『リセット』で状態をクリアできます。"))


# ===== ローカル実行 =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
