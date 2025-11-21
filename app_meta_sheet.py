# app_meta_sheet.py
import os
import sys
import json
import logging
from datetime import datetime
import requests
from flask import Flask, request, jsonify
import openai
import gspread
from google.oauth2.service_account import Credentials

# Twilio
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

# ==============================================================================
# CONFIGURAÇÕES
# ==============================================================================

# IDs da Planilha (mantive o seu default)
SHEET_ID = os.environ.get(
    "SHEET_ID", "1UfAxtLmB5LKNGoIOhue5jruSOA-AyC3CCQK6-P5YGvc")

# Verifica se está no Render (pasta secreta) ou no PC local
if os.path.exists("/etc/secrets/google-creds.json"):
    GOOGLE_SA_JSON_PATH = "/etc/secrets/google-creds.json"
else:
    GOOGLE_SA_JSON_PATH = os.environ.get(
        "GOOGLE_SA_JSON_PATH", "google-creds.json")

# Chaves de API (O código busca lá nas configurações do Render)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Meta (fallback)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")

# Twilio credentials (preferred)
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get(
    "TWILIO_WHATSAPP_FROM")  # ex: +14155238886

# Init Flask and logging
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# FUNÇÕES AUXILIARES
# ------------------------------------------------------------------------------


def normalize_text(text):
    import unicodedata
    if not text:
        return "Outros"
    try:
        return unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII').title()
    except Exception:
        return str(text).title()


def get_gspread_client():
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(
            GOOGLE_SA_JSON_PATH, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"Erro gspread auth: {e}")
        return None


def parse_expense_openai(text):
    try:
        prompt = f"""
        Analise: "{text}".
        Retorne apenas um JSON com as chaves: amount (float), category (string), note (string), payment (string), type (expense|income)
        Exemplo: {{"amount": 0.0, "category": "X", "note": "Y", "payment": "Pix", "type": "expense"}}
        """
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=120
        )
        content = resp.choices[0].message.content.strip()
        s = content.find('{')
        e = content.rfind('}') + 1
        if s != -1 and e > s:
            data = json.loads(content[s:e])
            return (
                float(data.get("amount", 0.0)),
                normalize_text(data.get("category", "Outros")),
                str(data.get("note", text)),
                normalize_text(data.get("payment", "Outros")),
                str(data.get("type", "expense"))
            )
        return 0.0, "Geral", text, "Outros", "expense"
    except Exception as e:
        logger.error(f"OpenAI Error: {e}")
        return 0.0, "Geral", text, "Outros", "expense"


# ------------------------------------------------------------------------------
# ENVIO: Twilio ou Meta (fallback)
# ------------------------------------------------------------------------------

def send_via_twilio(to_number, message):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        logger.error("Twilio não configurado (variáveis ausentes).")
        return False
    if TwilioClient is None:
        logger.error("Twilio SDK não instalado.")
        return False

    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        # Twilio espera formato 'whatsapp:+55119xxxxxxx'
        to = to_number
        if not to.startswith("whatsapp:"):
            if not to.startswith("+"):
                # assume número sem + -> assume já vem com DDI? tentar adicionar + se necessário
                to = f"whatsapp:+{to}"
            else:
                to = f"whatsapp:{to}"
        from_ = TWILIO_WHATSAPP_FROM
        if not from_.startswith("whatsapp:"):
            from_ = f"whatsapp:{from_}"
        msg = client.messages.create(body=message, from_=from_, to=to)
        logger.info(
            f"Mensagem enviada via Twilio, sid={getattr(msg, 'sid', None)}")
        return True
    except Exception as e:
        logger.error(f"Erro envio Twilio: {e}")
        return False


def send_via_meta(to_number, message):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logger.error("Faltam credenciais da Meta (Token ou Phone ID)")
        return False

    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    # Meta aceita número sem '+'
    clean_number = to_number.replace("+", "").replace("whatsapp:", "")

    data = {
        "messaging_product": "whatsapp",
        "to": clean_number,
        "type": "text",
        "text": {"body": message},
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code not in [200, 201]:
            logger.error(f"Erro Meta: {response.status_code} {response.text}")
            return False
        logger.info("Mensagem enviada com sucesso via Meta!")
        return True
    except Exception as e:
        logger.error(f"Erro requisição Meta: {e}")
        return False


def send_whatsapp_message(to_number, message):
    """
    Envia mensagem preferencialmente via Twilio se configurado.
    to_number pode vir em formatos: "+5511999998888", "5511999998888", "whatsapp:+5511999998888"
    """
    # Preferência: Twilio
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM:
        ok = send_via_twilio(to_number, message)
        if ok:
            return
        # se falhar, tenta fallback para Meta
        logger.warning("Envio via Twilio falhou — tentando Meta (fallback).")

    # Fallback Meta
    send_via_meta(to_number, message)


# ------------------------------------------------------------------------------
# WEBHOOK
# ------------------------------------------------------------------------------


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    # Mantive verificação GET para Meta (se você usar Meta)
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token and VERIFY_TOKEN and token == VERIFY_TOKEN:
        logger.info("WEBHOOK VERIFICADO COM SUCESSO!")
        return challenge, 200
    # Para Twilio não é necessário; apenas responda 200 para health checks
    return "OK", 200


@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        # 1) Primeiro ver se é Twilio (form-encoded)
        if request.content_type and ("application/x-www-form-urlencoded" in request.content_type or "multipart/form-data" in request.content_type):
            # Twilio envia From e Body em form-encoded
            from_number = request.form.get("From") or request.form.get("from")
            body = request.form.get("Body") or request.form.get("body")
            if from_number and body:
                logger.info(
                    f"Webhook Twilio recebido de {from_number}: {body}")
                # normalize Twilio "whatsapp:+55...." -> "+55..."
                if from_number.startswith("whatsapp:"):
                    from_number = from_number.replace("whatsapp:", "")
                # Processa a mensagem
                process_incoming_message(from_number, body)
                return jsonify({"status": "ok"}), 200

        # 2) Se não for Twilio, tenta o payload do Meta
        body_json = request.get_json(silent=True)
        if body_json and body_json.get("object") == "whatsapp_business_account":
            for entry in body_json.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if "messages" in value:
                        message = value["messages"][0]
                        from_number = message.get("from")
                        # mensagem pode ser text ou template ou interactive
                        text_obj = message.get("text") or {}
                        msg_body = text_obj.get("body") or ""
                        logger.info(
                            f"Webhook Meta recebido de {from_number}: {msg_body}")
                        process_incoming_message(from_number, msg_body)
            return jsonify({"status": "ok"}), 200

        # 3) Caso não reconheça, retorna 200 para evitar retry infinito
        logger.warning("Webhook recebido com formato desconhecido.")
        return jsonify({"status": "ignored"}), 200

    except Exception as e:
        logger.error(f"Erro no webhook: {e}", exc_info=True)
        return jsonify({"status": "error"}), 500


def process_incoming_message(from_number, msg_body):
    """
    Rotina central de processamento: parse + grava no Sheets + responde.
    from_number esperado: '+5511999998888' ou '5511999998888' ou similar.
    """
    try:
        # 1. Processar via OpenAI
        amount, category, note, payment, t_type = parse_expense_openai(
            msg_body)

        # 2. Salvar no Sheets
        gc = get_gspread_client()
        if gc:
            sh = gc.open_by_key(SHEET_ID)
            try:
                ws = sh.worksheet("Extrato_Geral")
            except Exception:
                ws = sh.get_worksheet(0)

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            val_fmt = f"{-abs(amount) if t_type == 'expense' else abs(amount):.2f}".replace(".", ",")

            try:
                ws.append_row([timestamp, val_fmt, category,
                              note, payment, t_type, msg_body])
                logger.info("Linha adicionada na planilha.")
            except Exception as e:
                logger.error(f"Erro ao gravar na planilha: {e}")

        # 3. Responder via Twilio/Meta
        reply_text = f"✅ Salvo!\nR$ {amount:.2f} ({category})"
        # garantir prefixo + no número para Twilio
        to_num = from_number
        if not to_num.startswith("+") and to_num.isdigit():
            to_num = f"+{to_num}"
        send_whatsapp_message(to_num, reply_text)

    except Exception as e:
        logger.error(f"Erro em process_incoming_message: {e}", exc_info=True)


if __name__ == "__main__":
    # Porta 5000 por padrão (Render/Heroku usam PORT env var) - respeite variável PORT se existir
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
