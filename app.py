import os
import json
import logging
from pathlib import Path
import json

# JSONファイルの読み込み
QUESTIONS_FILE = Path(__file__).with_name("questions_30.json")
with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)


from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
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
CMD_DONE  = "__DONE__"     # multi の完了
CMD_CLEAR = "__CLEAR__"    # multi の選択クリア
CMD_SKIP  = "__SKIP__"     # その設問を無回答でスキップ
CMD_FREE  = "__FREE__"     # 自由入力モード

# ---------- ユーザー状態 ----------
#   pos: 現在の設問インデックス
#   answers: { qid: value or [values] or str }
#   multi_selected: set()  途中の複数選択保持
#   await_input: bool      自由入力待ちか
STATE: Dict[str, Dict[str, Any]] = {}

# ---------- 質問ロード ----------
def load_questions() -> List[Dict[str, Any]]:
    p = Path(__file__).with_name("questions_30.json")
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("questions_30.json is empty or invalid")
    # id 昇順にしておく（念のため）
    try:
        data.sort(key=lambda q: int(q.get("id", 0)))
    except Exception:
        pass
    return data

QUESTIONS = load_questions()

# ---------- 便利関数 ----------
def q_id(q: Dict[str, Any]) -> str:
    return str(q.get("id", ""))

def q_title(q: Dict[str, Any]) -> str:
    return str(q.get("title", ""))

def q_desc(q: Dict[str, Any]) -> str:
    return str(q.get("desc", "")).strip()

def q_kind(q: Dict[str, Any]) -> str:
    # 'single' | 'multi' | 'free'
    return q.get("type", "single")

def q_choices(q: Dict[str, Any]) -> List[str]:
    # options の "text" を並べる
    opts = q.get("options", [])
    if isinstance(opts, list):
        return [str(item.get("text", "")) for item in opts if isinstance(item, dict)]
    return []

def start_or_reset_state(user_id: str):
    STATE[user_id] = {
        "pos": 0,
        "answers": {},
        "multi_selected": set(),
        "await_input": False,
    }

def ensure_state(user_id: str):
    if user_id not in STATE:
        start_or_reset_state(user_id)

def enforce_min_max(q: Dict[str, Any], selected: List[str]) -> Optional[str]:
    """min/max を満たさない時はエラーメッセージを返す。満たしていれば None"""
    kind = q_kind(q)
    if kind != "multi":
        return None
    min_req = int(q.get("min", 1))
    max_allowed = int(q.get("max", 8))
    n = len(selected)
    if n < min_req:
        return f"最低 {min_req} 個選んでください。現在 {n}/{min_req}。"
    if n > max_allowed:
        return f"最大 {max_allowed} 個までです。現在 {n}/{max_allowed}。"
    return None

def build_question_text(q: Dict[str, Any]) -> str:
    lines = [f"Q{q_id(q)}. {q_title(q)}"]
    desc = q_desc(q)
    if desc:
        lines.append(f"説明: {desc}")
    return "\n".join(lines)

def make_quick_reply_for_single(choices: List[str]) -> QuickReply:
    # 8〜12個に収まるように label を短めに
    items = [QuickReplyButton(action=MessageAction(label=c[:20], text=c)) for c in choices[:12]]
    items.append(QuickReplyButton(action=MessageAction(label="➡ スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)

def make_quick_reply_for_multi(choices: List[str], selected_count: int) -> QuickReply:
    items: List[QuickReplyButton] = []
    for c in choices[:9]:
        items.append(QuickReplyButton(action=MessageAction(label=c[:20], text=c)))
    items.append(QuickReplyButton(action=MessageAction(label=f"✅ 完了({selected_count})", text=CMD_DONE)))
    items.append(QuickReplyButton(action=MessageAction(label="➕ 自由入力", text=CMD_FREE)))
    items.append(QuickReplyButton(action=MessageAction(label="🧽 クリア", text=CMD_CLEAR)))
    items.append(QuickReplyButton(action=MessageAction(label="➡ スキップ", text=CMD_SKIP)))
    return QuickReply(items=items)

def make_quick_reply_for_free() -> QuickReply:
    items = [QuickReplyButton(action=MessageAction(label="キャンセル", text=CMD_CLEAR))]
    return QuickReply(items=items)

def send_question(user_id: str, q: Dict[str, Any]):
    kind = q_kind(q)
    title = build_question_text(q)

    if kind == "single":
        choices = q_choices(q)
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=title, quick_reply=make_quick_reply_for_single(choices))
        )
        return

    if kind == "multi":
        choices = q_choices(q)
        selected = list(STATE[user_id]["multi_selected"])
        msg = title + "\n（5～8択＋自由入力。押すたびにON/OFF。必要数そろったら「✅ 完了」。）"
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=msg, quick_reply=make_quick_reply_for_multi(choices, len(selected)))
        )
        return

    if kind == "free":
        STATE[user_id]["await_input"] = True
        msg = title + "\n自由回答を1行で送ってください。"
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=msg, quick_reply=make_quick_reply_for_free())
        )
        return

def advance(user_id: str):
    st = STATE[user_id]
    st["multi_selected"] = set()
    st["await_input"] = False

    st["pos"] += 1
    if st["pos"] >= len(QUESTIONS):
        # 結果を最後にまとめて送る
        send_result(user_id)
        return
    q = QUESTIONS[st["pos"]]
    send_question(user_id, q)

def send_result(user_id: str):
    st = STATE[user_id]
    answers = st["answers"]
    lines = ["【結果】"]
    for q in QUESTIONS:
        qid = q_id(q)
        val = answers.get(qid, "")
        if isinstance(val, list):
            val = ", ".join(val)
        lines.append(f"Q{qid}: {val}")
    text = "\n".join(lines) if len(lines) > 1 else "まだ回答がありません。"
    line_bot_api.push_message(user_id, TextSendMessage(text=text))

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

    # 強制リセット（ここが重要！）
    if text in ("開始", "最初から", "やり直し", "リセット"):
        start_or_reset_state(user_id)
        q = QUESTIONS[0]
        send_question(user_id, q)
        return

    # 結果だけ見たい
    if text == "結果":
        ensure_state(user_id)
        send_result(user_id)
        return

    ensure_state(user_id)
    st = STATE[user_id]
    pos = st["pos"]

    # 例外対策：pos が壊れていたら0に戻す
    if not isinstance(pos, int) or pos < 0 or pos >= len(QUESTIONS):
        start_or_reset_state(user_id)
        send_question(user_id, QUESTIONS[0])
        return

    q = QUESTIONS[pos]
    kind = q_kind(q)
    qid = q_id(q)

    # ---- free 入力待ち（free の確定は通常テキスト）----
    if st.get("await_input", False):
        if text == CMD_CLEAR:
            st["await_input"] = False
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="自由入力をキャンセルしました。")
            )
            send_question(user_id, q)  # 同じ設問を出し直す
            return
        # 1行を回答として保存
        st["answers"][qid] = text
        st["await_input"] = False
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"自由回答を記録しました: {text}"))
        advance(user_id)
        return

    # ---- single ----
    if kind == "single":
        if text == CMD_SKIP:
            st["answers"][qid] = ""
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="スキップしました。"))
            advance(user_id)
            return

        # どれかの選択肢
        if text in set(q_choices(q)):
            st["answers"][qid] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"選択: {text}"))
            advance(user_id)
            return

        # その他は無視してもう一度
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="選択肢から選んでください。"))
        send_question(user_id, q)
        return

    # ---- multi ----
    if kind == "multi":
        labels = set(q_choices(q))

        # クリア
        if text == CMD_CLEAR:
            st["multi_selected"].clear()
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="選択をクリアしました。", quick_reply=make_quick_reply_for_multi(list(labels), 0))
            )
            return

        # スキップ
        if text == CMD_SKIP:
            st["answers"][qid] = []
            st["multi_selected"].clear()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="スキップしました。"))
            advance(user_id)
            return

        # 自由入力モードへ
        if text == CMD_FREE:
            st["await_input"] = True
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="自由回答を1行で送ってください。", quick_reply=make_quick_reply_for_free())
            )
            return

        # 完了
        if text == CMD_DONE:
            chosen = list(st["multi_selected"])
            err = enforce_min_max(q, chosen)
            if err:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=err))
                send_question(user_id, q)
                return
            st["answers"][qid] = chosen
            st["multi_selected"].clear()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"確定: {', '.join(chosen) if chosen else '（なし）'}"))
            advance(user_id)
            return

        # 通常のトグル
        if text in labels:
            if text in st["multi_selected"]:
                st["multi_selected"].remove(text)
                msg = f"解除: {text}\n現在: {', '.join(st['multi_selected']) or '（なし）'}"
            else:
                st["multi_selected"].add(text)
                msg = f"選択: {text}\n現在: {', '.join(st['multi_selected'])}"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=msg, quick_reply=make_quick_reply_for_multi(list(labels), len(st['multi_selected'])))
            )
            return

        # その他
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="操作が分からない場合は「開始」と送ってください。"))
        return

    # ---- free（通常） ----
    if kind == "free":
        if text == CMD_CLEAR:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="取り消しました。"))
            send_question(user_id, q)
            return
        # single のような選択肢は無いので案内
        if text == CMD_SKIP:
            st["answers"][qid] = ""
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="スキップしました。"))
            advance(user_id)
            return
        # そのまま回答として受ける
        st["answers"][qid] = text
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"回答: {text}"))
        advance(user_id)
        return

