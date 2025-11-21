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

# ==============================================================================
# CONFIGURAÇÕES
# ==============================================================================

# IDs da Planilha
SHEET_ID = "1UfAxtLmB5LKNGoIOhue5jruSOA-AyC3CCQK6-P5YGvc"

# Verifica se está no Render (pasta secreta) ou no PC local
if os.path.exists("/etc/secrets/google-creds.json"):
    GOOGLE_SA_JSON_PATH = "/etc/secrets/google-creds.json"
else:
    GOOGLE_SA_JSON_PATH = "google-creds.json"

# Chaves de API (O código busca lá nas configurações do Render)
# ATENÇÃO: Não escreva sua senha aqui. Deixe exatamente como está abaixo.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")

app = Flask(__name__)

# Configuração de Log
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
    except:
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
            model="gpt-3.5-turbo", messages=[{"role": "user", "content": prompt}], temperature=0, max_tokens=120
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
# FUNÇÃO DE ENVIO (META WHATSAPP)
# ------------------------------------------------------------------------------


def send_whatsapp_message(to_number, message):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logger.error("Faltam credenciais da Meta (Token ou Phone ID)")
        return

    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    # Limpeza do número (Meta aceita apenas números, sem +)
        clean_number = to_number.replace("+", "").replace("whatsapp:", "")
        
        # --- CORREÇÃO BRASIL (ADICIONA O 9 SE FALTAR) ---
        # Se o número começar com 55 (Brasil) e tiver 12 dígitos (faltando o 9), a gente insere.
        if clean_number.startswith("55") and len(clean_number) == 12:
            clean_number = clean_number[:4] + "9" + clean_number[4:]
        
        data = {
        "messaging_product": "whatsapp",
        "to": clean_number,
        "type": "text",
        "text": {"body": message},
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code not in [200, 201]:
            logger.error(f"Erro Meta: {response.text}")
        else:
            logger.info("Mensagem enviada com sucesso via Meta!")
    except Exception as e:
        logger.error(f"Erro requisição Meta: {e}")

# ------------------------------------------------------------------------------
# WEBHOOK
# ------------------------------------------------------------------------------


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    # Verificação inicial da Meta
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("WEBHOOK VERIFICADO COM SUCESSO!")
        return challenge, 200
    else:
        return "Erro de verificação", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        body = request.get_json()

        if body.get("object") == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if "messages" in value:
                        message = value["messages"][0]
                        from_number = message["from"]
                        msg_body = message["text"]["body"]

                        logger.info(
                            f"Mensagem recebida de {from_number}: {msg_body}")

                        # 1. Processar
                        amount, category, note, payment, t_type = parse_expense_openai(
                            msg_body)

                        # 2. Salvar
                        gc = get_gspread_client()
                        if gc:
                            sh = gc.open_by_key(SHEET_ID)
                            try:
                                ws = sh.worksheet("Extrato_Geral")
                            except:
                                ws = sh.get_worksheet(0)

                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            val_fmt = f"{-abs(amount) if t_type == 'expense' else abs(amount):.2f}".replace(
                                ".", ",")

                            ws.append_row(
                                [timestamp, val_fmt, category, note, payment, t_type, msg_body])

                            # 3. Responder
                            reply_text = f"✅ Salvo!\nR$ {amount} ({category})"
                            send_whatsapp_message(from_number, reply_text)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return jsonify({"status": "error"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
