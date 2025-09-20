# app.py
# ------------------------------------------------------------
# LINE 公式アカウント：クイズ/設問ボット（Q1から順番に出題）
# - questions_30.json を app.py と同じフォルダに置く
# - 環境変数: CHANNEL_ACCESS_TOKEN, CHANNEL_SECRET
# ------------------------------------------------------------

import os
import json
import logging
from pathlib import Path
import json

def load_questions():
    p = Path(__file__).with_name("questions_30.json")  # ←ファイル名を固定
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError("questions_30.json is empty or invalid")
    # id順で並ぶよう一応ソート
    try:
        data.sort(key=lambda q: int(q.get("id", 0)))
    except Exception:
        pass
    print(f"[DEBUG] Loaded {len(data)} questions from {p.name}")  # ←ログに件数を出す
    return data

QUESTIONS = load_questions()

from pathlib import Path
from typing import Any, Dict, List, Optional


@app.get("/debug")
def debug():
    try:
        return {
            "loaded": len(QUESTIONS),
            "first": QUESTIONS[0].get("id"),
            "last": QUESTIONS[-1].get("id"),
        }
    except Exception as e:
        return {"error": str(e)}, 500


from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    QuickReply,
    QuickReplyButton,
    MessageAction,
)

# ------------  基本設定  ------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quiz-bot")

app = Flask(__name__)

STATE = {}

CMD_RESET = "__RESET__"

def new_state():
    return {"pos": 1, "answers": {}, "multi_selected": set(), "await_input": False}


LINE_TOKEN = os.environ["CHANNEL_ACCESS_TOKEN"]
LINE_SECRET = os.environ["CHANNEL_SECRET"]
line_bot_api = LineBotApi(LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# ------------  定数  ------------
CMD_DONE   = "__DONE__"     # multi の完了
CMD_CLEAR  = "__CLEAR__"    # multi の選択クリア
CMD_SKIP   = "__SKIP__"     # スキップ
CMD_FREE   = "__FREE__"     # 自由入力モード

# ------------  ユーザー状態 ------------
#  pos: 現在の設問インデックス
#  answers: { tag: value or [values] }
#  multi_selected: set()
#  await_input: input/free 入力待ち
#  free_for: 選択中の single/multi に対する自由入力の保存先 tag（なければ None）
STATE: Dict[str, Dict[str, Any]] = {}

# ------------  質問読み込み ------------
QUESTIONS_PATH = Path(__file__).with_name("questions_30.json")
with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
    QUESTIONS: List[Dict[str, Any]] = json.load(f)

# ------------  ユーティリティ ------------
def reset_state(user_id: str):
    STATE[user_id] = {
        "pos": 0,                 # ← Q1（index 0）から
        "answers": {},
        "multi_selected": set(),
        "await_input": False,
        "free_for": None,
    }

def q_id(q):     return q.get("id")
def q_tag(q):    return q.get("tag")
def q_kind(q):   return q.get("type")  # "input" | "single" | "multi"
def q_title(q):  return q.get("question")
def q_desc(q):   return q.get("desc")
def q_opts(q):   return [o.get("text") for o in q.get("options", [])]
def q_min(q):    return int(q.get("min", 1))
def q_max(q):    return int(q.get("max", 8))

def build_question_text(q: Dict[str, Any]) -> str:
    lines = [f"Q{q_id(q)}. {q_title(q)}"]
    if q_desc(q):
        lines.append(f"（{q_desc(q)}）")
    return "\n".join(lines)

def advance(user_id: str):
    st = STATE[user_id]
    st["pos"] += 1
    st["await_input"] = False
    st["free_for"] = None
    st["multi_selected"].clear()

def render_summary(answers: Dict[str, Any]) -> str:
    lines = ["✅ 回答まとめ"]
    for k, v in answers.items():
        if isinstance(v, list):
            val = "、".join(map(str, v))
        else:
            val = str(v)
        lines.append(f"- {k}: {val}")
    if len(lines) == 1:
        lines.append("（まだ回答がありません）")
    return "\n".join(lines)

# ------------  クイックリプライ ------------
def make_quick_reply_for_single(choices: List[str]) -> QuickReply:
    items = []
    for c in choices[:11]:  # 画面の見やすさ優先
        items.append(QuickReplyButton(action=MessageAction(label=c[:20], text=c)))
    items.append(QuickReplyButton(action=MessageAction(label="✍ 自由入力", text=CMD_FREE)))
    items.append(QuickReplyButton(action=MessageAction(label="〰 スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)

def make_quick_reply_for_multi(choices: List[str], selected_count: int) -> QuickReply:
    items = []
    for c in choices[:9]:
        items.append(QuickReplyButton(action=MessageAction(label=c[:20], text=c)))
    items.append(QuickReplyButton(action=MessageAction(label="✅ 完了",  text=CMD_DONE)))
    items.append(QuickReplyButton(action=MessageAction(label="➖ クリア", text=CMD_CLEAR)))
    items.append(QuickReplyButton(action=MessageAction(label="✍ 自由入力", text=CMD_FREE)))
    items.append(QuickReplyButton(action=MessageAction(label="〰 スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)

# ------------  出題 ------------
def ask_current_question(user_id: str, reply_token: str):
    st = STATE[user_id]
    pos = st["pos"]
    if pos >= len(QUESTIONS):
        line_bot_api.reply_message(reply_token, TextSendMessage(text=render_summary(st["answers"])))
        return

    q = QUESTIONS[pos]
    kind = q_kind(q)
    logger.info(f"ASK pos={pos} id={q_id(q)} type={kind} tag={q_tag(q)}")

    if kind == "input":
        st["await_input"] = True
        st["free_for"] = None
        msg = build_question_text(q) + "\n\n自由入力を1行で送ってください。"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=msg))
        return

    if kind == "single":
        qr = make_quick_reply_for_single(q_opts(q))
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=build_question_text(q), quick_reply=qr)
        )
        return

    # multi
    qr = make_quick_reply_for_multi(q_opts(q), selected_count=len(st["multi_selected"]))
    tips = "（5〜8択＋自由回答。押すたびにON/OFF。必要数そろったら「✅ 完了」。）"
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=build_question_text(q) + "\n\n" + tips, quick_reply=qr)
    )

# ------------  ルーティング ------------
@app.get("/healthz")
def healthz():
    return "OK", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.exception("Invalid signature")
        abort(401)
    except Exception:
        logger.exception("Unhandled error in callback")
    return "OK", 200

# ------------  メッセージ受信 ------------
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):

@handler.add(MessageEvent, message=TextMessage)
def on_message(event):
    user_id = event.source.user_id or event.source.sender_id
    text = (event.message.text or "").strip()

    # --- リセット ---
    if text in ("リセット", "reset", "/reset", CMD_RESET):
        STATE.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="OK: リセット"))
        return

    # --- 開始 ---
    if text in ("開始", "start", "/start"):
        STATE[user_id] = new_state()
        send_question(user_id, event.reply_token)  # ←出題用の関数名に置き換えて
        return

    # --- STATE 未定義のときの初期化 ---
    if user_id not in STATE:
        STATE[user_id] = new_state()

    st = STATE[user_id]

    # （ここから既存のQ&Aロジックにつなげる）


    
    user_id = event.source.user_id or event.source.sender_id
    text = (event.message.text or "").strip()

    if user_id not in STATE:
        reset_state(user_id)

    # 開始/リセット
    if text in ("開始", "リセット"):
        reset_state(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="OK: リセット"))
        ask_current_question(user_id, event.reply_token)
        return

    st = STATE[user_id]
    pos = st["pos"]
    if pos >= len(QUESTIONS):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=render_summary(st["answers"])))
        return

    q = QUESTIONS[pos]
    kind = q_kind(q)
    tag = q_tag(q)

    # ========= input / free 入力待ち =========
    if st["await_input"]:
        # input 問題（free_for=None）か、single/multi の自由入力（free_for=tag）
        if st["free_for"] is None:
            # input 設問への回答
            st["answers"][tag] = text
            advance(user_id)
            ask_current_question(user_id, event.reply_token)
            return
        else:
            # single/multi の自由入力
            target = st["free_for"]
            if kind == "single" and target == tag:
                st["answers"][tag] = text
                advance(user_id)
                ask_current_question(user_id, event.reply_token)
                return
            if kind == "multi" and target == tag:
                st["multi_selected"].add(text)
                st["await_input"] = False
                st["free_for"] = None
                msg = f"自由入力を追加：{text}\n現在：{ '、'.join(st['multi_selected']) or '（なし）' }"
                qr = make_quick_reply_for_multi(q_opts(q), selected_count=len(st["multi_selected"]))
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg, quick_reply=qr))
                return
        # タグがずれた場合はリカバリ
        st["await_input"] = False
        st["free_for"] = None
        ask_current_question(user_id, event.reply_token)
        return

    # ========= single =========
    if kind == "single":
        choices = q_opts(q)
        if text == CMD_SKIP:
            advance(user_id)
            ask_current_question(user_id, event.reply_token)
            return
        if text == CMD_FREE:
            st["await_input"] = True
            st["free_for"] = tag
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="自由入力を1行で送ってください。"))
            return
        if text in choices:
            st["answers"][tag] = text
            advance(user_id)
            ask_current_question(user_id, event.reply_token)
            return

        # 不正入力 → 再表示
        qr = make_quick_reply_for_single(choices)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="選択肢から選んでください。", quick_reply=qr)
        )
        return

    # ========= multi =========
    if kind == "multi":
        labels = set(q_opts(q))

        if text == CMD_CLEAR:
            st["multi_selected"].clear()
            qr = make_quick_reply_for_multi(list(labels), selected_count=0)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="選択をクリアしました。", quick_reply=qr)
            )
            return

        if text == CMD_SKIP:
            st["answers"][tag] = []
            st["multi_selected"].clear()
            advance(user_id)
            ask_current_question(user_id, event.reply_token)
            return

        if text == CMD_FREE:
            st["await_input"] = True
            st["free_for"] = tag
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="自由入力を1行で送ってください。"))
            return

        if text == CMD_DONE:
            chosen = list(st["multi_selected"])
            mi, mx = q_min(q), q_max(q)
            if not (mi <= len(chosen) <= mx):
                need = f"{mi}〜{mx}個選んでください。現在 {len(chosen)} 個"
                qr = make_quick_reply_for_multi(list(labels), selected_count=len(chosen))
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=need, quick_reply=qr))
                return
            st["answers"][tag] = chosen
            st["multi_selected"].clear()
            advance(user_id)
            ask_current_question(user_id, event.reply_token)
            return

        # ラベルのON/OFF
        if text in labels:
            if text in st["multi_selected"]:
                st["multi_selected"].remove(text)
                msg = f"解除：{text}\n現在：{ '、'.join(st['multi_selected']) or '（なし）' }"
            else:
                st["multi_selected"].add(text)
                msg = f"選択：{text}\n現在：{ '、'.join(st['multi_selected']) }"
            qr = make_quick_reply_for_multi(list(labels), selected_count=len(st["multi_selected"]))
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg, quick_reply=qr))
            return

        # 不正入力 → 再表示
        qr = make_quick_reply_for_multi(list(labels), selected_count=len(st["multi_selected"]))
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="操作が分からない場合は「✅ 完了」または「〰 スキップ」を押してください。", quick_reply=qr)
        )
        return

    # ここには来ない想定（保険）
    ask_current_question(user_id, event.reply_token)


# ------------  ローカル実行 ------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))



