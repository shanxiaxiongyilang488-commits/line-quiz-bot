# -*- coding: utf-8 -*-
# LINE Messaging API Webhook (Flask) — 10問＋5問の性格形成テスト
import os, json, logging, urllib.parse
from datetime import datetime, timezone
from flask import Flask, request
from dotenv import load_dotenv
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, QuickReply, QuickReplyButton, PostbackAction)

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),
    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN) if CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(CHANNEL_SECRET) if CHANNEL_SECRET else None

QUESTIONS = [
  {"id":0,"text":"キャラクターの名前は？","hint":"自由に名前をつけてください。","options":None,"multi":False,"free":True},
  {"id":1,"text":"一人称は？","hint":"キャラが自分を呼ぶときの言葉。","options":["わたし","あたし","ボク","オレ","その他"],"multi":False,"free":False},
  {"id":2,"text":"二人称は？","hint":"相手の呼び方。","options":["あなた","君","RootSさん","プロデューサー","その他"],"multi":False,"free":False},
  {"id":3,"text":"口癖（複数可）","hint":"トグル選択→『決定』で確定。","options":["なし","〜なのです","〜だよね","〜かな？","〜かも","…ね？","ふふっ","へぇ〜","なるほど〜","その他"],"multi":True,"free":False},
  {"id":4,"text":"語尾・文体のトーン","hint":"普段のしゃべり方。","options":["です・ます調","だよ・だね調","〜っス系","切替える"],"multi":False,"free":False},
  {"id":5,"text":"一言キャッチ","hint":"キャラを一言で。","options":None,"multi":False,"free":True},
  {"id":6,"text":"性格の軸","hint":"全体的な雰囲気。","options":["明るい","クール","天然","ツンデレ","ミステリアス","その他"],"multi":False,"free":False},
  {"id":7,"text":"エネルギーレベル","hint":"普段のテンション。","options":["低め","ふつう","高め","波がある"],"multi":False,"free":False},
  {"id":8,"text":"ユーモア度合い","hint":"冗談の取り入れ具合。","options":["控えめ","ほどよく","多め","ギャグ担当"],"multi":False,"free":False},
  {"id":9,"text":"丁寧さ","hint":"敬語かフランクか。","options":["基本は敬語","切り替える","基本はカジュアル","礼儀正しいが親しみやすい"],"multi":False,"free":False},
  {"id":11,"text":"説明スタイル","hint":"説明の傾向。","options":["かなり論理的","わかりやすさ重視","バランス型","感覚派","即断即決型","その他"],"multi":False,"free":False},
  {"id":12,"text":"褒め方（複数可）","hint":"『決定』で確定。","options":["さりげなく","ストレートに","大げさに","分析的に","結果を褒める","過程を褒める","比喩で褒める","その他"],"multi":True,"free":False},
  {"id":13,"text":"注意・指摘（複数可）","hint":"『決定』で確定。","options":["やわらかく婉曲に","事実ベースで率直に","ユーモアで和らげる","厳しくピシッと","質問で気づかせる","代替案を添える","個別に静かに","その他"],"multi":True,"free":False},
  {"id":14,"text":"リアクションの大きさ","hint":"反応の大きさ。","options":["控えめ","ふつう","大きめ","芸人級"],"multi":False,"free":False},
  {"id":15,"text":"絵文字を使う？","hint":"テキストでの絵文字使用。","options":["使わない","時々使う","よく使う","乱用する"],"multi":False,"free":False},
]
ORDER = [0,1,2,3,4,5,6,7,8,9,11,12,13,14,15]

SESSIONS = {}  # user_id: {idx, answers, multi:set, waiting_free, page}
def now_iso(): return datetime.now(timezone.utc).isoformat()
def start_session(uid): SESSIONS[uid] = {"idx":0,"answers":{}, "multi":set(),"waiting_free":None,"page":0,"ts":now_iso()}
def current_q(s): return next(q for q in QUESTIONS if q["id"]==ORDER[s["idx"]])

MAX_QR=13; CTRL=3; PAGE=MAX_QR-CTRL
def build_qr(s, q):
    items=[]; prog=f"{s['idx']+1}/{len(ORDER)}"
    sel = "選択中: " + (" / ".join(list(s["multi"]))[:180] if (q.get("multi") and s["multi"]) else "なし") if q.get("multi") else ""
    if q.get("options") and q.get("multi"):
        opts=q["options"]; k=s.get("page",0)*PAGE; chunk=opts[k:k+PAGE]; picked=s["multi"]
        for opt in chunk:
            mark="✅" if opt in picked else "□"
            items.append(QuickReplyButton(action=PostbackAction(label=f"{mark} {opt}", data=f"type=toggle&q={q['id']}&v={urllib.parse.quote(opt)}")))
        if k>0: items.append(QuickReplyButton(action=PostbackAction(label="◀ 前", data=f"type=page&q={q['id']}&d=prev")))
        if k+PAGE<len(opts): items.append(QuickReplyButton(action=PostbackAction(label="次 ▶", data=f"type=page&q={q['id']}&d=next")))
        for lab,typ in [("決定","decide"),("クリア","clear"),("スキップ","skip")]:
            items.append(QuickReplyButton(action=PostbackAction(label=lab, data=f"type={typ}&q={q['id']}")))
    elif q.get("options"):
        for opt in q["options"]:
            if opt=="その他": items.append(QuickReplyButton(action=PostbackAction(label="その他（自由入力）", data=f"type=other&q={q['id']}")))
            else: items.append(QuickReplyButton(action=PostbackAction(label=opt, data=f"type=choose&q={q['id']}&v={urllib.parse.quote(opt)}")))
        items.append(QuickReplyButton(action=PostbackAction(label="スキップ", data=f"type=skip&q={q['id']}")))
    else:
        items.append(QuickReplyButton(action=PostbackAction(label="スキップ", data=f"type=skip&q={q['id']}")))
    header=f"Q{q['id']}: {q['text']}（{prog}）"; hint=q.get("hint","")
    txt=f"{header}\n{(sel + '\n\n' if sel else '')}{hint}"
    return txt, QuickReply(items=items)

def safe_reply(token, msg:TextSendMessage):
    if not line_bot_api: return
    try: line_bot_api.reply_message(token, msg)
    except LineBotApiError as e: logger.error(f"[LINE API] {e}")

def send_q(uid, token):
    s=SESSIONS[uid]; q=current_q(s); txt,qr=build_qr(s,q); safe_reply(token, TextSendMessage(text=txt, quick_reply=qr))

def next_q(uid):
    s=SESSIONS[uid]
    if s["idx"]<len(ORDER)-1:
        s["idx"]+=1; s["multi"]=set(); s["waiting_free"]=None; s["page"]=0; s["ts"]=now_iso(); return True
    return False

def summary(s): return json.dumps({"answers_by_id":s["answers"],"finished_at":now_iso()}, ensure_ascii=False, indent=2)

@app.get("/")
def root(): return "ok"
@app.get("/healthz")
def healthz(): return "ok"

@app.post("/callback")
def callback():
    sig=request.headers.get("X-Line-Signature"); body=request.get_data(as_text=True)
    try:
        if not handler: return "ok",200
        handler.handle(body, sig)
    except InvalidSignatureError: return "bad signature",400
    except Exception: return "ok",200
    return "OK"

@handler.add(MessageEvent, message=TextMessage) if handler else (lambda f:f)
def on_message(event):
    uid=event.source.user_id; text=(event.message.text or "").strip()
    if text in ("開始","start","テスト","quiz"):
        start_session(uid); send_q(uid,event.reply_token); return
    s=SESSIONS.get(uid)
    if s and s.get("waiting_free"):
        qid=s["waiting_free"]; 
        if text: s["answers"][str(qid)]=text
        if next_q(uid): send_q(uid,event.reply_token)
        else: safe_reply(event.reply_token, TextSendMessage(text=f"完了！\n{summary(s)}"))
        return
    if not s: safe_reply(event.reply_token, TextSendMessage(text="「開始」と送るとテストを始めます。")); return
    send_q(uid,event.reply_token)

@handler.add(PostbackEvent) if handler else (lambda f:f)
def on_postback(event):
    uid=event.source.user_id; s=SESSIONS.get(uid)
    if not s: start_session(uid); send_q(uid,event.reply_token); return
    d=urllib.parse.parse_qs(event.postback.data or ""); typ=(d.get("type",[None])[0]); qid=int(d.get("q",[ORDER[0]])[0])
    q=next(x for x in QUESTIONS if x["id"]==qid)
    if typ=="page":
        s["page"]=max(0, s.get("page",0)+(-1 if (d.get("d",["next"])[0]=="prev") else 1))
        txt,qr=build_qr(s,q); safe_reply(event.reply_token, TextSendMessage(text=txt, quick_reply=qr)); return
    if typ=="toggle" and q["multi"]:
        v=urllib.parse.unquote(d.get("v",[""])[0]); (s["multi"].discard(v) if v in s["multi"] else s["multi"].add(v))
        txt,qr=build_qr(s,q); safe_reply(event.reply_token, TextSendMessage(text=txt, quick_reply=qr)); return
    if typ=="clear" and q["multi"]:
        s["multi"]=set(); txt,qr=build_qr(s,q); safe_reply(event.reply_token, TextSendMessage(text=txt, quick_reply=qr)); return
    if typ=="decide" and q["multi"]:
        vals=list(s["multi"]); 
        if vals: s["answers"][str(qid)]=vals
        if next_q(uid): send_q(uid,event.reply_token)
        else: safe_reply(event.reply_token, TextSendMessage(text=f"完了！\n{summary(s)}"))
        return
    if typ=="choose" and not q["multi"]:
        v=urllib.parse.unquote(d.get("v",[""])[0]); s["answers"][str(qid)]=v
        if next_q(uid): send_q(uid,event.reply_token)
        else: safe_reply(event.reply_token, TextSendMessage(text=f"完了！\n{summary(s)}"))
        return
    if typ=="other": s["waiting_free"]=qid; safe_reply(event.reply_token, TextSendMessage(text="その他の内容をテキストで送ってください。")); return
    if typ=="skip":
        if next_q(uid): send_q(uid,event.reply_token)
        else: safe_reply(event.reply_token, TextSendMessage(text=f"完了！\n{summary(s)}"))
        return
    send_q(uid,event.reply_token)

if __name__ == "__main__":
    port = int(os.getenv("PORT","8000"))
    app.run(host="0.0.0.0", port=port)
