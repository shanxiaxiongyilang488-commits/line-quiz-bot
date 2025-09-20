import os
import json
from pathlib import Path
from typing import Dict, Any, List

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction

# ===== 基本設定 =====
app = Flask(__name__)
CHANNEL_ACCESS_TOKEN = os.environ["CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["CHANNEL_SECRET"]

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ===== ユーザー状態（メモリ） =====
#  st = {"pos": 現在の出題番号(1始まり), "answers": {qid: value}}
STATE: Dict[str, Dict[str, Any]] = {}

CMD_RESET = "リセット"
CMD_START = "開始"

# ===== 質問の読み込み =====
def load_questions() -> List[Dict[str, Any]]:
    p = Path(__file__).with_name("questions_30.json")
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    # id順に並べる（文字列でも対応）
    data.sort(key=lambda q: int(q.get("id", 0)))
    return data

QUESTIONS = load_questions()
QMAX = len(QUESTIONS)

def new_state() -> Dict[str, Any]:
    return {"pos": 1, "answers": {}}

def current_question(st: Dict[str, Any]) -> Dict[str, Any] | None:
    """pos(1始まり)に対応する質問を返す"""
    pos = st.get("pos", 1)
    if 1 <= pos <= QMAX:
        # id が 1..QMAX である前提で id==pos を取る
        # 念のため index計算でも取れるようにしておく
        # まず id一致を探し、なければ index でフォールバック
        for q in QUESTIONS:
            try:
                if int(q.get("id", -1)) == int(pos):
                    return q
            except Exception:
                pass
        # フォールバック（idがズレていても動く）
        return QUESTIONS[pos - 1]
    return None

def advance(st: Dict[str, Any]):
    st["pos"] = int(st.get("pos", 1)) + 1

# ===== 出題メッセージ作成 =====
def build_question_text(q: Dict[str, Any]) -> str:
    title = q.get("question", "")
    qid = q.get("id", "?")
    desc = q.get("desc", "")
    lines = [f"Q{qid}. {title}"]
    if desc:
        lines.append(desc)
    return "\n".join(lines)

def make_quick_reply_for_choices(q: Dict[str, Any]) -> QuickReply | None:
    opts = q.get("options")
    if not isinstance(opts, list):
        return None
    items = []
    # 12個まで + 「スキップ」1個 の合計13制限に収める
    for c in opts[:12]:
        label = str(c.get("text") or c.get("tag") or "")[:20]
        if not label:
            continue
        items.append(QuickReplyButton(action=MessageAction(label=label, text=label)))
    if len(items) < 13:
        items.append(QuickReplyButton(action=MessageAction(label="➡ スキップ", text="スキップ")))
    return QuickReply(items=items)

def send_question(user_id: str, reply_token: str):
    st = STATE[user_id]
    q = current_question(st)
    if not q:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="全ての質問が終わりました。お疲れさまです！"))
        return
    text = build_question_text(q)
    qr = make_quick_reply_for_choices(q)
    line_bot_api.reply_message(reply_token, TextSendMessage(text=text, quick_reply=qr))

# ===== イベントハンドラ =====
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_id = event.source.user_id or getattr(event.source, "sender_id", None)
    if not user_id:
        return
    text = (event.message.text or "").strip()

    # --- リセット ---
    if text in (CMD_RESET, "reset", "/reset"):
        STATE.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="OK: リセット"))
        return

    # --- 開始 ---
    if text in (CMD_START, "start", "/start"):
        STATE[user_id] = new_state()           # ここで必ず pos=1 に初期化
        send_question(user_id, event.reply_token)
        return

    # 初回メッセージでまだSTATEが無ければ新規作成（Q1から）
    if user_id not in STATE:
        STATE[user_id] = new_state()

    st = STATE[user_id]
    q = current_question(st)

    # まだ始めていなければ開始をうながす
    if not q:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="「開始」と送ると第1問から始めます。"))
        return

    # 回答処理（シンプルにテキストを格納する。必要ならタグ変換など入れてください）
    qid = int(q.get("id", st["pos"]))
    if text == "スキップ":
        st["answers"][qid] = None
    else:
        st["answers"][qid] = text

    # 次へ
    advance(st)
    q_next = current_question(st)
    if q_next:
        send_question(user_id, event.reply_token)
    else:
        # すべて終了
        done = f"回答ありがとうございました！（全{QMAX}問）\n※「{CMD_START}」でやり直し、「{CMD_RESET}」で状態クリア。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=done))
        # 必要ならここで STATE を消す
        # STATE.pop(user_id, None)

# ===== ヘルスチェック & コールバック =====
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
        abort(400)
    return "OK"
